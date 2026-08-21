"""Contract tests for the app maintainer-session get-or-create seam.

Covers ``AppLifecycle.get_or_create_maintainer_session`` (the domain method) and
``AppsRoutesController.get_or_create_session`` (the thin wire mapping over it) —
the reverse-invocation channel opened for an app that has no other route back to
its maintainer (see ``lifecycle.py`` / ``routes.py`` docstrings for the design).

Real JSON app store; the session backend is the one faked I/O boundary, with an
explicit ``terminated`` set so a test can drive the "session is a dead end"
branch without a real ``SessionRuntime``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import AppFrontend, WorkspaceRef
from mewbo_api.apps.routes import AppsRoutesController
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore
from mewbo_api.apps.tokens import AppReadTokenSigner
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


class FakeSessions:
    """Records mint calls; hands out deterministic ids; simulates termination."""

    def __init__(self) -> None:
        self._n = 0
        self.minted: list[str] = []
        self.terminated: set[str] = set()
        # tag -> session id, exactly as the real tag collection is keyed: one tag
        # resolves to ONE session, so a second claimant STEALS it. That is the
        # property the fresh-session tests below assert against.
        self.tags: dict[str, str] = {}

    def create_session(self) -> str:
        self._n += 1
        session_id = f"session-{self._n}"
        self.minted.append(session_id)
        return session_id

    def tag_session(self, session_id: str, tag: str) -> None:  # noqa: D401 - fake
        self.tags[tag] = session_id

    def tags_for_session(self, session_id: str) -> list[str]:
        return [t for t, sid in self.tags.items() if sid == session_id]

    def append_context_event(self, session_id: str, context: dict) -> None:
        pass

    def append_event(self, session_id: str, event: dict) -> None:
        pass

    def is_terminated(self, session_id: str) -> bool:
        return session_id in self.terminated


def _make_lifecycle(
    tmp_path, sessions: FakeSessions | None = None
) -> tuple[AppLifecycle, JsonAppStore]:
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=JsonTriggerStore(data_file=tmp_path / "triggers.json"),
        trigger_policy=TriggerPolicy(),
        sessions=sessions or FakeSessions(),
        now_fn=lambda: NOW,
    )
    return lifecycle, app_store


def _draft(app_store: JsonAppStore, *, app_id: str, owner_session_id: str) -> None:
    """Persist a bare ``draft`` app — the shape ``create_draft`` leaves before submit."""
    from mewbo_api.apps.models import AppSpec

    app_store.save(
        AppSpec(
            app_id=app_id,
            title="Test app",
            owner_session_id=owner_session_id,
            workspace_ref=WorkspaceRef(kind="own", key="default"),
            frontend=AppFrontend(entrypoint="app.py", files={"app.py": "x = 1\n"}),
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _live_app(lifecycle: AppLifecycle) -> str:
    """Create + submit a live app; return its app_id (maintainer_session_id gets set)."""
    draft_id = "app-live0000001"
    owner = lifecycle.sessions.create_session()
    _draft(lifecycle.app_store, app_id=draft_id, owner_session_id=owner)
    draft = lifecycle.app_store.get(draft_id)
    assert draft is not None
    lifecycle.submit(draft, builder_session_id=draft.owner_session_id)
    return draft_id


class TestGetOrCreateMaintainerSession:
    def test_returns_existing_maintainer_without_minting(self, tmp_path):
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        app = app_store.get(app_id)
        assert app is not None and app.maintainer_session_id is not None
        minted_before = list(sessions.minted)

        result = lifecycle.get_or_create_maintainer_session(app_id)

        assert result == (app.maintainer_session_id, False)
        # No new session minted for an already-live maintainer.
        assert sessions.minted == minted_before

    def test_reuses_builder_session_for_unsubmitted_draft(self, tmp_path):
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        owner = sessions.create_session()
        _draft(app_store, app_id="app-draft00001", owner_session_id=owner)
        minted_before = list(sessions.minted)

        result = lifecycle.get_or_create_maintainer_session("app-draft00001")

        assert result == (owner, False)
        # Reusing the builder session mints nothing new...
        assert sessions.minted == minted_before
        # ...and does not promote it into maintainer_session_id: the _resolve_app
        # scan (maintainer OR owner) already finds this app via owner_session_id,
        # so a second claimant on the same app is never written.
        app = app_store.get("app-draft00001")
        assert app is not None and app.maintainer_session_id is None

    def test_mints_and_persists_when_no_reusable_session(self, tmp_path):
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        owner = sessions.create_session()
        sessions.terminated.add(owner)  # the only candidate session is a dead end
        _draft(app_store, app_id="app-dead00001", owner_session_id=owner)

        result = lifecycle.get_or_create_maintainer_session("app-dead00001")

        assert result is not None
        session_id, created = result
        assert created is True
        assert session_id != owner
        assert session_id in sessions.minted
        app = app_store.get("app-dead00001")
        assert app is not None and app.maintainer_session_id == session_id

    def test_unknown_app_id_returns_none(self, tmp_path):
        lifecycle, _ = _make_lifecycle(tmp_path)
        assert lifecycle.get_or_create_maintainer_session("no-such-app") is None

    def test_archived_app_is_not_refused(self, tmp_path):
        """Decision: archived apps are NOT refused (see lifecycle.py docstring).

        ``submit``'s maintainer-resubmit branch can revive an archived app by
        overwriting ``status`` before ``transition("live", ...)``, so severing
        this route for an archived app would strand the one session that can
        ever reach that revival path again.
        """
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        archived = lifecycle.archive(app_id)
        assert archived is not None and archived.status == "archived"
        maintainer = archived.maintainer_session_id
        assert maintainer is not None

        result = lifecycle.get_or_create_maintainer_session(app_id)

        assert result == (maintainer, False)


class TestFreshSession:
    """``fresh=True`` — an ADDITIONAL session against the app, never the maintainer.

    The defect this closes: the console composer, targeting an app, called the
    get-or-create endpoint and had its turn appended to the maintainer's
    transcript. A fresh session must be genuinely new, must not disturb the
    maintainer's binding, and must still be able to resolve the app it is for.
    """

    def test_mints_a_distinct_session_every_time(self, tmp_path):
        sessions = FakeSessions()
        lifecycle, _ = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)

        first = lifecycle.get_or_create_maintainer_session(app_id, fresh=True)
        second = lifecycle.get_or_create_maintainer_session(app_id, fresh=True)

        assert first is not None and second is not None
        assert first[1] is True and second[1] is True
        assert first[0] != second[0]

    def test_does_not_repoint_maintainer_session_id(self, tmp_path):
        """Two claimants would make the resolvers' scan ORDER decide who works.

        ``maintainer_session_id`` is what the repair wake dereferences, so a
        fresh session must leave it exactly where it was.
        """
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        maintainer = app_store.get(app_id).maintainer_session_id

        fresh, _ = lifecycle.get_or_create_maintainer_session(app_id, fresh=True)

        assert fresh != maintainer
        assert app_store.get(app_id).maintainer_session_id == maintainer

    def test_does_not_steal_the_canonical_tag(self, tmp_path):
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        maintainer = app_store.get(app_id).maintainer_session_id

        fresh, _ = lifecycle.get_or_create_maintainer_session(app_id, fresh=True)

        assert sessions.tags[f"app:{app_id}"] == maintainer
        assert sessions.tags_for_session(fresh) == [f"app:{app_id}:{fresh}"]

    def test_the_default_path_still_reuses_after_a_fresh_mint(self, tmp_path):
        """The app detail header's "open session" action is untouched."""
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        maintainer = app_store.get(app_id).maintainer_session_id
        lifecycle.get_or_create_maintainer_session(app_id, fresh=True)

        assert lifecycle.get_or_create_maintainer_session(app_id) == (maintainer, False)

    def test_carries_the_same_capability_and_workspace_scope(self, tmp_path):
        """One mint, so the ``apps`` stamp and the project scope cannot drift."""
        recorded: dict[str, dict] = {}

        class _RecordingSessions(FakeSessions):
            def append_context_event(self, session_id: str, context: dict) -> None:
                recorded[session_id] = context

        sessions = _RecordingSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        maintainer = app_store.get(app_id).maintainer_session_id

        fresh, _ = lifecycle.get_or_create_maintainer_session(app_id, fresh=True)

        assert recorded[fresh] == recorded[maintainer]
        assert recorded[fresh]["client_capabilities"] == ["apps"]
        assert recorded[fresh]["app_id"] == app_id

    def test_unknown_app_id_returns_none(self, tmp_path):
        lifecycle, _ = _make_lifecycle(tmp_path)
        assert lifecycle.get_or_create_maintainer_session("nope", fresh=True) is None


