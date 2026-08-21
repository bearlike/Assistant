#!/usr/bin/env python3
"""The one place a project name becomes a directory, and the one place they are listed.

Four unrelated things in this tree are called a "project", and only the first
three are filesystem workspaces an agent can run in:

1. A **configured** project — :class:`~mewbo_core.config.ProjectConfig`, a
   directory an operator registered by hand in ``app.json``. Mewbo only ever
   points at it; it never creates, moves or deletes one.
2. A **managed** project — :class:`~mewbo_core.workspaces.project_store.VirtualProject`,
   a directory Mewbo created and owns. Addressed as ``managed:<project_id>``.
3. A **worktree** — a managed project with ``is_worktree`` set, which is a
   child checkout of another managed project on its own branch.
4. A **repository** — :class:`~mewbo_core.workspaces.repositories.Repository`, a git
   remote identity. Registration is INERT, so a repository has NO path until
   somebody checks it out; it is listed here so a caller can see that it
   exists, and refused by :meth:`ProjectCatalog.resolve` until it does.

(A wiki ``Project`` is an INDEX of a repository, not a workspace, and is
deliberately absent from this catalog.)

"Project name to path" has five call sites across the API app, and a private
resolution at each one drifts in the direction nobody watches: an endpoint that
resolves a configured project but returns nothing for ``managed:<id>`` scopes a
worktree-backed session to the unscoped list, with no error to read. One
resolver is what makes the agent-facing switch tool a thin caller rather than a
sixth divergent copy.

**This lives in core, not in the app, for the same reason the repository
registry does:** the CLI drives the tool-use loop in-process with no
``mewbo_api`` present, and the tools that consume this are core. An app-side
home would put the resolver above two of its own callers in the DAG.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mewbo_core.config import ProjectConfig
from mewbo_core.workspaces.project_identity import AUTO_PROJECT, ProjectIdentity
from mewbo_core.workspaces.project_store import ProjectStoreBase
from mewbo_core.workspaces.repositories import Repository
from mewbo_core.workspaces.repository_store import RepositoryStoreBase

# ``AUTO_PROJECT`` is re-exported here, where every resolver-side caller already
# looks for it, but it is DECLARED in ``project_identity`` — a module light
# enough for the session store's append hook to import, which this one is not
# (it pulls config, the project store and the repository store behind it). One
# literal, one home; a duplicated sentinel is exactly how ``app:<app_id>`` came
# to be stamped for the whole life of the Apps sub-product with no classifier
# arm able to read it.

# The prefix addressing a Mewbo-managed project. Pre-existing wire grammar --
# ``managed:<project_id>`` appears in stored session context, in the console's
# picker and in the Aura composer -- so it is named here rather than respelled.
MANAGED_PREFIX: str = "managed:"

ProjectKind = Literal["configured", "managed", "worktree", "repository"]


class ProjectResolutionError(ValueError):
    """A project key could not be turned into a usable directory.

    Carries ``code`` so a caller can branch on the KIND of refusal without
    parsing prose, and a message that NAMES what is available. The naming half
    is not politeness: the path guard learned the same lesson (see
    ``mewbo_tools/CLAUDE.md``) — a mute denial trains a model to hunt for
    workarounds, while a denial that lists the real options is corrected in one
    turn.
    """

    def __init__(self, code: str, message: str) -> None:
        """Bind the machine-readable code alongside the human-readable message."""
        super().__init__(message)
        self.code = code
        self.message = message


class ProjectEntry(BaseModel):
    """One workspace a session can be anchored to, in the form callers read it.

    ``key`` is the round-trip identity: whatever is listed here is what
    :meth:`ProjectCatalog.resolve` accepts and what a session's ``project``
    context field stores. ``path`` is ``None`` only for a registered repository
    with no checkout — the one entry kind that is genuinely listable but not
    yet runnable.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Identifier to pass back when selecting this project.")
    name: str = Field(description="Human-readable name.")
    kind: ProjectKind = Field(description="Which of the four project kinds this is.")
    path: str | None = Field(
        default=None,
        description="Absolute directory, or null for a repository with no checkout.",
    )
    description: str = Field(default="", description="Operator-supplied blurb, if any.")
    available: bool = Field(
        default=False,
        description="Whether the directory exists on disk right now.",
    )
    repo: str | None = Field(
        default=None, description="Canonical host/owner/repo slug, when known."
    )
    branch: str | None = Field(default=None, description="Branch, for worktrees.")
    parent_key: str | None = Field(
        default=None, description="Parent project key, for worktrees."
    )

    @property
    def runnable(self) -> bool:
        """Whether an agent could be pointed at this entry right now."""
        return bool(self.path) and self.available


