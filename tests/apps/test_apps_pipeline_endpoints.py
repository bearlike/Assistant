"""Contract + Flask-level tests for the pipeline endpoint surface.

Covers ``GET /apps/<id>/pipelines`` (declared surface), ``GET``/``POST
/apps/<id>/pipelines/<name>`` (invoke a ``mode="code"`` pipeline), and the
write-scope token gate the POST verb requires. Controller-direct tests drive
:class:`AppsRoutesController` with plain dicts (the ``TriggerRoutesController``
stance — no Flask); the Flask-client tests hit the real registered routes
through the shared backend, the ONLY way to catch the dual-registration /
missing-``resource_class_kwargs`` class of bug (mirrors
``test_apps_routes_flask.py``).

The runner is a fake — these are contract tests from the caller site and this
suite never runs a real materialized pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import mewbo_api.backend as backend
import pytest
from mewbo_api.apps import routes as apps_routes
from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import CronSchedule, CsvResult, PipelineRun, PipelineSpec, TextResult
from mewbo_api.apps.routes import AppsRoutesController
from mewbo_api.apps.store import (
    JsonAppDataStore,
    JsonAppStore,
    JsonPipelineRunStore,
    new_run_key,
)
from mewbo_api.apps.tokens import AppReadTokenSigner
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    """Structurally satisfies routes.py's local ``PipelineResult`` Protocol."""

    output: Any
    evaluated_at: datetime
    cache: str = "miss"
    docs_written: dict[str, int] = field(default_factory=dict)


class _EchoRunner:
    """Fake ``PipelineRunner``: echoes params back as output; records every call.

    Also exposes ``params_hash`` (a real caller — ``AppPipelineRunTracker.
    record_code_run`` — calls ``runner.params_hash(params)``, so this fake must
    carry it too, not just ``execute``).
    """

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        cache: str = "miss",
        docs_written: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._raises = raises
        self._cache = cache
        self._docs_written = docs_written or {}

    def execute(self, app, pipeline, params, *, now, dry_run=False):  # noqa: ANN001, ANN201
        self.calls.append((app.app_id, pipeline.name, dict(params)))
        if self._raises is not None:
            raise self._raises
        return _FakeResult(
            output={"echo": params}, evaluated_at=now, cache=self._cache,
            docs_written=dict(self._docs_written),
        )

    @staticmethod
    def params_hash(params: dict) -> str:  # noqa: ANN001
        from mewbo_api.apps.pipeline_runner import AppPipelineRunner

        return AppPipelineRunner.params_hash(params)


class FakeRunStarter:
    """Records ``start_app_run`` calls; returns the widened landed signal."""

    def __init__(self, *, landed: str = "started") -> None:
        self.calls: list[tuple[str, str]] = []
        self._landed = landed

    def start_app_run(self, session_id: str, message: str) -> str:
        self.calls.append((session_id, message))
        return self._landed


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


def _wire_fire(controller, *, runner=None, starter=None):  # noqa: ANN001, ANN202
    """Attach a real ``AppPipelineRunTracker`` (fire seam) over the controller's stores.

    Also points ``controller.lifecycle.tracker`` at it, so a lifecycle seed/re-arm
    goes through the SAME tracker the route does. Returns the wired starter.
    """
    from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker

    starter = starter or FakeRunStarter()
    tracker = AppPipelineRunTracker(
        run_store=controller.run_store,
        app_store=controller.app_store,
        pipeline_runner=runner if runner is not None else controller.runner,
        run_starter=starter,
        now_fn=lambda: NOW,
    )
    controller.tracker = tracker
    controller.lifecycle.tracker = tracker
    controller.lifecycle.run_starter = starter
    return starter


def _make(tmp_path, *, allow: bool = True, runner: Any = None) -> AppsRoutesController:
    """Build a controller over real JSON stores, optionally with a fake *runner*."""
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    trigger_store = JsonTriggerStore(data_file=tmp_path / "triggers.json")
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=trigger_store,
        trigger_policy=TriggerPolicy(),
        sessions=FakeSessions(),
        now_fn=lambda: NOW,
    )

    def guard():
        return None if allow else ({"message": "Unauthorized"}, 401)

    return AppsRoutesController(
        lifecycle=lifecycle,
        app_store=app_store,
        run_store=run_store,
        data_store=data_store,
        trigger_store=trigger_store,
        token_signer=AppReadTokenSigner(secret="sekret"),
        require_api_key=guard,
        require_master_token=guard,
        now_fn=lambda: NOW,
        runner=runner,
    )


def _create_live_app_with_code_pipeline(
    controller: AppsRoutesController,
    *,
    pipeline_name: str = "report",
    params_schema: dict[str, Any] | None = None,
    cache_ttl_seconds: int = 0,
    on_demand: bool = True,
    user_writable: bool = False,
) -> str:
    """Create+submit a live app with one ``mode="code"`` pipeline; return the app_id."""
    body, status = controller.create_app({"intent": "Weekly report"})
    assert status == 201
    app_id = body["app_id"]
    builder_sid = body["session_id"]
    entrypoint = f"pipelines/{pipeline_name}.py"
    pipeline = PipelineSpec(
        name=pipeline_name,
        wake_prompt="unused for code mode",
        on_demand=on_demand,
        mode="code",
        entrypoint=entrypoint,
        params_schema=params_schema,
        cache_ttl_seconds=cache_ttl_seconds,
        user_writable=user_writable,
    )
    draft = controller.app_store.get(app_id)
    # Give the bundle the entrypoint file too, so this stays valid even once the
    # lifecycle grows a submit-time "entrypoint resolves to a real bundle file"
    # check (per PipelineSpec's docstring).
    source = "def run(params, ctx):\n    return params\n"
    frontend = draft.frontend.model_copy(
        update={"files": {**draft.frontend.files, entrypoint: source}}
    )
    draft = draft.model_copy(update={"pipelines": [pipeline], "frontend": frontend})
    controller.lifecycle.submit(draft, builder_session_id=builder_sid)
    return app_id


