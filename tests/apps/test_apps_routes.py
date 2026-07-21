"""Contract tests for ``AppsRoutesController`` (the request-path logic).

Follows the ``TriggerRoutesController`` test stance: the controller's domain
methods take request data as ARGUMENTS, so they drive directly with plain dicts
— no Flask app, no namespace re-registration on the shared backend (which the
suite forbids). Wire shapes match the committed clients (console ``apps.ts`` /
``types.ts`` + the injected SDK): AppDetail ({spec, versions}), AppSummary
gallery, the consolidated ``/system`` payload, token_id as the read credential,
token-scope enforcement, and the missing-write-verb 405 shape.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask_restx import Resource
from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import CollectionSpec, CronSchedule, PipelineRun, PipelineSpec
from mewbo_api.apps.routes import AppData, AppsRoutesController, AppSystem
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore
from mewbo_api.apps.tokens import AppReadTokenSigner
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


class FakeSessions:
    def __init__(self) -> None:
        self._n = 0

    def create_session(self) -> str:
        self._n += 1
        return f"session-{self._n}"

    def tag_session(self, session_id: str, tag: str) -> None:  # noqa: D401 - fake
        pass

    def append_context_event(self, session_id: str, context: dict) -> None:
        pass

    def append_event(self, session_id: str, event: dict) -> None:
        pass


def _make(tmp_path, *, allow: bool = True):
    """Build a controller over real JSON stores. ``allow`` gates the api-key guard."""
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    trigger_store = JsonTriggerStore(data_file=tmp_path / "triggers.json")
    sessions = FakeSessions()
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=trigger_store,
        trigger_policy=TriggerPolicy(),
        sessions=sessions,
        now_fn=lambda: NOW,
    )

    def guard():
        return None if allow else ({"message": "Unauthorized"}, 401)

    def master_guard():
        return None if allow else ({"message": "Unauthorized"}, 401)

    controller = AppsRoutesController(
        lifecycle=lifecycle,
        app_store=app_store,
        run_store=run_store,
        data_store=data_store,
        trigger_store=trigger_store,
        token_signer=AppReadTokenSigner(secret="sekret"),
        require_api_key=guard,
        require_master_token=master_guard,
        now_fn=lambda: NOW,
    )
    return controller


def _create_live_app(controller) -> str:
    """Create a draft then submit it live; return the app_id."""
    body, status = controller.create_app({"intent": "Digest my email"})
    assert status == 201
    app_id = body["app_id"]
    builder_sid = body["session_id"]
    draft = controller.app_store.get(app_id)
    controller.lifecycle.submit(draft, builder_session_id=builder_sid)
    return app_id


def _create_live_app_with_pipeline(controller, *, pipeline_name: str = "p", armed: bool) -> str:
    """Create+submit a live app with one pipeline: a declared schedule vs on-demand.

    ``armed=True`` declares a ``time.cron`` schedule the PLATFORM arms at submit
    (Phase 1 — the primary path); ``armed=False`` is an ``on_demand`` pipeline
    (no armed wake, so it lands in ``unscheduled_pipelines``).
    """
    body, status = controller.create_app({"intent": "Digest my email"})
    assert status == 201
    app_id = body["app_id"]
    builder_sid = body["session_id"]
    if armed:
        pipeline = PipelineSpec(
            name=pipeline_name, wake_prompt="wake", schedule=CronSchedule(cron="0 9 * * *")
        )
    else:
        pipeline = PipelineSpec(name=pipeline_name, wake_prompt="wake", on_demand=True)
    draft = controller.app_store.get(app_id)
    draft = draft.model_copy(update={"pipelines": [pipeline]})
    controller.lifecycle.submit(draft, builder_session_id=builder_sid)
    return app_id


def _create_live_app_with_collections(controller, collection_names: list[str]) -> str:
    """Create+submit a live app declaring *collection_names* (schema is irrelevant here)."""
    body, status = controller.create_app({"intent": "Digest my email"})
    assert status == 201
    app_id = body["app_id"]
    builder_sid = body["session_id"]
    collections = [
        CollectionSpec(name=name, json_schema={"type": "object"}) for name in collection_names
    ]
    draft = controller.app_store.get(app_id)
    draft = draft.model_copy(update={"collections": collections})
    controller.lifecycle.submit(draft, builder_session_id=builder_sid)
    return app_id


def _create_live_app_with_user_writable_pipeline(controller, *, name: str = "form") -> str:
    """Create+submit a live app with one ``user_writable`` ``mode="code"`` pipeline.

    This is the shape a WRITE token may be minted for (wave 5): a form
    pipeline whose params are user input the served frontend submits.
    """
    body, status = controller.create_app({"intent": "A form app"})
    assert status == 201
    app_id = body["app_id"]
    builder_sid = body["session_id"]
    entrypoint = f"pipelines/{name}.py"
    pipeline = PipelineSpec(
        name=name,
        wake_prompt="unused for code mode",
        on_demand=True,
        mode="code",
        entrypoint=entrypoint,
        user_writable=True,
    )
    source = "def run(params, ctx):\n    return params\n"
    draft = controller.app_store.get(app_id)
    frontend = draft.frontend.model_copy(
        update={"files": {**draft.frontend.files, entrypoint: source}}
    )
    draft = draft.model_copy(update={"pipelines": [pipeline], "frontend": frontend})
    controller.lifecycle.submit(draft, builder_session_id=builder_sid)
    return app_id


class TestCrud:
    def test_create_returns_app_id_and_builder_session(self, tmp_path):
        controller = _make(tmp_path)
        body, status = controller.create_app({"intent": "Track my reading list"})
        assert status == 201
        assert set(body) == {"app_id", "session_id"}
        spec = controller.app_store.get(body["app_id"])
        assert spec.status == "building"
        assert spec.owner_session_id == body["session_id"]

    def test_create_rejects_unknown_field(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.create_app({"intent": "x", "token": "smuggled"})
        assert status == 400  # extra="forbid" turns a smuggled field into a clean 400

    def test_create_requires_intent(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.create_app({})
        assert status == 400

    def test_get_returns_detail_envelope(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, status = controller.get_app(app_id)
        assert status == 200
        assert body["spec"]["app_id"] == app_id
        assert [v["version"] for v in body["versions"]] == [1]  # AppDetail carries history

    def test_get_survives_a_pre_phase1_version_snapshot(self, tmp_path):
        # Regression for the live 500 on app-a2a5f299e0ad: the append-only
        # version history holds pre-existing snapshots whose pipelines declare no
        # schedule/on_demand/trigger_ref. The wakeability floor lives at the
        # SUBMIT boundary, so this history must parse and the detail must 200.
        from mewbo_api.apps.models import AppVersion

        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        live = controller.app_store.get(app_id)
        legacy_spec = live.model_dump(mode="json")
        legacy_spec["pipelines"] = [
            {  # the exact production shape from the incident traceback
                "name": "refresh-trackers",
                "wake_prompt": "refresh",
                "trigger_ref": None,
                "tools_allowlist": ["app_data", "read_file"],
                "cursor": {},
            }
        ]
        controller.app_store.save_version(
            AppVersion.model_validate(
                {"app_id": app_id, "version": 2, "spec": legacy_spec, "author": "builder"}
            )
        )
        body, status = controller.get_app(app_id)
        assert status == 200
        assert sorted(v["version"] for v in body["versions"]) == [1, 2]

    def test_render_version_carries_summary_and_verification(self, tmp_path):
        # A version snapshot on the wire now carries the change summary + the
        # per-pipeline verification verdict (Task D reads these off versions[]).
        controller = _make(tmp_path)
        app_id = _create_live_app_with_pipeline(controller, armed=False)  # one agentic pipeline
        body, _ = controller.get_app(app_id)
        v1 = next(v for v in body["versions"] if v["version"] == 1)
        assert v1["summary"] is not None
        assert v1["summary"]["pipelines_added"] == ["p"]
        # The routes controller's lifecycle has no tracker (no runner reachable), so
        # the agentic pipeline verifies as "skipped" — present + shaped on the wire.
        assert v1["verification"] == {"p": "skipped"}

    def test_render_version_old_row_without_new_fields_renders_null(self, tmp_path):
        # A pre-transparency snapshot (no summary/verification) must still render —
        # the fields default None and dump as null, never a 500 or a missing key.
        from mewbo_api.apps.models import AppVersion

        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        live = controller.app_store.get(app_id)
        controller.app_store.save_version(
            AppVersion.model_validate(
                {
                    "app_id": app_id,
                    "version": 2,
                    "spec": live.model_dump(mode="json"),
                    "author": "user",
                }
            )
        )
        body, status = controller.get_app(app_id)
        assert status == 200
        v2 = next(v for v in body["versions"] if v["version"] == 2)
        assert v2["summary"] is None and v2["verification"] is None

    def test_list_returns_lean_summaries(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        listing, _ = controller.list_apps()
        card = next(a for a in listing["apps"] if a["app_id"] == app_id)
        assert "frontend" not in card  # lean projection, no source
        assert set(card) >= {"app_id", "title", "status", "version", "workspace_ref"}

    def test_get_unknown_is_404_with_message(self, tmp_path):
        controller = _make(tmp_path)
        body, status = controller.get_app("nope")
        # Error bodies carry a top-level `message` (console readJson reads it).
        assert status == 404 and "not found" in body["message"]

    def test_patch_edits_presentation_only(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, status = controller.patch_app(app_id, {"title": "Renamed", "icon": "📚"})
        assert status == 200
        assert body["spec"]["title"] == "Renamed"
        assert body["spec"]["icon"] == "📚"

    def test_patch_rejects_status_field(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        _, status = controller.patch_app(app_id, {"status": "archived"})
        assert status == 400  # status is not a client-editable field

    def test_archive_returns_bare_spec(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, status = controller.archive_app(app_id)
        assert status == 200 and body["status"] == "archived"
        # Archived apps drop out of the gallery.
        listing, _ = controller.list_apps()
        assert app_id not in {a["app_id"] for a in listing["apps"]}


class TestReadAuthorization:
    def test_master_key_passes(self, tmp_path):
        controller = _make(tmp_path)
        assert controller.authorize_read("app-x", credential_ok=True, app_token=None) is None

    def test_no_credential_no_token_is_401(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.authorize_read("app-x", credential_ok=False, app_token=None)
        assert status == 401

    def test_token_id_is_the_credential(self, tmp_path):
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-x", now=NOW)
        # The client presents token_id verbatim (the whole signed blob).
        assert (
            controller.authorize_read(
                "app-x", credential_ok=False, app_token=token.token_id
            )
            is None
        )

    def test_valid_token_for_other_app_is_403(self, tmp_path):
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-other", now=NOW)
        _, status = controller.authorize_read(
            "app-x", credential_ok=False, app_token=token.token_id
        )
        assert status == 403  # a valid token for the WRONG app never reads this one

    def test_forged_token_is_401(self, tmp_path):
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-x", now=NOW)
        _, status = controller.authorize_read(
            "app-x", credential_ok=False, app_token=token.token_id[:-2] + "zz"
        )
        assert status == 401

    def test_write_token_also_passes_read_auth(self, tmp_path):
        # Write is a superset: any valid scope satisfies the read-auth floor.
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-x", now=NOW, scope="write")
        assert (
            controller.authorize_read("app-x", credential_ok=False, app_token=token.token_id)
            is None
        )


class TestWriteAuthorization:
    def test_master_key_passes(self, tmp_path):
        controller = _make(tmp_path)
        assert controller.authorize_write("app-x", credential_ok=True, app_token=None) is None

    def test_read_scoped_token_is_403(self, tmp_path):
        # A valid, correctly-scoped-for-app READ token must not satisfy a
        # write-gated call — never a silent downgrade.
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-x", now=NOW, scope="read")
        _, status = controller.authorize_write(
            "app-x", credential_ok=False, app_token=token.token_id
        )
        assert status == 403

    def test_write_scoped_token_passes(self, tmp_path):
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-x", now=NOW, scope="write")
        assert (
            controller.authorize_write("app-x", credential_ok=False, app_token=token.token_id)
            is None
        )

    def test_write_token_for_other_app_is_403(self, tmp_path):
        controller = _make(tmp_path)
        token = controller.token_signer.mint("app-other", now=NOW, scope="write")
        _, status = controller.authorize_write(
            "app-x", credential_ok=False, app_token=token.token_id
        )
        assert status == 403

    def test_no_credential_no_token_is_401(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.authorize_write("app-x", credential_ok=False, app_token=None)
        assert status == 401

    def test_legacy_four_part_token_verifies_as_read_so_write_auth_403s(self, tmp_path):
        # A token minted before Phase 2 (4-part blob, no scope segment) must
        # still verify — but only ever as "read", so it 403s a write-gated call.
        controller = _make(tmp_path)
        signer = controller.token_signer
        exp = int(NOW.timestamp()) + 1800
        nonce = "deadbeef"
        message = f"app-x:{exp}:{nonce}"
        legacy_token_id = f"{message}:{signer._sign(message)}"
        verified = signer.verify(legacy_token_id, now=NOW)
        assert verified is not None and verified.scope == "read"
        _, status = controller.authorize_write(
            "app-x", credential_ok=False, app_token=legacy_token_id
        )
        assert status == 403


class TestDataAndSystem:
    def test_read_data_returns_appdatadoc_envelopes(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        controller.data_store.upsert(app_id, "tasks", "a", {"title": "A", "done": False})
        controller.data_store.upsert(app_id, "tasks", "b", {"title": "B", "done": True})
        body, status = controller.read_data(
            app_id, "tasks", filter={"done": False}, sort=None, limit=100
        )
        assert status == 200
        assert body["collection"] == "tasks"
        # Documents are full AppDataDoc envelopes; the SDK unwraps `row["doc"]`.
        assert len(body["documents"]) == 1
        doc = body["documents"][0]
        assert doc["key"] == "a"
        assert doc["doc"] == {"title": "A", "done": False}

    def test_read_data_unknown_app_404(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.read_data("nope", "tasks", filter=None, sort=None, limit=100)
        assert status == 404

    def test_system_health_consolidated_shape(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, status = controller.system_health(app_id)
        assert status == 200
        assert body["app_id"] == app_id
        # unscheduled_pipelines is a TOP-LEVEL field (a peer of freshness/triggers/
        # runs/maintainer) — the console's landed type expects it there, not nested.
        assert set(body) == {
            "app_id",
            "status",
            "freshness",
            "triggers",
            "runs",
            "maintainer",
            "pipelines",
            "unscheduled_pipelines",
        }
        assert set(body["freshness"]) == {
            "last_success_at",
            "last_run_status",
            "next_fire_at",
            "stale",
            "unwritten_collections",
        }
        assert body["freshness"]["stale"] is False  # no runs yet -> not stale
        assert body["freshness"]["unwritten_collections"] == []  # no runs, nothing to report
        assert body["unscheduled_pipelines"] == []  # no pipelines at all
        assert body["pipelines"] == []  # no pipelines at all
        assert body["maintainer"]["session_id"] is not None

    def test_system_health_flags_a_pipeline_with_no_armed_trigger(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_pipeline(controller, pipeline_name="ingest", armed=False)
        body, _ = controller.system_health(app_id)
        assert body["unscheduled_pipelines"] == ["ingest"]

    def test_system_health_omits_a_properly_armed_pipeline(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_pipeline(controller, pipeline_name="ingest", armed=True)
        body, _ = controller.system_health(app_id)
        assert body["unscheduled_pipelines"] == []

    def test_system_health_pipeline_row_carries_declared_scheduled_tier(self, tmp_path):
        # Phase 1: each pipeline row carries the declared schedule union +
        # on_demand + trigger_ref + armed, so a client renders "refreshes hourly".
        controller = _make(tmp_path)
        app_id = _create_live_app_with_pipeline(controller, pipeline_name="ingest", armed=True)
        body, _ = controller.system_health(app_id)
        row = next(p for p in body["pipelines"] if p["name"] == "ingest")
        assert row["schedule"] == {"kind": "time.cron", "cron": "0 9 * * *"}
        assert row["on_demand"] is False
        assert row["trigger_ref"] is not None  # platform-stamped
        assert row["armed"] is True
        assert row["mode"] == "agentic" # Phase 2: additive, default unchanged

    def test_system_health_pipeline_row_marks_on_demand(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_pipeline(controller, pipeline_name="reindex", armed=False)
        body, _ = controller.system_health(app_id)
        row = next(p for p in body["pipelines"] if p["name"] == "reindex")
        assert row["schedule"] is None
        assert row["on_demand"] is True
        assert row["trigger_ref"] is None
        assert row["armed"] is False
        assert row["mode"] == "agentic"

    def test_system_health_pipeline_row_carries_code_mode(self, tmp_path):
        # Phase 2: a mode="code" pipeline's row says so (additive field).
        controller = _make(tmp_path)
        body, status = controller.create_app({"intent": "Weekly report"})
        assert status == 201
        app_id = body["app_id"]
        builder_sid = body["session_id"]
        pipeline = PipelineSpec(
            name="report",
            wake_prompt="unused for code mode",
            on_demand=True,
            mode="code",
            entrypoint="pipelines/report.py",
            params_schema={"type": "object", "properties": {"since": {"type": "string"}}},
            cache_ttl_seconds=300,
        )
        draft = controller.app_store.get(app_id)
        # Give the bundle the entrypoint file too, so this test stays valid even
        # once the lifecycle grows a submit-time "entrypoint resolves to a real
        # bundle file" check (per PipelineSpec's docstring).
        report_source = "def run(params, ctx):\n    return params\n"
        frontend = draft.frontend.model_copy(
            update={"files": {**draft.frontend.files, "pipelines/report.py": report_source}}
        )
        draft = draft.model_copy(update={"pipelines": [pipeline], "frontend": frontend})
        controller.lifecycle.submit(draft, builder_session_id=builder_sid)
        sys_body, _ = controller.system_health(app_id)
        row = next(p for p in sys_body["pipelines"] if p["name"] == "report")
        assert row["mode"] == "code"

    def test_system_health_runs_carry_the_wrote_nothing_honesty_flag(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        empty_run = PipelineRun.open(run_key="r-empty", app_id=app_id, pipeline_name="p", now=NOW)
        empty_run.close(now=NOW, status="succeeded")  # no writes recorded
        controller.run_store.save(empty_run)
        real_run = PipelineRun.open(run_key="r-real", app_id=app_id, pipeline_name="p", now=NOW)
        real_run.record_write("tasks", 3)
        real_run.close(now=NOW, status="succeeded")
        controller.run_store.save(real_run)

        body, _ = controller.system_health(app_id)
        runs_by_key = {r["run_key"]: r for r in body["runs"]}
        assert runs_by_key["r-empty"]["wrote_nothing"] is True
        assert runs_by_key["r-real"]["wrote_nothing"] is False

    def test_system_health_flags_a_declared_collection_no_run_ever_wrote(self, tmp_path):
        # The verified defect: a run succeeds (stale=False), writes real data
        # to ONE declared collection, but a SECOND declared collection — the
        # one a frontend page actually reads — never receives a document, and
        # `stale` alone can't see it. `unwritten_collections` is the added
        # signal that does.
        controller = _make(tmp_path)
        app_id = _create_live_app_with_collections(controller, ["records", "today_digest"])
        run = PipelineRun.open(run_key="r1", app_id=app_id, pipeline_name="p", now=NOW)
        run.record_write("today_digest", 1)
        run.close(now=NOW, status="succeeded")
        controller.run_store.save(run)

        body, _ = controller.system_health(app_id)
        assert body["freshness"]["stale"] is False  # the run DID succeed
        assert body["freshness"]["unwritten_collections"] == ["records"]
        run_row = next(r for r in body["runs"] if r["run_key"] == "r1")
        assert run_row["unwritten_collections"] == ["records"]

    def test_system_health_omits_declared_collections_the_latest_run_wrote(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_collections(controller, ["records"])
        run = PipelineRun.open(run_key="r1", app_id=app_id, pipeline_name="p", now=NOW)
        run.record_write("records", 2)
        run.close(now=NOW, status="succeeded")
        controller.run_store.save(run)

        body, _ = controller.system_health(app_id)
        assert body["freshness"]["unwritten_collections"] == []

    def test_read_routes_are_read_only(self):
        # The read-only contract at the HTTP layer: the data + consolidated system
        # Resources implement ONLY `get`, so any write verb (POST/PUT/PATCH/DELETE)
        # hits a method Flask-RESTX answers with 405. Guards a future write leak.
        for resource in (AppData, AppSystem):
            assert hasattr(resource, "get")
            for verb in ("post", "put", "patch", "delete"):
                assert getattr(resource, verb, None) is getattr(Resource, verb, None)


class TestManagement:
    def test_mint_token_returns_token_id_credential(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, status = controller.mint_token(app_id, {})
        assert status == 201
        # Exactly the AppReadToken wire shape the console consumes.
        assert set(body) == {"token_id", "app_id", "scope", "expires_at"}
        assert body["app_id"] == app_id and body["scope"] == "read"
        verified = controller.token_signer.verify(body["token_id"], now=NOW)
        assert verified is not None and verified.app_id == app_id

    def test_mint_token_unknown_app_404(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.mint_token("nope", {})
        assert status == 404

    def test_mint_token_rejects_bad_body(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        _, status = controller.mint_token(app_id, {"scope": "admin"})
        assert status == 400  # not a valid AppReadTokenScope literal

    def test_mint_token_rejects_smuggled_field(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        _, status = controller.mint_token(app_id, {"scope": "read", "app_id": "app-other"})
        assert status == 400  # extra="forbid" — app_id is server-owned, never client input

    def test_mint_token_write_scope_requires_master_token(self, tmp_path):
        # allow=True -> the fake master-token guard passes; the OTHER app-key-only
        # guard used elsewhere in this suite is a distinct collaborator (see
        # test_mint_token_write_scope_rejected_when_master_guard_denies). The app
        # must ALSO declare a user_writable pipeline (the structural gate below).
        controller = _make(tmp_path)
        app_id = _create_live_app_with_user_writable_pipeline(controller)
        body, status = controller.mint_token(app_id, {"scope": "write"})
        assert status == 201
        assert body["scope"] == "write"
        verified = controller.token_signer.verify(body["token_id"], now=NOW)
        assert verified is not None and verified.scope == "write"

    def test_mint_token_write_scope_403_without_user_writable_pipeline(self, tmp_path):
        # Structural least privilege (wave 5): even with the master token, a
        # write token can only be minted for an app that DECLARES a user_writable
        # pipeline — an app with none (here, no pipelines at all) is 403'd, so a
        # served frontend can never obtain a write-back credential it can't use.
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        _, status = controller.mint_token(app_id, {"scope": "write"})
        assert status == 403
        # Read minting on the same app is unaffected.
        body, status = controller.mint_token(app_id, {})
        assert status == 201 and body["scope"] == "read"

    def test_mint_token_write_scope_master_guard_denies_before_user_writable_check(self, tmp_path):
        # Ordering: the master-token guard runs BEFORE the user_writable check, so
        # a non-master key is 401 (not 403) regardless of the app's pipelines.
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)  # no user_writable pipeline
        controller.require_master_token = lambda: ({"message": "Unauthorized"}, 401)
        _, status = controller.mint_token(app_id, {"scope": "write"})
        assert status == 401

    def test_mint_token_write_scope_rejected_when_master_guard_denies(self, tmp_path):
        # An issued (non-master) key passes require_api_key but must NOT be able
        # to mint a write-scoped token — require_master_token is the separate,
        # stricter gate that denies it.
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        controller.require_master_token = lambda: ({"message": "Unauthorized"}, 401)
        _, status = controller.mint_token(app_id, {"scope": "write"})
        assert status == 401
        # Read minting is unaffected by the same denied guard.
        body, status = controller.mint_token(app_id, {})
        assert status == 201 and body["scope"] == "read"

    def test_pause_resume_return_bare_spec(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        paused, _ = controller.pause_app(app_id)
        assert paused["status"] == "paused"
        resumed, _ = controller.resume_app(app_id)
        assert resumed["status"] == "live"

    def test_rollback_returns_detail_envelope(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, status = controller.rollback_app(app_id, {"version": 1})
        assert status == 200
        assert body["spec"]["version"] == 2  # append-only repoint
        # AppDetail versions are newest-first (latest at the top).
        assert [v["version"] for v in body["versions"]] == [2, 1]

    def test_rollback_unknown_version_404(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        _, status = controller.rollback_app(app_id, {"version": 9})
        assert status == 404

    def test_rollback_rejects_bad_body(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        _, status = controller.rollback_app(app_id, {"version": "not-int"})
        assert status == 400
