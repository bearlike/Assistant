"""``workspace_ref.key`` must name a project the platform can actually resolve.

A maintainer session wedged on every turn with ``Project 'app-89f9c9479143' is
not known.`` Two disjoint namespaces and nothing compared them: ``AppLifecycle``
persists a ``kind="shared"`` key verbatim as the agent session's ``project``
context field, while the resolver validates that field against
:class:`ProjectCatalog`. The session was broken the moment it was created — and
broken QUIETLY, because ``_resolve_session_cwd`` swallowed the refusal and fell
back to an empty scratch directory with nothing logged.

The fixtures mirror what the deployed store actually holds. The carrier of the
bad key is a DIFFERENT, newer app: building it, the model set the new app's
``workspace_ref`` to ``{kind:"shared", key:"<a neighbouring app's id>"}``,
reaching for another app's identity as if it were a workspace. The app whose id
was borrowed is fine and its own ref is ``{kind:"own", key:""}`` — which is also
the commonest live shape, hence the empty-key case below.

**Nothing here is pattern-based, and no production code may be either.** App ids
are model-supplied on the chat-builder path, so a live one reads
``llm-model-compare-upgraded`` — not ``app-<hex>``. The only question the check
asks is whether the catalog knows the key.

Same fixture discipline as ``test_apps_lifecycle.py``: real JSON stores, a real
``ProjectCatalog`` over an in-memory configured map, the session backend the one
faked I/O boundary, an injected fixed NOW. No network, no LLM.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from loguru import logger as loguru_logger
from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import AppFrontend, AppSpec, AppVersion, WorkspaceRef
from mewbo_api.apps.store import JsonAppStore
from mewbo_core.config import ProjectConfig
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore
from mewbo_core.workspaces.project_catalog import MANAGED_PREFIX, ProjectCatalog
from mewbo_core.workspaces.project_store import VirtualProject
from mewbo_core.workspaces.repositories import Repository

from tests.apps.test_apps_lifecycle import FakeSessions

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)

# The live shapes. ``CARRIER`` is the app whose manifest holds the bad key — a
# model-supplied id that is not ``app-<hex>`` at all, which is precisely why no
# check may be pattern-based. ``BAD_KEY`` is the neighbouring app's id it
# borrowed. ``GOOD_KEY`` is the shape a VALID shared key really has in the wild:
# a plain configured project name.
CARRIER = "llm-model-compare-upgraded"
BAD_KEY = "app-89f9c9479143"
GOOD_KEY = "Agents"


@contextmanager
def _capture_loguru(level: str = "WARNING"):
    """Capture loguru records emitted inside the block.

    These modules log through loguru (``mewbo_core.common.get_logger``), which
    pytest's ``caplog`` never sees — a temporary sink is loguru's own documented
    way to assert on an emitted message (tests/CLAUDE.md).
    """
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda msg: messages.append(str(msg)), level=level)
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


def _catalog(tmp_path, *, project_store=None, repository_store=None) -> ProjectCatalog:
    """A catalog knowing exactly one real, on-disk configured project: ``Agents``."""
    project_dir = tmp_path / GOOD_KEY
    project_dir.mkdir()
    return ProjectCatalog(
        configured={GOOD_KEY: ProjectConfig(path=str(project_dir))},
        project_store=project_store,
        repository_store=repository_store,
    )


class _FakeProjectStore:
    """The one method :meth:`ProjectCatalog._managed_entries` calls."""

    def __init__(self, *projects: VirtualProject) -> None:
        self._projects = list(projects)

    def list_projects(self) -> list[VirtualProject]:
        return list(self._projects)


class _FakeRepositoryStore:
    """The one method :meth:`ProjectCatalog._repository_entries` calls."""

    def __init__(self, *repositories: Repository) -> None:
        self._repositories = list(repositories)

    def list(self) -> list[Repository]:
        return list(self._repositories)


def _managed(path: str, *, is_worktree: bool = False) -> VirtualProject:
    """A managed project row pointing at *path* (which need not exist)."""
    return VirtualProject(
        project_id="p1",
        name="Managed",
        description="",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        path=path,
        is_worktree=is_worktree,
        branch="feature" if is_worktree else None,
    )


def _make(tmp_path, *, catalog: ProjectCatalog | None):
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    sessions = FakeSessions()
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=JsonTriggerStore(data_file=tmp_path / "triggers.json"),
        trigger_policy=TriggerPolicy(),
        sessions=sessions,
        now_fn=lambda: NOW,
        project_catalog=catalog,
    )
    return lifecycle, app_store, sessions


def _draft(app_id: str, *, builder_sid: str, workspace_ref: WorkspaceRef) -> AppSpec:
    return AppSpec(
        app_id=app_id,
        title="Inbox digest",
        summary="Groups email into tasks.",
        owner_session_id=builder_sid,
        workspace_ref=workspace_ref,
        frontend=AppFrontend(entrypoint="app.py", files={"app.py": "import streamlit as st\n"}),
        status="building",
        created_at=NOW,
        updated_at=NOW,
    )


class TestSubmitValidatesWorkspaceRef:
    def test_an_unresolvable_shared_key_is_refused_naming_what_is_available(self, tmp_path):
        """The regression, in the live shape: a NEW app borrowing a neighbour's app id."""
        lifecycle, _, _ = _make(tmp_path, catalog=_catalog(tmp_path))
        draft = _draft(
            CARRIER,
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=BAD_KEY),
        )
        with pytest.raises(ValueError) as excinfo:
            lifecycle.submit(draft, builder_session_id="builder-1")
        message = str(excinfo.value)
        assert BAD_KEY in message
        # The catalog's OWN available-list rides the refusal, so the reask is
        # correctable in one turn rather than a mute denial.
        assert GOOD_KEY in message

    def test_the_refusal_leaves_no_state(self, tmp_path):
        """Submit's ordering contract: a refusal persists nothing and mints nothing."""
        lifecycle, app_store, sessions = _make(tmp_path, catalog=_catalog(tmp_path))
        draft = _draft(
            CARRIER,
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=BAD_KEY),
        )
        with pytest.raises(ValueError):
            lifecycle.submit(draft, builder_session_id="builder-1")
        assert app_store.get(CARRIER) is None
        assert app_store.list_versions(CARRIER) == []
        # No maintainer session was minted, tagged or given a context event.
        assert sessions.tags == {}
        assert sessions.contexts == {}

    def test_an_arbitrary_unknown_key_is_refused_too(self, tmp_path):
        """The check is catalog MEMBERSHIP, never a pattern.

        App ids are model-supplied, so one can look like anything; the refusal
        must not depend on a key resembling an id.
        """
        lifecycle, _, _ = _make(tmp_path, catalog=_catalog(tmp_path))
        draft = _draft(
            "app-y",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key="Agent Workspace"),
        )
        with pytest.raises(ValueError, match="Agent Workspace"):
            lifecycle.submit(draft, builder_session_id="builder-1")

    def test_a_valid_shared_key_still_submits_and_stamps_the_project(self, tmp_path):
        """Non-regression, live shape: a plain configured project name still works."""
        lifecycle, app_store, sessions = _make(tmp_path, catalog=_catalog(tmp_path))
        draft = _draft(
            "app-ok",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=GOOD_KEY),
        )
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get("app-ok") is not None
        maintainer = live.maintainer_session_id
        assert maintainer is not None
        context = sessions.contexts[maintainer][0]
        assert context["project"] == GOOD_KEY

    @pytest.mark.parametrize("own_key", ["", BAD_KEY])
    def test_kind_own_is_never_checked(self, tmp_path, own_key):
        """``own`` writes no ``project``, so its key is not a project key at all.

        ``key=""`` is the commonest shape in the live store, and it is the case
        that catches an implementation testing ``key.strip()`` BEFORE ``kind``:
        such a check would let an own-with-a-key app through and refuse nothing
        of the empty case, i.e. pass a naive test while branching on the wrong
        field.
        """
        lifecycle, app_store, sessions = _make(tmp_path, catalog=_catalog(tmp_path))
        draft = _draft(
            "app-own",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="own", key=own_key),
        )
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get("app-own") is not None
        maintainer = live.maintainer_session_id
        assert "project" not in sessions.contexts[maintainer][0]

    def test_an_unwired_catalog_degrades_with_a_warning(self, tmp_path):
        """``None`` means unwired: the check is skipped, loudly — never a crash."""
        lifecycle, app_store, _ = _make(tmp_path, catalog=None)
        draft = _draft(
            "app-unwired",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=BAD_KEY),
        )
        with _capture_loguru() as messages:
            live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get("app-unwired") is not None
        assert any("No project catalog wired" in m for m in messages)