def _create_live_app_with_agentic_pipeline(
    controller: AppsRoutesController, *, pipeline_name: str = "wake"
) -> str:
    """Create+submit a live app with one default (``mode="agentic"``) pipeline."""
    body, status = controller.create_app({"intent": "Digest my email"})
    assert status == 201
    app_id = body["app_id"]
    builder_sid = body["session_id"]
    pipeline = PipelineSpec(name=pipeline_name, wake_prompt="wake up", on_demand=True)
    draft = controller.app_store.get(app_id)
    draft = draft.model_copy(update={"pipelines": [pipeline]})
    controller.lifecycle.submit(draft, builder_session_id=builder_sid)
    return app_id


# ---------------------------------------------------------------------------
# Controller-direct tests
# ---------------------------------------------------------------------------


class TestListPipelines:
    def test_lists_declared_surface_for_a_code_pipeline(self, tmp_path):
        controller = _make(tmp_path)
        schema = {"type": "object", "properties": {"since": {"type": "string"}}}
        app_id = _create_live_app_with_code_pipeline(
            controller, params_schema=schema, cache_ttl_seconds=300
        )
        body, status = controller.list_pipelines(app_id)
        assert status == 200
        row = body["pipelines"][0]
        assert row["name"] == "report"
        assert row["mode"] == "code"
        assert row["params_schema"] == schema
        assert row["cache_ttl_seconds"] == 300
        assert row["on_demand"] is True
        assert row["armed"] is False  # on_demand -> no armed trigger to be

    def test_agentic_pipeline_row_defaults(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_agentic_pipeline(controller)
        body, _ = controller.list_pipelines(app_id)
        row = body["pipelines"][0]
        assert row["mode"] == "agentic"
        assert row["params_schema"] is None
        assert row["cache_ttl_seconds"] == 0

    def test_unknown_app_404(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.list_pipelines("nope")
        assert status == 404

    def test_no_pipelines_is_an_empty_list(self, tmp_path):
        controller = _make(tmp_path)
        body, status = controller.create_app({"intent": "x"})
        app_id = body["app_id"]
        draft = controller.app_store.get(app_id)
        controller.lifecycle.submit(draft, builder_session_id=body["session_id"])
        listing, status = controller.list_pipelines(app_id)
        assert status == 200
        assert listing["pipelines"] == []


class TestInvokePipeline:
    def test_unknown_app_404(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.invoke_pipeline("nope", "x", raw_query_params={})
        assert status == 404

    def test_unknown_pipeline_404(self, tmp_path):
        controller = _make(tmp_path, runner=_EchoRunner())
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        _, status = controller.invoke_pipeline(app_id, "does-not-exist", raw_query_params={})
        assert status == 404

    def test_agentic_pipeline_is_409(self, tmp_path):
        # Unwired runner (None) proves the mode check wins BEFORE the unwired
        # 503 would otherwise fire — an agentic pipeline never reaches the runner.
        controller = _make(tmp_path, runner=None)
        app_id = _create_live_app_with_agentic_pipeline(controller, pipeline_name="wake")
        _, status = controller.invoke_pipeline(app_id, "wake", raw_query_params={})
        assert status == 409

    def test_unwired_runner_is_503(self, tmp_path):
        controller = _make(tmp_path, runner=None)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 503
        assert "not configured" in body["message"]

    def test_get_invoke_happy_path_coerces_query_params(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        schema = {
            "type": "object",
            "properties": {"since": {"type": "string"}, "limit": {"type": "integer"}},
        }
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=schema)
        body, status = controller.invoke_pipeline(
            app_id, "report", raw_query_params={"since": "2026-07-01", "limit": "5"}
        )
        assert status == 200
        assert body == {
            "output": {"echo": {"since": "2026-07-01", "limit": 5}},
            "evaluated_at": NOW.isoformat(),
            "cache": "miss",
            "docs_written": {},
            "pipeline": "report",
        }
        assert runner.calls == [(app_id, "report", {"since": "2026-07-01", "limit": 5})]

    def test_post_invoke_happy_path_json_body(self, tmp_path):
        runner = _EchoRunner(cache="hit")
        controller = _make(tmp_path, runner=runner)
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        # POST (json_body) now requires the pipeline itself be user_writable.
        app_id = _create_live_app_with_code_pipeline(
            controller, params_schema=schema, user_writable=True
        )
        body, status = controller.invoke_pipeline(app_id, "report", json_body={"q": "hello"})
        assert status == 200
        assert body["output"] == {"echo": {"q": "hello"}}
        assert body["cache"] == "hit"

    def test_post_non_user_writable_pipeline_403(self, tmp_path):
        # The POST/form path requires the TARGET pipeline declare user_writable —
        # the per-pipeline least-privilege gate (a despec'd pipeline flips to 403).
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        app_id = _create_live_app_with_code_pipeline(controller, user_writable=False)
        _, status = controller.invoke_pipeline(app_id, "report", json_body={})
        assert status == 403
        assert runner.calls == []  # never reached the runner

    def test_get_invoke_non_user_writable_pipeline_still_ok(self, tmp_path):
        # GET stays open to ANY code pipeline (effectful refresh is deliberate) —
        # the user_writable gate is the POST/form path only.
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        app_id = _create_live_app_with_code_pipeline(controller, user_writable=False)
        _, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 200

    def test_response_surfaces_docs_written(self, tmp_path):
        runner = _EchoRunner(docs_written={"tasks": 4})
        controller = _make(tmp_path, runner=runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 200
        assert body["docs_written"] == {"tasks": 4}

    def test_no_schema_no_params_succeeds(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 200
        assert body["output"] == {"echo": {}}

    def test_no_schema_any_param_is_400(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        _, status = controller.invoke_pipeline(app_id, "report", raw_query_params={"x": "1"})
        assert status == 400
        assert runner.calls == []

    def test_query_unknown_key_400(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=schema)
        _, status = controller.invoke_pipeline(app_id, "report", raw_query_params={"bogus": "1"})
        assert status == 400
        assert runner.calls == []

    def test_query_type_coercion_failure_400(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=schema)
        _, status = controller.invoke_pipeline(app_id, "report", raw_query_params={"limit": "abc"})
        assert status == 400
        assert runner.calls == []

    def test_query_boolean_coercion(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        schema = {"type": "object", "properties": {"active": {"type": "boolean"}}}
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=schema)
        body, status = controller.invoke_pipeline(
            app_id, "report", raw_query_params={"active": "true"}
        )
        assert status == 200
        assert body["output"] == {"echo": {"active": True}}

    def test_json_body_unknown_key_400(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        app_id = _create_live_app_with_code_pipeline(
            controller, params_schema=schema, user_writable=True
        )
        _, status = controller.invoke_pipeline(app_id, "report", json_body={"bogus": 1})
        assert status == 400

    def test_json_body_not_an_object_400(self, tmp_path):
        controller = _make(tmp_path, runner=_EchoRunner())
        app_id = _create_live_app_with_code_pipeline(
            controller, params_schema=None, user_writable=True
        )
        _, status = controller.invoke_pipeline(app_id, "report", json_body=["not", "a", "dict"])
        assert status == 400

    def test_required_param_missing_400(self, tmp_path):
        # An empty body is NOT unconditionally valid — a schema with `required`
        # must still reject it (the no-schema short-circuit is scoped to only
        # when there's genuinely nothing to validate against).
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        app_id = _create_live_app_with_code_pipeline(
            controller, params_schema=schema, user_writable=True
        )
        _, status = controller.invoke_pipeline(app_id, "report", json_body={})
        assert status == 400
        assert runner.calls == []

    def test_runner_exception_is_502_with_message_not_traceback(self, tmp_path):
        runner = _EchoRunner(raises=RuntimeError("boom: upstream unreachable"))
        controller = _make(tmp_path, runner=runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 502
        assert body["message"] == "boom: upstream unreachable"
        assert "Traceback" not in body["message"]


class _SpyFailureHandler:
    """Records ``handle_pipeline_failure`` calls — asserts the on-request path skips it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handle_pipeline_failure(self, app, issue) -> None:  # noqa: ANN001
        self.calls.append((app.app_id, issue.kind))


class TestInvokePipelineLedger:
    """Ledger an on-request invoke IFF it had a real effect (wrote data) or
    genuinely failed — never a cache hit or a read-only render. That line is
    what keeps a polling client from spamming the ledger."""

    @staticmethod
    def _wire_tracker(controller, runner, *, failure_handler=None):
        """Attach a real ``AppPipelineRunTracker`` over the SAME stores + *runner*."""
        from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker

        controller.tracker = AppPipelineRunTracker(
            run_store=controller.run_store,
            app_store=controller.app_store,
            failure_handler=failure_handler,
            pipeline_runner=runner,
            now_fn=lambda: NOW,
        )
        return controller

    def test_writing_invoke_ledgers_on_request_and_moves_freshness(self, tmp_path):
        runner = _EchoRunner(docs_written={"notes": 2})  # cache="miss" default
        controller = _make(tmp_path, runner=runner)
        self._wire_tracker(controller, runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)

        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 200  # the response is unaffected by ledgering either way

        runs = controller.run_store.list_runs(app_id)
        assert len(runs) == 1
        assert runs[0].kind == "on_request"
        assert runs[0].status == "succeeded"
        assert runs[0].docs_written == {"notes": 2}

        sys_body, _ = controller.system_health(app_id)
        assert sys_body["freshness"]["last_run_status"] == "succeeded"
        assert sys_body["freshness"]["stale"] is False

    def test_read_only_invoke_does_not_ledger(self, tmp_path):
        # cache="miss" (freshly computed) but docs_written empty -> no real effect.
        runner = _EchoRunner(docs_written={})
        controller = _make(tmp_path, runner=runner)
        self._wire_tracker(controller, runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)

        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 200
        assert controller.run_store.list_runs(app_id) == []

    def test_cache_hit_does_not_ledger(self, tmp_path):
        # Even with docs_written reported, a cache HIT never re-wrote anything —
        # not a fresh effect, so no row (a polling dashboard on cache_ttl_seconds
        # must not mint one per render).
        runner = _EchoRunner(cache="hit", docs_written={"notes": 2})
        controller = _make(tmp_path, runner=runner)
        self._wire_tracker(controller, runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)

        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 200
        assert body["cache"] == "hit"
        assert controller.run_store.list_runs(app_id) == []

    def test_fresh_render_result_ledgers_without_an_integrity_violation(self, tmp_path):
        runner = _EchoRunner()
        controller = _make(tmp_path, runner=runner)
        self._wire_tracker(controller, runner)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        app = controller.app_store.get(app_id)
        pipeline = app.pipelines[0].model_copy(
            update={"tier": "render", "result": TextResult()}
        )
        controller.app_store.save(app.model_copy(update={"pipelines": [pipeline]}))

        _, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})

        assert status == 200
        runs = controller.run_store.list_runs(app_id)
        assert len(runs) == 1 and runs[0].status == "succeeded"


    def test_failed_invoke_ledgers_without_dispatching_repair(self, tmp_path):
        # record_code_run only catches PipelineExecutionError internally (that's
        # what makes it write the `failed` row) — a bare exception would just
        # propagate past it to the controller's outer 502 catch, unledgered.
        from mewbo_api.apps.pipeline_runner import PipelineExecutionError

        spy = _SpyFailureHandler()
        runner = _EchoRunner(raises=PipelineExecutionError("runtime", "pipeline blew up"))
        controller = _make(tmp_path, runner=runner)
        self._wire_tracker(controller, runner, failure_handler=spy)
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)

        body, status = controller.invoke_pipeline(app_id, "report", raw_query_params={})
        assert status == 502
        assert "pipeline blew up" in body["message"]

        # A failure IS ledgered regardless of require_effect...
        runs = controller.run_store.list_runs(app_id)
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].kind == "on_request"
        # ...but dispatch_failure=False means a user-triggered failure must NOT
        # auto-repair/pause the app (only an autonomous scheduled fire does that).
        assert spy.calls == []


class TestRenderedPipelineResult:
    @staticmethod
    def _render_controller(tmp_path, result_spec, *, output):
        class _ResultRunner(_EchoRunner):
            def execute(self, app, pipeline, params, *, now, dry_run=False):  # noqa: ANN001, ANN201
                self.calls.append((app.app_id, pipeline.name, dict(params)))
                return _FakeResult(output=output, evaluated_at=now)

        controller = _make(tmp_path, runner=_ResultRunner())
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)
        app = controller.app_store.get(app_id)
        pipeline = app.pipelines[0].model_copy(update={"tier": "render", "result": result_spec})
        controller.app_store.save(app.model_copy(update={"pipelines": [pipeline]}))
        return controller, app_id

    @pytest.mark.parametrize(
        ("result_spec", "output", "body", "content_type"),
        [
            (TextResult(), "ready", "ready", "text/plain"),
            (
                CsvResult(columns=["title"]),
                [{"title": "Digest"}],
                "title\r\nDigest\r\n",
                "text/csv",
            ),
        ],
    )
    def test_result_route_renders_declared_content_type(
        self, tmp_path, result_spec, output, body, content_type
    ):
        controller, app_id = self._render_controller(tmp_path, result_spec, output=output)

        rendered = controller.render_pipeline_result(app_id, "report", raw_query_params={})

        assert rendered.body == body
        assert rendered.content_type == content_type

    def test_result_route_refuses_pipeline_without_a_result(self, tmp_path):
        controller = _make(tmp_path, runner=_EchoRunner())
        app_id = _create_live_app_with_code_pipeline(controller, params_schema=None)

        body, status = controller.render_pipeline_result(app_id, "report", raw_query_params={})

        assert status == 409
        assert "no result renderer" in body["message"]

    def test_result_route_refuses_when_its_execution_slot_is_held(self, tmp_path):
        from threading import BoundedSemaphore

        controller, app_id = self._render_controller(tmp_path, TextResult(), output="ready")
        controller.apps_max_concurrent_pipelines = 1
        controller.pipeline_execution_gate = BoundedSemaphore(1)
        assert controller.pipeline_execution_gate.acquire(blocking=False)

        body, status = controller.render_pipeline_result(app_id, "report", raw_query_params={})

        assert status == 429
        assert "capacity exhausted" in body["message"]
        controller.pipeline_execution_gate.release()


def _create_live_scheduled_app(controller: AppsRoutesController, *, name: str = "ingest") -> str:
    """Create+submit a live app with one scheduled (agentic) pipeline; return the app_id."""
    body, status = controller.create_app({"intent": "Scheduled digest"})
    assert status == 201
    app_id = body["app_id"]
    pipeline = PipelineSpec(
        name=name, wake_prompt="Ingest email", schedule=CronSchedule(cron="0 9 * * *")
    )
    draft = controller.app_store.get(app_id).model_copy(update={"pipelines": [pipeline]})
    controller.lifecycle.submit(draft, builder_session_id=body["session_id"])
    return app_id


class TestFireRouteController:
    """Controller-direct fire matrix — the fire seam is a REAL tracker over the stores.

    Apps are created BEFORE the tracker is wired, so no go-live seed fires during
    setup (it would leave an open row and perturb the guard assertions).
    """

    def test_code_fire_200_and_always_ledgers(self, tmp_path):
        controller = _make(tmp_path, runner=_EchoRunner(docs_written={"tasks": 2}))
        app_id = _create_live_app_with_code_pipeline(controller)
        _wire_fire(controller)
        body, status = controller.fire_pipeline(app_id, "report")
        assert status == 200
        assert body["mode"] == "code" and body["status"] == "succeeded"
        assert body["docs_written"] == {"tasks": 2} and body["cache"] == "miss"
        assert body["evaluated_at"] == NOW.isoformat()
        runs = controller.run_store.list_runs(app_id)
        assert len(runs) == 1 and runs[0].kind == "on_request"

    def test_code_fire_failure_502(self, tmp_path):
        from mewbo_api.apps.pipeline_runner import PipelineExecutionError

        controller = _make(
            tmp_path, runner=_EchoRunner(raises=PipelineExecutionError("runtime", "kaboom"))
        )
        app_id = _create_live_app_with_code_pipeline(controller)
        _wire_fire(controller)
        body, status = controller.fire_pipeline(app_id, "report")
        assert status == 502 and "kaboom" in body["message"]

    def test_agentic_fire_202_opens_row_and_wakes(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_agentic_pipeline(controller, pipeline_name="wake")
        starter = _wire_fire(controller)
        body, status = controller.fire_pipeline(app_id, "wake")
        assert status == 202
        assert body == {"pipeline": "wake", "mode": "agentic", "status": "started"}
        assert controller.run_store.get_open(app_id, "wake") is not None
        assert starter.calls and starter.calls[0][1] == "wake up"  # the wake_prompt

    def test_agentic_fire_not_live_409(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_agentic_pipeline(controller, pipeline_name="wake")
        _wire_fire(controller)
        paused = controller.app_store.get(app_id).model_copy(update={"status": "paused"})
        controller.app_store.save(paused)
        _, status = controller.fire_pipeline(app_id, "wake")
        assert status == 409

    def test_agentic_fire_wake_refused_409_closes_the_row(self, tmp_path):
        # A refused wake -> 409 (not a false 202), and the just-opened row is closed,
        # never stranded as a perpetual already_running.
        controller = _make(tmp_path)
        app_id = _create_live_app_with_agentic_pipeline(controller, pipeline_name="wake")
        _wire_fire(controller, starter=FakeRunStarter(landed="refused"))
        _, status = controller.fire_pipeline(app_id, "wake")
        assert status == 409
        assert controller.run_store.get_open(app_id, "wake") is None

    def test_agentic_fire_already_running_409(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_agentic_pipeline(controller, pipeline_name="wake")
        _wire_fire(controller)
        app = controller.app_store.get(app_id)
        controller.tracker.open_on_request_run(app, app.pipelines[0], now=NOW)
        _, status = controller.fire_pipeline(app_id, "wake")
        assert status == 409

    def test_agentic_fire_cooldown_429_with_retry_after(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_agentic_pipeline(controller, pipeline_name="wake")
        _wire_fire(controller)
        recent = PipelineRun.open(
            run_key=new_run_key(), app_id=app_id, pipeline_name="wake",
            now=NOW - timedelta(seconds=60),
        )
        recent.close(now=NOW - timedelta(seconds=50), status="succeeded")
        controller.run_store.save(recent)
        body, status = controller.fire_pipeline(app_id, "wake")
        assert status == 429 and body["retry_after_seconds"] == 240

    def test_unknown_app_404(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.fire_pipeline("nope", "x")
        assert status == 404

    def test_unknown_pipeline_404(self, tmp_path):
        controller = _make(tmp_path, runner=_EchoRunner())
        app_id = _create_live_app_with_code_pipeline(controller)
        _wire_fire(controller)
        _, status = controller.fire_pipeline(app_id, "does-not-exist")
        assert status == 404

    def test_unwired_tracker_503(self, tmp_path):
        controller = _make(tmp_path)  # tracker defaults None
        app_id = _create_live_app_with_code_pipeline(controller)
        _, status = controller.fire_pipeline(app_id, "report")
        assert status == 503


class TestRearmRouteController:
    """Controller-direct re-arm matrix."""

    def test_rearms_a_dead_trigger(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_scheduled_app(controller)
        maintainer = controller.app_store.get(app_id).maintainer_session_id
        controller.trigger_store.cancel_for_session(maintainer)
        body, status = controller.rearm_app(app_id, {})
        assert status == 200
        assert [a["pipeline"] for a in body["armed"]] == ["ingest"]
        assert body["seeded"] == []

    def test_no_scheduled_pipelines_is_all_empty(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_app_with_code_pipeline(controller)  # on_demand, no schedule
        body, status = controller.rearm_app(app_id, {})
        assert status == 200
        assert body == {"armed": [], "unchanged": [], "seeded": []}

    def test_not_live_409(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_scheduled_app(controller)
        paused = controller.app_store.get(app_id).model_copy(update={"status": "paused"})
        controller.app_store.save(paused)
        _, status = controller.rearm_app(app_id, {})
        assert status == 409

    def test_extra_field_400(self, tmp_path):
        controller = _make(tmp_path)
        app_id = _create_live_scheduled_app(controller)
        _, status = controller.rearm_app(app_id, {"pipeline": "ingest"})
        assert status == 400

    def test_unknown_app_404(self, tmp_path):
        controller = _make(tmp_path)
        _, status = controller.rearm_app("nope", {})
        assert status == 404

    def test_seed_true_fires_the_rearmed_pipeline(self, tmp_path):
        # No go-live seed ran (tracker was None at submit), so no open row blocks the
        # re-arm seed's agentic fire.
        controller = _make(tmp_path)
        app_id = _create_live_scheduled_app(controller)
        starter = _wire_fire(controller)
        maintainer = controller.app_store.get(app_id).maintainer_session_id
        controller.trigger_store.cancel_for_session(maintainer)
        body, status = controller.rearm_app(app_id, {"seed": True})
        assert status == 200
        assert body["seeded"] == ["ingest"]
        assert any(sid == maintainer for sid, _ in starter.calls)


# ---------------------------------------------------------------------------
# Flask-level contract tests (the dual-registration guard pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def client_and_key(tmp_path):
    """Point the registered apps controller at fresh temp stores + a fake runner.

    Also swaps ``ctrl.lifecycle`` for a fresh one over the SAME temp app_store,
    a ``FakeSessions`` backend, and no ``run_starter``. Every other apps Flask
    test avoids this by never calling ``create_app``/``submit`` through the
    shared controller — this suite's tests do (to build a real pipeline through
    the real routes), and the production-wired ``ctrl.lifecycle`` mints REAL
    sessions via ``RuntimeSessionBackend`` and kicks off a REAL (credential-less,
    failing) LLM call otherwise. Swapping the lifecycle avoids both, and keeps
    ``ctrl.app_store``/``ctrl.lifecycle.app_store`` the SAME instance so a draft
    created via the HTTP route is visible to direct store reads in test setup.

    ``ctrl.tracker`` is set to ``None`` too — the shared controller is wired at
    startup with the REAL production ``AppPipelineRunTracker`` (its OWN
    reference to the REAL ``AppPipelineRunner``, not this fixture's fake), and
    ``invoke_pipeline`` prefers the tracker over ``ctrl.runner`` when one is
    wired. Left un-swapped, every invoke in this file would silently execute
    against the real production runner instead of ``_EchoRunner``. The
    ledger/tracker-specific behavior is exercised separately via
    controller-direct tests with a purpose-built fake tracker —
    see ``TestInvokePipelineLedger`` below.
    """
    ctrl = apps_routes._controller
    assert ctrl is not None, "apps controller must be wired at backend import"
    saved_fields = (
        ctrl.app_store, ctrl.run_store, ctrl.data_store, ctrl.runner, ctrl.tracker, ctrl.now_fn
    )
    saved_lifecycle = ctrl.lifecycle
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    ctrl.app_store = app_store
    ctrl.run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    ctrl.data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    ctrl.runner = _EchoRunner()
    ctrl.tracker = None
    ctrl.now_fn = lambda: NOW
    ctrl.lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=JsonTriggerStore(data_file=tmp_path / "triggers.json"),
        trigger_policy=TriggerPolicy(),
        sessions=FakeSessions(),
        now_fn=lambda: NOW,
        # Left unwired (None, the default) -> create_draft's kickoff degrades to
        # a clean, logged no-op instead of a real LLM call.
    )
    try:
        yield backend.app.test_client(), backend.MASTER_API_TOKEN, ctrl
    finally:
        (
            ctrl.app_store, ctrl.run_store, ctrl.data_store, ctrl.runner, ctrl.tracker, ctrl.now_fn
        ) = saved_fields
        ctrl.lifecycle = saved_lifecycle


def _submit_code_pipeline(ctrl, client, key, *, params_schema=None, user_writable=False) -> str:
    resp = client.post(
        "/api/apps", headers={"X-API-Key": key}, json={"intent": "Weekly report"}
    )
    assert resp.status_code == 201
    app_id = resp.get_json()["app_id"]
    draft = ctrl.app_store.get(app_id)
    entrypoint = "pipelines/report.py"
    pipeline = PipelineSpec(
        name="report",
        wake_prompt="unused",
        on_demand=True,
        mode="code",
        entrypoint=entrypoint,
        params_schema=params_schema,
        user_writable=user_writable,
    )
    frontend = draft.frontend.model_copy(
        update={"files": {**draft.frontend.files, entrypoint: "def run(p, c):\n    return p\n"}}
    )
    draft = draft.model_copy(update={"pipelines": [pipeline], "frontend": frontend})
    ctrl.lifecycle.submit(draft, builder_session_id=resp.get_json()["session_id"])
    return app_id


def test_list_pipelines_reaches_the_controller(client_and_key):
    client, key, ctrl = client_and_key
    schema = {"type": "object", "properties": {"since": {"type": "string"}}}
    app_id = _submit_code_pipeline(ctrl, client, key, params_schema=schema)
    resp = client.get(f"/api/apps/{app_id}/pipelines", headers={"X-API-Key": key})
    assert resp.status_code == 200
    row = resp.get_json()["pipelines"][0]
    assert row["name"] == "report" and row["mode"] == "code"
    assert row["params_schema"] == schema


def test_get_invoke_happy_path(client_and_key):
    client, key, ctrl = client_and_key
    schema = {"type": "object", "properties": {"since": {"type": "string"}}}
    app_id = _submit_code_pipeline(ctrl, client, key, params_schema=schema)
    resp = client.get(
        f"/api/apps/{app_id}/pipelines/report?since=2026-07-01", headers={"X-API-Key": key}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pipeline"] == "report"
    assert body["output"] == {"echo": {"since": "2026-07-01"}}


def test_get_invoke_params_validation_400(client_and_key):
    client, key, ctrl = client_and_key
    schema = {"type": "object", "properties": {"since": {"type": "string"}}}
    app_id = _submit_code_pipeline(ctrl, client, key, params_schema=schema)
    resp = client.get(
        f"/api/apps/{app_id}/pipelines/report?bogus=1", headers={"X-API-Key": key}
    )
    assert resp.status_code == 400
    assert "message" in resp.get_json()


def test_get_invoke_agentic_pipeline_409(client_and_key):
    client, key, ctrl = client_and_key
    resp = client.post(
        "/api/apps", headers={"X-API-Key": key}, json={"intent": "Digest"}
    )
    app_id = resp.get_json()["app_id"]
    draft = ctrl.app_store.get(app_id)
    pipeline = PipelineSpec(name="wake", wake_prompt="wake up", on_demand=True)
    draft = draft.model_copy(update={"pipelines": [pipeline]})
    ctrl.lifecycle.submit(draft, builder_session_id=resp.get_json()["session_id"])
    resp = client.get(f"/api/apps/{app_id}/pipelines/wake", headers={"X-API-Key": key})
    assert resp.status_code == 409


def test_get_invoke_unknown_pipeline_404(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    resp = client.get(
        f"/api/apps/{app_id}/pipelines/does-not-exist", headers={"X-API-Key": key}
    )
    assert resp.status_code == 404


def test_get_invoke_unwired_runner_503(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    ctrl.runner = None
    resp = client.get(f"/api/apps/{app_id}/pipelines/report", headers={"X-API-Key": key})
    assert resp.status_code == 503


class TestPostWriteScopeMatrix:
    """POST .../pipelines/<name> — the write-scope enforcement matrix."""

    def test_master_key_200(self, client_and_key):
        client, key, ctrl = client_and_key
        # POST requires the pipeline itself be user_writable, master key included.
        app_id = _submit_code_pipeline(ctrl, client, key, user_writable=True)
        resp = client.post(
            f"/api/apps/{app_id}/pipelines/report", headers={"X-API-Key": key}, json={}
        )
        assert resp.status_code == 200

    def test_master_key_non_user_writable_403(self, client_and_key):
        # The per-pipeline write gate is credential-agnostic: even the master key
        # cannot POST/form-invoke a pipeline that is not user_writable (it must use
        # GET to invoke it read-only). Closes the "write token reaches a sibling
        # effectful pipeline" gap structurally.
        client, key, ctrl = client_and_key
        app_id = _submit_code_pipeline(ctrl, client, key, user_writable=False)
        resp = client.post(
            f"/api/apps/{app_id}/pipelines/report", headers={"X-API-Key": key}, json={}
        )
        assert resp.status_code == 403
        # ...while GET on the same pipeline is still fine.
        get = client.get(f"/api/apps/{app_id}/pipelines/report", headers={"X-API-Key": key})
        assert get.status_code == 200

    def test_read_token_403(self, client_and_key):
        client, key, ctrl = client_and_key
        app_id = _submit_code_pipeline(ctrl, client, key)
        mint = client.post(
            f"/api/apps/{app_id}/token", headers={"X-API-Key": key}, json={}
        )
        token = mint.get_json()["token_id"]
        resp = client.post(
            f"/api/apps/{app_id}/pipelines/report",
            headers={"X-Mewbo-App-Token": token},
            json={},
        )
        assert resp.status_code == 403

    def test_write_token_200(self, client_and_key):
        client, key, ctrl = client_and_key
        # A write token can only be minted for a user_writable app (wave 5).
        app_id = _submit_code_pipeline(ctrl, client, key, user_writable=True)
        mint = client.post(
            f"/api/apps/{app_id}/token",
            headers={"X-API-Key": key},
            json={"scope": "write"},
        )
        assert mint.status_code == 201
        token = mint.get_json()["token_id"]
        resp = client.post(
            f"/api/apps/{app_id}/pipelines/report",
            headers={"X-Mewbo-App-Token": token},
            json={},
        )
        assert resp.status_code == 200

    def test_cross_app_write_token_403(self, client_and_key):
        client, key, ctrl = client_and_key
        # Both apps are user_writable so both write mints succeed; the cross-app
        # invoke is what must 403 (a token for one app never opens another).
        app_id = _submit_code_pipeline(ctrl, client, key, user_writable=True)
        other_id = _submit_code_pipeline(ctrl, client, key, user_writable=True)
        mint = client.post(
            f"/api/apps/{other_id}/token",
            headers={"X-API-Key": key},
            json={"scope": "write"},
        )
        token = mint.get_json()["token_id"]
        resp = client.post(
            f"/api/apps/{app_id}/pipelines/report",
            headers={"X-Mewbo-App-Token": token},
            json={},
        )
        assert resp.status_code == 403

    def test_legacy_four_part_token_verifies_as_read_so_403s(self, client_and_key):
        client, key, ctrl = client_and_key
        app_id = _submit_code_pipeline(ctrl, client, key)
        signer = ctrl.token_signer
        exp = int(NOW.timestamp()) + 1800
        message = f"{app_id}:{exp}:legacynonce"
        legacy_token_id = f"{message}:{signer._sign(message)}"
        resp = client.post(
            f"/api/apps/{app_id}/pipelines/report",
            headers={"X-Mewbo-App-Token": legacy_token_id},
            json={},
        )
        assert resp.status_code == 403

    def test_no_credential_401(self, client_and_key):
        client, _key, ctrl = client_and_key
        app_id = _submit_code_pipeline(ctrl, client, _key)
        resp = client.post(f"/api/apps/{app_id}/pipelines/report", json={})
        assert resp.status_code == 401


def test_token_mint_write_scope_is_master_key_only(client_and_key):
    # An issued (non-master) API key must not be able to mint a write token —
    # only the literal master token may. KeyStore-minted keys aren't exercised
    # in-process here (no key store wiring in this fixture); this asserts the
    # master-token path succeeds and is the one this suite's other tests rely on.
    client, key, ctrl = client_and_key
    assert key == backend.MASTER_API_TOKEN
    # A write token is mintable only for a user_writable app (wave 5).
    app_id = _submit_code_pipeline(ctrl, client, key, user_writable=True)
    mint = client.post(
        f"/api/apps/{app_id}/token", headers={"X-API-Key": key}, json={"scope": "write"}
    )
    assert mint.status_code == 201
    assert mint.get_json()["scope"] == "write"


def test_token_mint_write_scope_403_without_user_writable_pipeline(client_and_key):
    # Full-stack (Flask client) counterpart to the controller-direct gate test:
    # an app with no user_writable pipeline is 403'd on a write mint even with the
    # master key, so a served frontend can never obtain an unusable write token.
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)  # user_writable=False
    mint = client.post(
        f"/api/apps/{app_id}/token", headers={"X-API-Key": key}, json={"scope": "write"}
    )
    assert mint.status_code == 403
    # Read minting on the same app still works.
    read = client.post(f"/api/apps/{app_id}/token", headers={"X-API-Key": key}, json={})
    assert read.status_code == 201 and read.get_json()["scope"] == "read"


def test_write_back_dataflow_reachable_end_to_end(tmp_path):
    """The headline wave 5 deliverable, proven with the REAL runner (no fake):

    mint a write token for a user_writable app -> authorize the write path with it ->
    POST form params (a JSON body, object values allowed) -> the code pipeline's
    `ctx.collection` write LANDS a schema-validated doc -> readable via the data GET.
    Every link the SDK's `app.pipelines.submit` traverses, end to end.
    """
    from mewbo_api.apps.models import CollectionSpec
    from mewbo_api.apps.pipeline_runner import AppPipelineRunner

    workspace = tmp_path / "ws"
    workspace.mkdir()
    controller = _make(tmp_path)  # real JSON stores; runner wired below
    controller.runner = AppPipelineRunner(
        app_store=controller.app_store,
        app_data=controller.data_store,
        workspace_resolver=lambda _app: str(workspace),
        clock=lambda: NOW,
    )

    # Build a user_writable code pipeline that writes a submitted note.
    body, status = controller.create_app({"intent": "Notes"})
    assert status == 201
    app_id = body["app_id"]
    entrypoint = "pipelines/add_note.py"
    source = (
        "def run(params, ctx):\n"
        "    ctx.collection('notes').upsert(params['key'], {'text': params['text']})\n"
        "    return {'saved': params['key']}\n"
    )
    schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}, "text": {"type": "string"}},
        "required": ["key", "text"],
        "additionalProperties": False,
    }
    pipeline = PipelineSpec(
        name="add-note",
        wake_prompt="unused",
        on_demand=True,
        mode="code",
        entrypoint=entrypoint,
        params_schema=schema,
        user_writable=True,
    )
    collection = CollectionSpec(
        name="notes",
        json_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    draft = controller.app_store.get(app_id)
    frontend = draft.frontend.model_copy(
        update={"files": {**draft.frontend.files, entrypoint: source}}
    )
    draft = draft.model_copy(
        update={"pipelines": [pipeline], "collections": [collection], "frontend": frontend}
    )
    controller.lifecycle.submit(draft, builder_session_id=body["session_id"])

    # 1) A WRITE token is mintable (the app declares a user_writable pipeline).
    token_body, status = controller.mint_token(app_id, {"scope": "write"})
    assert status == 201 and token_body["scope"] == "write"
    token_id = token_body["token_id"]

    # 2) That token authorizes the write path (a read token would 403 here).
    assert (
        controller.authorize_write(app_id, credential_ok=False, app_token=token_id) is None
    )

    # 3) POST the form params (the SDK's submit body) -> the pipeline runs + writes.
    result, status = controller.invoke_pipeline(
        app_id, "add-note", json_body={"key": "n1", "text": "hello from the form"}
    )
    assert status == 200
    assert result["output"] == {"saved": "n1"}
    assert result["docs_written"] == {"notes": 1}

    # 4) The written doc is readable via the same data GET the SDK's data.query uses.
    read, status = controller.read_data(app_id, "notes", filter=None, sort=None, limit=100)
    assert status == 200
    docs = read["documents"]
    assert [d["doc"] for d in docs] == [{"text": "hello from the form"}]


# ---------------------------------------------------------------------------
# Flask-level fire + re-arm (dual-registration guard + auth model)
# ---------------------------------------------------------------------------


def test_fire_code_reaches_the_controller(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    _wire_fire(ctrl)
    resp = client.post(f"/api/apps/{app_id}/pipelines/report/fire", headers={"X-API-Key": key})
    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "code"


def test_fire_read_auth_accepts_a_same_app_token(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    _wire_fire(ctrl)
    token = client.post(
        f"/api/apps/{app_id}/token", headers={"X-API-Key": key}, json={}
    ).get_json()["token_id"]
    resp = client.post(
        f"/api/apps/{app_id}/pipelines/report/fire", headers={"X-Mewbo-App-Token": token}
    )
    assert resp.status_code == 200  # /fire is read-auth (a served app may refresh itself)


def test_fire_rejects_a_cross_app_token(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    other = _submit_code_pipeline(ctrl, client, key)
    _wire_fire(ctrl)
    token = client.post(
        f"/api/apps/{other}/token", headers={"X-API-Key": key}, json={}
    ).get_json()["token_id"]
    resp = client.post(
        f"/api/apps/{app_id}/pipelines/report/fire", headers={"X-Mewbo-App-Token": token}
    )
    assert resp.status_code == 403


def test_fire_no_credential_401(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    _wire_fire(ctrl)
    assert client.post(f"/api/apps/{app_id}/pipelines/report/fire").status_code == 401


def test_rearm_reaches_the_controller(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)  # on_demand code app, no schedules
    resp = client.post(f"/api/apps/{app_id}/rearm", headers={"X-API-Key": key}, json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"armed": [], "unchanged": [], "seeded": []}


def test_rearm_extra_field_400(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    resp = client.post(
        f"/api/apps/{app_id}/rearm", headers={"X-API-Key": key}, json={"pipeline": "x"}
    )
    assert resp.status_code == 400


def test_rearm_requires_api_key_not_an_app_token(client_and_key):
    client, key, ctrl = client_and_key
    app_id = _submit_code_pipeline(ctrl, client, key)
    token = client.post(
        f"/api/apps/{app_id}/token", headers={"X-API-Key": key}, json={}
    ).get_json()["token_id"]
    # An app token cannot drive operator repair — master/issued key only.
    resp = client.post(
        f"/api/apps/{app_id}/rearm", headers={"X-Mewbo-App-Token": token}, json={}
    )
    assert resp.status_code == 401
    assert client.post(f"/api/apps/{app_id}/rearm", json={}).status_code == 401
