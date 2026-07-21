"""Editable wiki-project settings — the read/validate/write façade.

Post-onboarding CRUD for a wiki project's settings (model, ref, depth, language,
scope filters, dev-mode, description). Before this, those were captured once by
the wizard and effectively immutable: there was no PATCH route under
``/v1/wiki/*`` and no write target that survived a reindex.

**The architectural fact this module is built around.** Two records back a "wiki
project", and only one of them is editable:

- :class:`~mewbo_graph.wiki.types.Project` is a DISPLAY snapshot, rebuilt
  WHOLESALE by ``wiki_finalize`` / ``GraphOnlyIndexer`` on every successful
  (re)index. A field written directly onto it is silently wiped by the next one —
  which is why ``desc`` is ALSO persisted as an override on the settings record
  and read back at finalize (``finalize._resolve_project_desc``).
- :class:`~mewbo_graph.wiki.types.ProjectSettings` is the durable, slug-keyed
  record of what the project is CONFIGURED with. ``WikiIndexingJob.refresh``
  consults it first, so an edit here is what actually takes effect on the next
  index.

Everything except ``desc`` therefore **takes effect on the next index**, not
immediately — this façade never triggers one (the user drives that with the
existing Refresh action), so a settings edit is free and can't be used to bypass
the per-IP indexing rate limiter.

Paradigm: :class:`WikiProjectSettings` is an atomic class (the
``TriggerRoutesController`` / ``VcsPickupService`` idiom) — collaborators injected
as fields, every rule a method, no module-level mutable wiring. The routes in
``routes.py`` are thin adapters that construct one per request and let its
``WikiHTTPError`` raises map to the wire through the already-registered handler.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger
from mewbo_graph.wiki.credentials import CredentialScope
from mewbo_graph.wiki.types import (
    DepthMode,
    FilterMode,
    PlatformId,
    Project,
    ProjectSettings,
    WikiError,
    WizardSubmission,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import WikiHTTPError
from .jobs import (
    _durable_credential_scope,
    _latest_job_submission,
    submission_from_project,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Callable

    from mewbo_graph.wiki.store import WikiStoreBase

logging = get_logger(name="api.wiki.settings")


# ---------------------------------------------------------------------------
# Wire model — the PATCH body (transport only; never persisted as-is)
# ---------------------------------------------------------------------------


class ProjectSettingsPatch(BaseModel):
    """``PATCH /v1/wiki/projects/<slug>`` body — every field optional.

    ``extra="forbid"`` is load-bearing, not hygiene: it is what makes a client
    that tries to smuggle a ``token``, a ``slug`` rename, or a system-owned field
    (``pages``/``indexedAt``/``commitSha``/``landingPageId``) get a clean 400
    instead of having it silently ignored — the same rule ``CredentialUpsert``
    enforces on the credential registry.

    Every field is ``| None`` so it can be omitted. "Omitted" and "explicitly set
    to null" are distinguished by Pydantic's ``model_fields_set``, NOT by the
    value — that is how ``{"ref": null}`` clears a pinned branch (back to the
    repo's default) while a body that simply doesn't mention ``ref`` leaves it
    alone. :meth:`changes` is the one place that distinction is read.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model: str | None = None
    ref: str | None = None
    depth: DepthMode | None = None
    language: str | None = None
    filter_mode: FilterMode | None = Field(default=None, alias="filterMode")
    dirs: list[str] | None = None
    files: list[str] | None = None
    graph_only: bool | None = Field(default=None, alias="graphOnly")
    # Per-project cross-model fallback ladder. Flat ``list[str] | None`` — the
    # SAME name/type as ``WizardSubmission.fallback_models`` and
    # ``ProjectSettings.fallback_models`` (mewbo_graph.wiki.types), a pinned
    # cross-package contract: no shape translation at any hop. ``None`` = no
    # override (inherit the configured global policy); an explicit
    # ``{"fallbackModels": null}`` clears a project-specific override back to
    # that policy, while an omitted key leaves whatever is on file untouched
    # (:meth:`changes`).
    fallback_models: list[str] | None = Field(default=None, alias="fallbackModels")
    desc: str | None = None
    # Accepted, but IDENTITY-GUARDED (see WikiProjectSettings._guard_identity):
    # only a re-normalisation of the SAME repo is allowed. Re-pointing a slug at
    # a different repo is a delete+recreate, never a PATCH.
    repo_url: str | None = Field(default=None, alias="repoUrl")
    platform: PlatformId | None = None

    @field_validator("model", "language", "ref", "desc")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        """Strip surrounding whitespace; a whitespace-only value becomes ``None``.

        For ``ref``/``desc`` that collapses "clear it" and "blank it" onto the one
        meaning the store already has for absence. ``model``/``language`` are
        re-validated as non-empty below — a blank one is a mistake, not a clear.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("dirs", "files", "fallback_models")
    @classmethod
    def _clean_globs(cls, v: list[str] | None) -> list[str] | None:
        """Drop empty/whitespace-only entries — a blank glob/model id matches nothing."""
        if v is None:
            return None
        return [item.strip() for item in v if item and item.strip()]

    def changes(self) -> dict[str, Any]:
        """The fields the client EXPLICITLY sent, by python name.

        Reads ``model_fields_set`` rather than filtering on ``is not None`` so an
        explicit null (clear the pinned ref) is preserved and an omitted field is
        genuinely absent. ``model``/``language`` that validated down to ``None``
        (a blank string) are dropped — they have no null meaning.
        """
        sent = {name: getattr(self, name) for name in self.model_fields_set}
        for name in ("model", "language"):
            if name in sent and sent[name] is None:
                del sent[name]
        return sent


# ---------------------------------------------------------------------------
# WikiProjectSettings — the atomic façade
# ---------------------------------------------------------------------------


class WikiProjectSettings:
    """Owns the settings read/validate/write orchestration over its injected store.

    One instance per request (built by the route adapters). Collaborators are
    fields; every rule — the effective-settings ladder, the dev-mode re-gate, the
    repo-identity guard, the catalog branch, the credential status — is a method,
    so there is no module-global wiring and a test can drive the class directly
    without a Flask app.
    """

    # Settings that only take effect when the project is next INDEXED. A catalog
    # (non-git) project has no indexing pipeline, so none of these are applicable
    # to one — that is the 410 branch in :meth:`patch`. Keys are python names (the
    # ``ProjectSettingsPatch`` attribute names ``changes()`` returns).
    INDEX_TIME_FIELDS: frozenset[str] = frozenset(
        {
            "model",
            "ref",
            "depth",
            "language",
            "filter_mode",
            "dirs",
            "files",
            "graph_only",
            "repo_url",
            "platform",
            "fallback_models",
        }
    )

    # python name → wire name. The DTO is camelCase throughout (it feeds the same
    # console that reads ``Project``/``WizardSubmission``), so the ``editable`` map
    # and the ``fields`` of an error envelope must be keyed the way the client
    # actually names them — a snake_case key there is a field the FE can't find.
    WIRE_NAMES: dict[str, str] = {
        "filter_mode": "filterMode",
        "graph_only": "graphOnly",
        "repo_url": "repoUrl",
        "fallback_models": "fallbackModels",
    }

    @classmethod
    def _wire(cls, field: str) -> str:
        """The camelCase wire name for a python field name."""
        return cls.WIRE_NAMES.get(field, field)

    def __init__(
        self,
        store: WikiStoreBase,
        *,
        developer_mode: Callable[[], bool] | None = None,
    ) -> None:
        """Bind the wiki store and the developer-mode predicate.

        *developer_mode* is injected so the privileged ``graph_only`` re-gate is
        testable without mutating global config; it defaults to the same
        ``runtime.developer_mode`` config read the ``POST /index`` route gates on.
        """
        self._store = store
        self._developer_mode = developer_mode or _runtime_developer_mode

    # ── reads ───────────────────────────────────────────────────────────────

    def read(self, slug: str) -> dict[str, Any]:
        """The ``GET /v1/wiki/projects/<slug>/settings`` DTO.

        Raises :class:`WikiHTTPError` 404 for an unknown project. A catalog
        (non-git) project returns the reduced ``kind: "catalog"`` shape — it has
        no clone, no ref, no scope filters and no index model to speak of.
        """
        project = self._require_project(slug)
        credential = self._credential_status(slug)
        if self.is_catalog(slug, project.repo_url):
            return {
                "slug": slug,
                "kind": "catalog",
                "desc": project.desc,
                "credential": credential,
                "editable": self._editable_flags(catalog=True),
            }
        settings = self._effective(slug)
        return {
            "slug": slug,
            "kind": "git",
            "repoUrl": settings.repo_url,
            "platform": settings.platform,
            "model": settings.model,
            "ref": settings.ref,
            "depth": settings.depth,
            "language": settings.language,
            "filterMode": settings.filter_mode,
            "dirs": list(settings.dirs),
            "files": list(settings.files),
            "graphOnly": settings.graph_only,
            "fallbackModels": settings.fallback_models,
            # The override if one is set, else what the project is actually
            # displaying today — so the edit form is never blank on a project
            # whose description came from the platform fetch.
            "desc": settings.desc or project.desc,
            "credential": credential,
            "editable": self._editable_flags(catalog=False),
        }

    # ── writes ──────────────────────────────────────────────────────────────

    def patch(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        """Validate + apply a partial settings update; return the fresh GET DTO.

        Raises :class:`WikiHTTPError`: 400 (unknown/invalid field), 403 (graph-only
        without developer mode), 404 (unknown project), 409 (a repo edit that would
        re-point the slug at a different repository), 410 (an index-time field on a
        catalog project).

        Does NOT start a re-index. Everything but ``desc`` lands on the settings
        record and takes effect the next time the project is indexed; ``desc`` is
        display-only and is written straight through to the ``Project`` snapshot as
        well, so the console doesn't show a stale description until then.
        """
        project = self._require_project(slug)
        patch = self._parse(body)
        changes = patch.changes()
        if not changes:
            return self.read(slug)

        catalog = self.is_catalog(slug, project.repo_url)
        self._guard_catalog(changes, catalog=catalog)
        self._guard_developer_mode(changes)

        if catalog:
            # Only ``desc`` survived the catalog guard — no settings record to
            # write (a catalog project has no indexing contract to configure).
            self._apply_desc(slug, changes)
            return self.read(slug)

        current = self._effective(slug)
        self._guard_identity(current, changes)

        # Every ProjectSettingsPatch field maps 1:1 onto ProjectSettings (including
        # ``fallback_models`` — same name/type on both, a pinned cross-package
        # contract; see ``mewbo_graph.wiki.types.ProjectSettings.fallback_models``),
        # so the explicit-changes dict IS the update. A blank ``desc`` arrives as
        # None and therefore CLEARS the override — the next index re-fetches the
        # repo's own description instead of pinning an empty edit forever.
        updated = current.model_copy(update={**changes, "updated_at": _utc_now()})
        self._store.save_project_settings(slug, updated)
        self._apply_desc(slug, changes)
        # Loguru formats with ``{}``, not ``%s`` — a printf-style call here logs the
        # literal placeholders and silently drops the slug.
        logging.info("wiki settings updated slug={} fields={}", slug, sorted(changes))
        return self.read(slug)

    # ── the effective-settings ladder ───────────────────────────────────────

    def _effective(self, slug: str) -> ProjectSettings:
        """The settings a re-index of *slug* would actually run with.

        The SAME ladder ``WikiIndexingJob.refresh`` walks, so what the UI shows and
        what a refresh does can never disagree:

        1. the slug-keyed :class:`ProjectSettings` record (the PATCH target);
        2. the newest per-job submission sidecar (a project onboarded before that
           record existed — the first PATCH materialises a record from it);
        3. the ``Project`` record's own fields (nothing recorded how it was indexed).
        """
        settings = self._store.get_project_settings(slug)
        if settings is not None:
            return settings
        submission = self._legacy_submission(slug)
        return ProjectSettings.from_submission(submission)

    def _legacy_submission(self, slug: str) -> WizardSubmission:
        """Tiers 2 and 3 of the ladder, for a project with no settings record."""
        submission = _latest_job_submission(self._store, slug)
        if submission is not None:
            return submission
        return submission_from_project(self._require_project(slug))

    # ── guards ──────────────────────────────────────────────────────────────

    def _parse(self, body: dict[str, Any]) -> ProjectSettingsPatch:
        """Validate the request body; a bad shape is a 400 at the boundary."""
        if not isinstance(body, dict):
            raise WikiHTTPError(
                WikiError(code="validation", message="request body must be a JSON object")
            )
        try:
            return ProjectSettingsPatch.model_validate(body)
        except Exception as exc:
            raise WikiHTTPError(
                WikiError(
                    code="validation",
                    message=str(exc),
                    fields=_pydantic_fields(exc) or None,
                )
            ) from exc

    def _guard_developer_mode(self, changes: dict[str, Any]) -> None:
        """Re-gate ``graph_only`` on ``runtime.developer_mode`` — 403 otherwise.

        THE privilege check, and the reason this route can't just write the record
        blind. Graph-only mode is gated at ``POST /v1/wiki/index`` only, and the
        mode is deliberately STICKY thereafter (refresh replays the stored value
        without re-gating, so a project's mode isn't flipped mid-life). That means
        a PATCH which wrote ``graph_only=True`` into the settings record WITHOUT
        this check would hand an unprivileged caller the no-docs/zero-LLM path the
        index route refuses them — the escalation is real, and this is the seam
        that closes it.

        Turning graph-only OFF is not privileged: it only restores documentation.
        """
        if changes.get("graph_only") and not self._developer_mode():
            raise WikiHTTPError(
                WikiError(
                    code="forbidden",
                    message=(
                        "graph-only (developer) indexing is disabled on this "
                        "instance — enable runtime.developer_mode to use it"
                    ),
                    fields={"graphOnly": "requires developer mode"},
                ),
                status=403,
            )

    def _guard_catalog(self, changes: dict[str, Any], *, catalog: bool) -> None:
        """A catalog (non-git) project has no indexing contract to configure — 410.

        Its content arrives through ``POST .../documents``, not a clone/scan/graph
        run, so a ref, a scope filter or an index model would be settings that
        nothing ever reads. ``desc`` stays editable (it is pure display). Mirrors
        the branch ``refresh_project`` already takes via ``_has_git_submission``.
        """
        if not catalog:
            return
        offending = sorted(self._wire(f) for f in set(changes) & self.INDEX_TIME_FIELDS)
        if offending:
            raise WikiHTTPError(
                WikiError(
                    code="validation",
                    message=(
                        "this is a catalog (non-git) project — "
                        f"{', '.join(offending)} apply only to a git index. "
                        "Re-ingest its documents via "
                        "POST /v1/wiki/projects/<slug>/documents."
                    ),
                    fields={field: "not applicable to a catalog project" for field in offending},
                ),
                status=410,
            )

    def _guard_identity(self, current: ProjectSettings, changes: dict[str, Any]) -> None:
        """Reject a repo edit that re-points the slug at a DIFFERENT repository — 409.

        The slug is the canonical identity: pages, the code graph, jobs, credentials
        and the freshness baseline are all keyed by it. A bare URL swap would leave
        every one of them pinned to the old repo while the next index cloned a new
        one — silent, and unrecoverable without a delete.

        Re-NORMALISATION of the same repo is fine, and falls out for free:
        ``CredentialScope.from_repo_url`` lowercases the host, strips ``.git`` and a
        trailing slash, and preserves owner/repo case — so ``http://Git.Example.Com/o/r.git/``
        and ``https://git.example.com/o/r`` compare EQUAL. Anything that doesn't compare
        equal is a different (host, owner, repo), i.e. a different repo.

        ``platform`` alone is not identity — it names the software running at the
        host (which REST shape to speak), so correcting a mislabelled one is allowed.
        """
        new_url = changes.get("repo_url")
        if not new_url:
            return
        try:
            new_scope = CredentialScope.from_repo_url(new_url)
        except Exception as exc:
            raise WikiHTTPError(
                WikiError(
                    code="validation",
                    message=f"repoUrl is not a parseable git remote: {new_url!r}",
                    fields={"repoUrl": "unparseable"},
                )
            ) from exc
        current_scope = self._identity_of(current)
        if current_scope is not None and new_scope.value != current_scope.value:
            raise WikiHTTPError(
                WikiError(
                    code="validation",
                    message=(
                        f"repoUrl would re-point {current_scope.value!r} at "
                        f"{new_scope.value!r} — the slug is this project's identity "
                        "(pages, graph, credentials and freshness are all keyed by "
                        "it). Delete the project and onboard the new repository "
                        "instead."
                    ),
                    fields={"repoUrl": "would change repository identity"},
                ),
                status=409,
            )

    def _identity_of(self, settings: ProjectSettings) -> CredentialScope | None:
        """The (host, owner, repo) identity a project is currently pinned to.

        Prefers the configured ``repo_url`` and falls back to the slug — a legacy
        project may carry no URL while its slug still names the repo.
        """
        if settings.repo_url:
            try:
                return CredentialScope.from_repo_url(settings.repo_url)
            except Exception:
                pass
        return CredentialScope.coerce(settings.slug)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _require_project(self, slug: str) -> Project:
        """Return the Project for *slug*, or raise the 404 envelope."""
        project = self._store.get_project(slug)
        if project is None:
            raise WikiHTTPError(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        return project

    def is_catalog(self, slug: str, repo_url: str | None) -> bool:
        """True for a non-git catalog project (no clone URL, no git submission).

        THE one catalog test, shared with ``routes.refresh_project`` (which must
        reject a catalog project before the git pipeline synthesizes a bogus
        ``git clone <slug>``): a Project whose record merely omits ``repo_url`` is
        still git-backed if any stored submission carries a real clone URL, so both
        signals are consulted before calling it a catalog. A read hiccup resolves to
        "not a catalog" — never block a refresh on a store glitch; the git pipeline
        surfaces its own error if the URL is genuinely missing.
        """
        if repo_url:
            return False
        try:
            for job in self._store.list_jobs(slug=slug):
                sub = self._store.get_job_submission(job.job_id)
                if sub and str(sub.get("repoUrl") or "").strip():
                    return False
        except Exception:  # pragma: no cover — a read hiccup must not mislabel a project
            return False
        return True

    def _apply_desc(self, slug: str, changes: dict[str, Any]) -> None:
        """Write an edited description straight onto the Project snapshot too.

        ``desc`` is the one display field a user can edit, and ``Project`` is what
        the console renders — so without this the edit wouldn't show until the next
        index. The settings record holds the same value as the durable override, so
        the next rebuild re-applies it (``finalize._resolve_project_desc``) instead
        of overwriting it with the platform fetch. A CLEARED override (``None``) is
        deliberately NOT written through: the snapshot keeps its current text until
        the next index re-fetches the repo's own description.
        """
        desc = changes.get("desc")
        if "desc" in changes and desc:
            self._store.update_project(slug, {"desc": desc})

    def _credential_status(self, slug: str) -> dict[str, Any]:
        """Whether a git credential is on file for this project — never its value.

        Reuses the ONE durable-tier walk (``jobs._durable_credential_scope``: repo
        scope → host scope), so this agrees with the auth note the indexer renders.
        The scope is named so the console can deep-link the Security facet; the
        value never leaves the credential store.
        """
        scope = _durable_credential_scope(self._store, slug)
        if scope is None:
            return {"present": False, "scope": None, "scopeType": None}
        return {"present": True, "scope": scope.value, "scopeType": scope.kind}

    def _editable_flags(self, *, catalog: bool) -> dict[str, bool]:
        """Per-field editability (camelCase keys), so the console disables rather than guesses.

        ``graphOnly`` mirrors the server's developer-mode gate — the FE hides the
        switch instead of offering an edit that would come back 403. A catalog
        project can only edit ``desc``.
        """
        flags = {self._wire(field): not catalog for field in self.INDEX_TIME_FIELDS}
        flags["desc"] = True
        if not catalog:
            # The one field whose editability is a server-side privilege, not a
            # property of the project (see _guard_developer_mode). ``repoUrl``/
            # ``platform`` stay True: a same-repo re-normalisation is allowed, and
            # an identity change is refused with a 409 that says why.
            flags["graphOnly"] = self._developer_mode()
        return flags


# ---------------------------------------------------------------------------
# Module helpers (no state — the class owns the behaviour)
# ---------------------------------------------------------------------------


def _runtime_developer_mode() -> bool:
    """Read ``runtime.developer_mode`` — the same gate ``POST /v1/wiki/index`` uses."""
    try:
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        return bool(get_config_value("runtime", "developer_mode", default=False))
    except Exception:  # pragma: no cover — an unreadable config is not developer mode
        return False


def _pydantic_fields(exc: Exception) -> dict[str, str]:
    """Extract a field→message map from a Pydantic v2 ValidationError."""
    try:
        from pydantic import ValidationError  # noqa: PLC0415

        if isinstance(exc, ValidationError):
            fields: dict[str, str] = {}
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                fields[loc or "root"] = err.get("msg", "invalid")
            return fields
    except Exception:  # pragma: no cover
        pass
    return {}


def _utc_now() -> str:
    """ISO-8601 UTC timestamp, matching the format finalize stamps on Project."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["ProjectSettingsPatch", "WikiProjectSettings"]