class TestBoundResubmitRepairsLegacyWorkspace:
    def test_a_bound_resubmit_replaces_a_legacy_bad_key_and_fresh_sessions_use_it(
        self, tmp_path
    ):
        lifecycle, app_store, sessions = _make(tmp_path, catalog=_catalog(tmp_path))
        legacy = _draft(
            CARRIER,
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=BAD_KEY),
        ).model_copy(
            update={"status": "live", "maintainer_session_id": "maintainer-1", "version": 1}
        )
        app_store.save(legacy)
        app_store.save_version(AppVersion(app_id=CARRIER, version=1, spec=legacy, author="builder"))

        repaired = lifecycle.submit(
            _draft(
                CARRIER,
                builder_sid="maintainer-1",
                workspace_ref=WorkspaceRef(kind="own", key=""),
            ),
            builder_session_id="maintainer-1",
        )

        assert repaired.version == 2
        assert repaired.workspace_ref == WorkspaceRef(kind="own", key="")
        assert app_store.get(CARRIER).workspace_ref == WorkspaceRef(kind="own", key="")
        # Historical snapshots retain their original contract and stay readable.
        assert app_store.get_version(CARRIER, 1).spec.workspace_ref.key == BAD_KEY

        session_id, created = lifecycle.get_or_create_maintainer_session(CARRIER, fresh=True)
        assert created is True
        assert "project" not in sessions.contexts[session_id][0]