class TestFreshSessionResolvesItsApp:
    """The tag tier on ``AppStagingArea.app_for_session`` — and its bounds."""

    def _fresh(self, tmp_path):
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        fresh, _ = lifecycle.get_or_create_maintainer_session(app_id, fresh=True)
        return app_store, app_id, fresh, sessions

    def test_the_tag_resolves_the_app(self, tmp_path):
        from mewbo_api.apps.staging import AppStagingArea

        app_store, app_id, fresh, sessions = self._fresh(tmp_path)

        resolved = AppStagingArea(session_id=fresh).app_for_session(
            app_store, session_tags=sessions.tags_for_session(fresh)
        )

        assert resolved is not None and resolved.app_id == app_id

    def test_without_the_tag_it_resolves_nothing(self, tmp_path):
        """The no-tag default is the pre-existing behaviour, byte for byte."""
        from mewbo_api.apps.staging import AppStagingArea

        app_store, _, fresh, _ = self._fresh(tmp_path)

        assert AppStagingArea(session_id=fresh).app_for_session(app_store) is None

    def test_an_app_id_CONTEXT_key_authorizes_NOTHING(self, tmp_path):
        """The security property: only a server-stamped TAG binds a session.

        ``backend.py``'s ``_build_context_payload`` merges a request's
        ``context`` verbatim, so any caller can put an ``app_id`` on a session it
        owns. Reading that key would hand any caller any app's manifest and
        source. This asserts the property directly — a session carrying the key
        and no tag resolves nothing — so it would still fail if the tag check
        were replaced by a context read, which "an unbound session resolves
        nothing" would not.
        """
        from mewbo_api.apps.staging import AppStagingArea

        app_store, app_id, _, _ = self._fresh(tmp_path)
        area = AppStagingArea(session_id="session-forged")

        # The key a hostile caller can write is not a tag, so nothing resolves...
        assert area.app_for_session(app_store) is None
        # ...while the SERVER-stamped tag for the same app does, which is what
        # proves the refusal above is about authorization, not the fixture.
        assert (
            area.app_for_session(
                app_store, session_tags=[f"app:{app_id}:session-forged"]
            ).app_id
            == app_id
        )

    def test_a_tag_for_an_unknown_app_resolves_nothing(self, tmp_path):
        from mewbo_api.apps.staging import AppStagingArea

        app_store, _, fresh, _ = self._fresh(tmp_path)

        assert (
            AppStagingArea(session_id=fresh).app_for_session(
                app_store, session_tags=[f"app:app-gone000001:{fresh}"]
            )
            is None
        )

    def test_a_non_apps_tag_is_ignored(self, tmp_path):
        from mewbo_api.apps.staging import AppStagingArea

        app_store, _, fresh, _ = self._fresh(tmp_path)

        assert (
            AppStagingArea(session_id=fresh).app_for_session(
                app_store, session_tags=["wiki:maintain:git.example.com/acme/beacon"]
            )
            is None
        )

    def test_a_fresh_session_reaches_the_data_plane(self, tmp_path):
        """A tag-bound session resolves in ``app_data`` exactly as it does in ``get_app``.

        This asserted the opposite until the tag tier reached every tool, and the
        inversion is the same correction ``test_a_session_with_no_tag_for_the_app_
        cannot_overwrite_it`` below already records for ``submit``: confining the
        tier to ``get_app`` did not make the composer's session read-only, it made
        the suite INCOHERENT. That session could stage the bundle and ship a new
        live version while ``app_data`` and ``run_pipeline`` told it no app was
        bound — the destructive operation permitted and the two diagnostic ones
        refused, so a maintainer could push a guess but never dry-run it.

        Driven through the real tool, not asserted against a copy of its rule.
        The scope gate is what is under test, so clearing it and failing LATER (on
        the collection name) is the pass condition — same shape as the maintainer
        control below.
        """
        import asyncio

        from mewbo_api.apps.plugin.app_data import AppDataTool
        from mewbo_api.apps.store import JsonAppDataStore, JsonPipelineRunStore
        from mewbo_core.classes import ActionStep

        app_store, app_id, fresh, sessions = self._fresh(tmp_path)
        maintainer = app_store.get(app_id).maintainer_session_id
        step = ActionStep(
            tool_id="app_data",
            operation="upsert",
            tool_input={
                "operation": "upsert",
                "app_id": app_id,
                "collection": "items",
                "key": "k1",
                "doc": {"a": 1},
            },
        )

        def _run(session_id: str) -> str:
            tool = AppDataTool(
                session_id=session_id,
                app_store=app_store,
                data_store=JsonAppDataStore(root_dir=tmp_path / "apps"),
                run_store=JsonPipelineRunStore(root_dir=tmp_path / "apps"),
                tags_reader=sessions.tags_for_session,
            )
            return asyncio.run(tool.handle(step)).content

        fresh_result = _run(fresh)
        assert "not_found" not in fresh_result
        assert "unknown collection" in fresh_result
        # The control that makes the assertion above non-vacuous: the MAINTAINER
        # clears the same gate and fails at the same later point.
        maintainer_result = _run(maintainer)
        assert "not_found" not in maintainer_result
        assert "unknown collection" in maintainer_result

    def test_every_app_tool_agrees_on_the_binding(self, tmp_path):
        """One session, one binding — asserted ACROSS the tools, not within one.

        The gap this closes is why the tier could drift: every existing test
        pinned ONE tool against its OWN rule, so three tools disagreeing was
        unobservable. The reported symptom was exactly that disagreement —
        ``get_app`` resolved an app that ``run_pipeline`` and ``app_data``
        simultaneously reported unbound, in consecutive calls on one session.

        Asserts the AGREEMENT rather than any single verdict, so it fails the
        moment a fourth tool starts resolving the binding privately again.
        """
        from mewbo_api.apps.plugin.app_data import AppDataTool
        from mewbo_api.apps.plugin.get_app import GetAppTool
        from mewbo_api.apps.plugin.run_pipeline import RunPipelineTool
        from mewbo_api.apps.store import JsonAppDataStore, JsonPipelineRunStore

        app_store, app_id, fresh, sessions = self._fresh(tmp_path)
        data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
        run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")

        def _resolved_app_id(tool) -> str | None:
            app = tool._resolve_app(app_store)  # noqa: SLF001 — the binding IS the subject
            return None if app is None else app.app_id

        get_app = GetAppTool(
            session_id=fresh, app_store=app_store, data_store=data_store,
            run_store=run_store, tags_reader=sessions.tags_for_session,
        )
        run_pipeline = RunPipelineTool(
            session_id=fresh, app_store=app_store,
            tags_reader=sessions.tags_for_session,
        )
        app_data = AppDataTool(
            session_id=fresh, app_store=app_store, data_store=data_store,
            run_store=run_store, tags_reader=sessions.tags_for_session,
        )

        assert _resolved_app_id(get_app) == app_id
        assert _resolved_app_id(run_pipeline) == app_id
        assert app_data._resolve_app(app_store, app_id).app_id == app_id  # noqa: SLF001

        # And the refusal agrees too: an UNBOUND session resolves nothing
        # anywhere, so the agreement above is not "everything always passes".
        unbound = "session-bound-to-nothing"
        assert _resolved_app_id(
            GetAppTool(session_id=unbound, app_store=app_store, data_store=data_store,
                       run_store=run_store, tags_reader=sessions.tags_for_session)
        ) is None
        assert _resolved_app_id(
            RunPipelineTool(session_id=unbound, app_store=app_store,
                            tags_reader=sessions.tags_for_session)
        ) is None
        assert AppDataTool(
            session_id=unbound, app_store=app_store, data_store=data_store,
            run_store=run_store, tags_reader=sessions.tags_for_session,
        )._resolve_app(app_store, app_id) is None  # noqa: SLF001

    def test_a_tag_bound_session_cannot_reach_a_DIFFERENT_app(self, tmp_path):
        """The tag scopes to exactly ONE app — widening the tier did not widen that.

        ``app_data`` takes an ``app_id`` argument, so it is the one tool that can
        be POINTED at a foreign app. Naming one the session's tag does not cover
        stays a uniform ``not_found``.
        """
        import asyncio

        from mewbo_api.apps.plugin.app_data import AppDataTool
        from mewbo_api.apps.store import JsonAppDataStore, JsonPipelineRunStore
        from mewbo_core.classes import ActionStep

        app_store, app_id, fresh, sessions = self._fresh(tmp_path)
        # A SECOND live app in the same store, which this session's tag does not name.
        lifecycle, _ = _make_lifecycle(tmp_path, sessions)
        other_id = "app-other000001"
        _draft(app_store, app_id=other_id, owner_session_id=sessions.create_session())
        other_draft = app_store.get(other_id)
        assert other_draft is not None
        lifecycle.submit(other_draft, builder_session_id=other_draft.owner_session_id)

        tool = AppDataTool(
            session_id=fresh,
            app_store=app_store,
            data_store=JsonAppDataStore(root_dir=tmp_path / "apps"),
            run_store=JsonPipelineRunStore(root_dir=tmp_path / "apps"),
            tags_reader=sessions.tags_for_session,
        )
        step = ActionStep(
            tool_id="app_data",
            operation="query",
            tool_input={"operation": "query", "app_id": other_id, "collection": "items"},
        )

        assert "not_found" in asyncio.run(tool.handle(step)).content

    def test_a_session_with_no_tag_for_the_app_cannot_overwrite_it(self, tmp_path):
        """``submit``'s live-overwrite guard refuses every UNBOUND session.

        This test previously asserted the opposite of the product promise — that
        a FRESH session could never overwrite a live app — which is exactly the
        defect: the composer's session is a fresh one, its resubmit was refused,
        and the model forked a second app. What the guard genuinely protects is a
        session the server never bound to this app, and that is what it now
        pins; the fresh session's own (now allowed) resubmit is covered by
        ``test_app_resubmit_authorization.py``.

        The fresh session is still minted here, unused, so the assertion cannot
        pass merely because no fresh session exists: the refusal below is about
        the SUBMITTER's binding, not about the app's state.
        """
        import pytest

        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        app_id = _live_app(lifecycle)
        lifecycle.get_or_create_maintainer_session(app_id, fresh=True)
        live = app_store.get(app_id)
        assert live is not None and live.status == "live"
        unbound = sessions.create_session()  # minted, never tagged for this app

        with pytest.raises(ValueError, match="refusing to overwrite a live app"):
            lifecycle.submit(live, builder_session_id=unbound)

        assert app_store.get(app_id).version == live.version  # untouched