class ProjectCatalog:
    """Lists every project a session may run in, and resolves one to a directory.

    Collaborators arrive as injected FIELDS rather than being reached for, so a
    test drives the whole surface with two in-memory stores and a dict — no
    config file, no filesystem beyond the paths it is handed.

    ``checkout_locator`` is the deliberate seam for the one fact core cannot
    compute for itself: which managed project (if any) is a checkout of a given
    repository slug. Answering that means matching a repository identity
    against each managed project's git remotes, and the canonical matcher lives
    in the API app, above core in the DAG. So the app injects it and a
    core-only caller (the CLI) simply gets repositories listed without a path,
    which is the honest answer there rather than a wrong one. Same rule that
    keeps ``RepositoryUsage`` a pure projection whose legs are handed in.
    """

    def __init__(
        self,
        *,
        configured: Mapping[str, ProjectConfig],
        project_store: ProjectStoreBase | None = None,
        repository_store: RepositoryStoreBase | None = None,
        checkout_locator: Callable[[str], str | None] | None = None,
    ) -> None:
        """Bind the four sources this catalog unions over."""
        self.configured = configured
        self.project_store = project_store
        self.repository_store = repository_store
        self.checkout_locator = checkout_locator

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def entries(self) -> tuple[ProjectEntry, ...]:
        """Every project, configured first, then managed/worktree, then repositories.

        Order is stable and meaningful rather than incidental: an operator's own
        directories lead, because those are the ones a human deliberately named.
        A store that raises is NOT allowed to empty the whole list — each source
        degrades independently, so one unreachable backend costs one section.
        """
        workspaces = self._configured_entries() + self._managed_entries()
        return tuple(workspaces + self._repository_entries(workspaces))

    def _configured_entries(self) -> list[ProjectEntry]:
        """Operator-registered directories from ``app.json``."""
        out: list[ProjectEntry] = []
        for name, cfg in self.configured.items():
            if not cfg.path:
                continue
            out.append(
                ProjectEntry(
                    key=name,
                    name=name,
                    kind="configured",
                    path=cfg.path,
                    description=cfg.description,
                    available=os.path.isdir(cfg.path),
                )
            )
        return out

    def _managed_entries(self) -> list[ProjectEntry]:
        """Mewbo-owned projects, with worktrees carrying their parent and branch."""
        if self.project_store is None:
            return []
        try:
            projects = self.project_store.list_projects()
        except Exception:  # noqa: BLE001 - one dead store must not empty the list
            return []
        out: list[ProjectEntry] = []
        for vp in projects:
            out.append(
                ProjectEntry(
                    key=f"{MANAGED_PREFIX}{vp.project_id}",
                    name=vp.name,
                    kind="worktree" if vp.is_worktree else "managed",
                    path=vp.path,
                    description=vp.description,
                    available=os.path.isdir(vp.path),
                    branch=vp.branch,
                    parent_key=(
                        f"{MANAGED_PREFIX}{vp.parent_project_id}"
                        if vp.parent_project_id
                        else None
                    ),
                )
            )
        return out

    def _repository_entries(self, workspaces: list[ProjectEntry]) -> list[ProjectEntry]:
        """Registered remotes, each resolved to its checkout when one exists.

        A repository whose checkout IS one of the workspaces already listed is
        not listed again — it would show one directory twice under two names,
        and a caller reading that list would reasonably conclude it had two
        places to work. Instead the identity is stamped onto the entry that is
        already there, so the connection is visible without the duplicate.

        **Dedup keys on the resolved PATH, not on the slug.** Keying on the slug
        was the first cut and it never fired: the check consulted ``repo`` on
        entries that had already been collected, but a configured or managed
        entry never carries one — only this method sets that field, on rows it
        had not appended yet. The set was therefore empty on every call, the
        skip was unreachable, and the duplicate the docstring promised to
        prevent was emitted every time. A directory is the thing that can be
        the same twice, so the directory is what the comparison must be about.
        """
        if self.repository_store is None:
            return []
        try:
            repositories = self.repository_store.list()
        except Exception:  # noqa: BLE001 - degrade this section, never the list
            return []
        by_path = {self.same_dir_key(e.path): e for e in workspaces if e.path}
        out: list[ProjectEntry] = []
        for repo in repositories:
            path = self._locate_checkout(repo)
            listed = by_path.get(self.same_dir_key(path)) if path else None
            if listed is not None:
                listed.repo = repo.slug
                continue
            out.append(
                ProjectEntry(
                    key=repo.slug,
                    name=repo.display_name,
                    kind="repository",
                    path=path,
                    description=repo.description or "",
                    available=bool(path) and os.path.isdir(str(path)),
                    repo=repo.slug,
                )
            )
        return out

    @staticmethod
    def same_dir_key(path: str | None) -> str:
        """Normalize a path so two spellings of one directory compare equal.

        ``realpath`` rather than ``abspath``: a managed project reached through
        a symlinked projects root and a checkout recorded by its physical path
        are the same workspace, and only resolving links makes them compare so.
        This is public because ``ExternalCwdPolicy`` in the API app compares a
        caller-supplied path against a session's bound directory through this
        same normalizer: one normalizer, not two.
        """
        if not path:
            return ""
        return os.path.realpath(path)

    def _locate_checkout(self, repo: Repository) -> str | None:
        """Ask the injected locator where this repository lives, if anywhere."""
        if self.checkout_locator is None:
            return None
        try:
            return self.checkout_locator(repo.slug)
        except Exception:  # noqa: BLE001 - a best-effort join, never a hard failure
            return None

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def find(self, key: str) -> ProjectEntry | None:
        """Return the entry a key names, or ``None``. Never raises."""
        cleaned = key.strip()
        if not cleaned:
            return None
        for entry in self.entries():
            if entry.key == cleaned:
                return entry
        return None

    def owns_path(self, path: str | None) -> bool:
        """Whether *path* is a directory the server minted or was configured with.

        This distinguishes a server-known directory from a path a caller
        invented. **Cost: O(collection).** It walks :meth:`entries`, the
        configured map plus every managed project and registered repository,
        with an ``os.path.isdir`` per row, so callers reach it only on a path
        that would otherwise be refused, never on a hot happy path.

        This does not check availability: a listed-but-missing directory remains
        server-known; existence is the caller's validation step, and answering
        both here would make one refusal wear two meanings.
        """
        if not path or not path.strip():
            return False
        key = self.same_dir_key(path)
        return any(
            entry.path and self.same_dir_key(entry.path) == key for entry in self.entries()
        )

    def resolve(self, key: str) -> str:
        """Turn a project key into an absolute directory, or refuse with a reason.

        The sentinel is refused HERE rather than being silently treated as
        "no project": ``auto`` means the model has not chosen yet, and a
        resolver that quietly handed back a temp directory for it would make
        "never chose" and "chose the scratch dir" indistinguishable at every
        call site downstream. Callers that legitimately fall back to a temp
        directory test for the sentinel themselves, before asking.
        """
        cleaned = key.strip()
        if not cleaned:
            raise ProjectResolutionError("empty", "No project was named.")
        if cleaned == AUTO_PROJECT:
            raise ProjectResolutionError(
                "auto_sentinel",
                f"'{AUTO_PROJECT}' is not a project; it means one has not been "
                "chosen yet. Pick a real project from the catalog.",
            )
        entry = self.find(cleaned)
        if entry is None:
            raise ProjectResolutionError(
                "not_found", f"Project '{cleaned}' is not known. {self._available_hint()}"
            )
        if not entry.path:
            raise ProjectResolutionError(
                "no_checkout",
                f"Repository '{cleaned}' is registered but has no local checkout, "
                "so there is no directory to work in yet. Check it out first, or "
                "choose a project that is already on disk.",
            )
        if not entry.available:
            raise ProjectResolutionError(
                "unavailable",
                f"Project '{cleaned}' points at '{entry.path}', which does not "
                "exist on this host. In Docker the directory must be mounted at "
                "the identical path inside the container.",
            )
        return entry.path

    def _available_hint(self) -> str:
        """Name what IS resolvable, so a refusal is correctable in one turn."""
        keys = [e.key for e in self.entries() if e.runnable]
        if not keys:
            return "No projects are currently available."
        shown = ", ".join(f"'{k}'" for k in keys[:12])
        suffix = f" (and {len(keys) - 12} more)" if len(keys) > 12 else ""
        return f"Available: {shown}{suffix}."


def is_auto_project(value: object) -> bool:
    """Whether a stored/requested project field is the auto-select sentinel.

    Thin alias over :meth:`ProjectIdentity.is_auto` so the sentinel test has one
    implementation; kept as a module-level name because every existing caller
    imports it from here.
    """
    return ProjectIdentity.is_auto(value)