class TestSubmitMirrorsTheRuntimeRecovery:
    """The validator must predict ``_resolve_project_cwd``, not ``catalog.resolve``.

    That runtime recovers from exactly one refusal: an ``unavailable`` MANAGED
    project or worktree is a directory Mewbo owns, so it creates it and
    succeeds. A validator refusing there would be STRICTER than the thing it
    predicts and would block a legitimate app. Every other refusal stands.
    """

    def test_a_managed_project_whose_directory_is_missing_is_accepted(self, tmp_path):
        missing = str(tmp_path / "not-created-yet")
        catalog = _catalog(tmp_path, project_store=_FakeProjectStore(_managed(missing)))
        lifecycle, app_store, sessions = _make(tmp_path, catalog=catalog)
        key = f"{MANAGED_PREFIX}p1"
        draft = _draft(
            "app-managed",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=key),
        )
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get("app-managed") is not None
        assert sessions.contexts[live.maintainer_session_id][0]["project"] == key
        # The validator mirrors the DECISION, never the repair: the runtime is
        # what makes the directory, so nothing was created here.
        assert not (tmp_path / "not-created-yet").exists()

    def test_a_worktree_whose_directory_is_missing_is_accepted(self, tmp_path):
        missing = str(tmp_path / "wt-not-created")
        catalog = _catalog(
            tmp_path, project_store=_FakeProjectStore(_managed(missing, is_worktree=True))
        )
        lifecycle, _, _ = _make(tmp_path, catalog=catalog)
        draft = _draft(
            "app-wt",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=f"{MANAGED_PREFIX}p1"),
        )
        assert lifecycle.submit(draft, builder_session_id="builder-1").status == "live"

    def test_a_configured_project_whose_directory_is_missing_is_still_refused(self, tmp_path):
        """The asymmetry is the point: an operator's path is not Mewbo's to create."""
        catalog = ProjectCatalog(
            configured={
                GOOD_KEY: ProjectConfig(path=str(tmp_path / GOOD_KEY)),
                "Gone": ProjectConfig(path=str(tmp_path / "gone")),
            }
        )
        (tmp_path / GOOD_KEY).mkdir()
        lifecycle, app_store, _ = _make(tmp_path, catalog=catalog)
        draft = _draft(
            "app-gone",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key="Gone"),
        )
        with pytest.raises(ValueError, match="Gone"):
            lifecycle.submit(draft, builder_session_id="builder-1")
        assert app_store.get("app-gone") is None

    def test_a_repository_with_no_checkout_is_still_refused(self, tmp_path):
        """``no_checkout`` is not the recovered code — there is no directory to make."""
        catalog = _catalog(
            tmp_path,
            repository_store=_FakeRepositoryStore(Repository(slug="git.example.com/acme/beacon")),
        )
        lifecycle, app_store, _ = _make(tmp_path, catalog=catalog)
        draft = _draft(
            "app-repo",
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key="git.example.com/acme/beacon"),
        )
        with pytest.raises(ValueError, match="beacon"):
            lifecycle.submit(draft, builder_session_id="builder-1")
        assert app_store.get("app-repo") is None