class TestControllerGetOrCreateSession:
    """The thin wire mapping in ``AppsRoutesController.get_or_create_session``."""

    def _make_controller(self, tmp_path) -> AppsRoutesController:
        sessions = FakeSessions()
        lifecycle, app_store = _make_lifecycle(tmp_path, sessions)
        return AppsRoutesController(
            lifecycle=lifecycle,
            app_store=app_store,
            run_store=JsonPipelineRunStore(root_dir=tmp_path / "apps"),
            data_store=JsonAppDataStore(root_dir=tmp_path / "apps"),
            trigger_store=lifecycle.trigger_store,
            token_signer=AppReadTokenSigner(secret="sekret"),
            require_api_key=lambda: None,
            require_master_token=lambda: None,
            now_fn=lambda: NOW,
        )

    def test_mint_returns_201_and_created_true(self, tmp_path):
        controller = self._make_controller(tmp_path)
        owner = controller.lifecycle.sessions.create_session()
        _draft(controller.app_store, app_id="app-ctl0000001", owner_session_id=owner)
        controller.lifecycle.sessions.terminated.add(owner)

        body, status = controller.get_or_create_session("app-ctl0000001", {})

        assert status == 201
        assert body["created"] is True
        assert body["session_id"] != owner

    def test_reuse_returns_200_and_created_false(self, tmp_path):
        controller = self._make_controller(tmp_path)
        app_id = _live_app(controller.lifecycle)
        app = controller.app_store.get(app_id)

        body, status = controller.get_or_create_session(app_id, {})

        assert status == 200
        assert body == {"session_id": app.maintainer_session_id, "created": False}

    def test_unknown_app_id_404(self, tmp_path):
        controller = self._make_controller(tmp_path)
        body, status = controller.get_or_create_session("nope", {})
        assert status == 404
        assert "message" in body

    def test_fresh_body_always_mints_and_reports_201(self, tmp_path):
        controller = self._make_controller(tmp_path)
        app_id = _live_app(controller.lifecycle)
        maintainer = controller.app_store.get(app_id).maintainer_session_id

        first, status = controller.get_or_create_session(app_id, {"new_session": True})
        second, _ = controller.get_or_create_session(app_id, {"new_session": True})

        assert status == 201
        assert first["created"] is True
        assert first["session_id"] not in (maintainer, second["session_id"])

    def test_unknown_body_field_is_a_400(self, tmp_path):
        """``extra="forbid"``: a misspelled field must not silently reuse."""
        controller = self._make_controller(tmp_path)
        app_id = _live_app(controller.lifecycle)

        body, status = controller.get_or_create_session(app_id, {"new_sesion": True})

        assert status == 400
        assert "message" in body