class TestStoredSnapshotsKeepParsing:
    def test_a_stored_spec_holding_a_bad_key_round_trips(self, tmp_path):
        """Why this is a submit-boundary METHOD and not a ``model_validator``.

        The app store is append-only, so a snapshot written before the check
        existed still holds a bad key. A parse-time floor would 500 every detail
        read and every rollback of an affected app. This test fails the moment
        someone "simplifies" the check onto :class:`WorkspaceRef`.

        The row built here is the one the deployed store really holds: the
        carrier app, live, bound to a neighbouring app's id.
        """
        store = JsonAppStore(root_dir=tmp_path / "apps")
        spec = _draft(
            CARRIER,
            builder_sid="builder-1",
            workspace_ref=WorkspaceRef(kind="shared", key=BAD_KEY),
        )
        store.save(spec)
        store.save_version(AppVersion(app_id=CARRIER, version=1, spec=spec, author="builder"))

        read_back = store.get(CARRIER)
        assert read_back is not None
        assert read_back.workspace_ref.key == BAD_KEY
        version = store.get_version(CARRIER, 1)
        assert version is not None
        assert version.spec.workspace_ref.key == BAD_KEY


class TestSwallowedRefusalIsLogged:
    def test_resolve_session_cwd_logs_and_still_returns_none(self, tmp_path, monkeypatch):
        """The QUIET half: the run never failed, it just ran in an empty scratch dir.

        Control flow is unchanged (``None`` ⇒ the caller's documented
        fall-through to the session temp dir); what changes is that the refusal
        is now readable.
        """
        import mewbo_api.backend as backend
        from mewbo_core.loop.session_runtime import SessionRuntime
        from mewbo_core.session.session_store import SessionStore

        store = SessionStore(root_dir=str(tmp_path / "sessions"))
        monkeypatch.setattr(backend, "runtime", SessionRuntime(session_store=store), raising=False)
        session_id = "sess-wedged"
        store.append_event(
            session_id, {"type": "context", "payload": {"project": BAD_KEY}}
        )

        with _capture_loguru() as messages:
            resolved = backend._resolve_session_cwd(session_id)

        assert resolved is None
        assert any(BAD_KEY in m and session_id in m for m in messages)
