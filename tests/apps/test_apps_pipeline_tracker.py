"""Contract tests for ``AppPipelineRunTracker`` — the PipelineRun ledger seam.

Real JSON app + run stores (the ledger math is the contract under test); the
failure handler is the one faked collaborator, and NOW is injected. Covers the
open seam (trigger fire -> running run, keyed on the firing trigger), the close
seam (session end -> succeeded/failed by outcome, failed dispatches the policy),
the no-op paths (non-app fire, non-app session, already-open), and the I3
``pipeline_scope`` least-privilege derivation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from mewbo_api.apps.models import (
    AppFrontend,
    AppSpec,
    CollectionSpec,
    PipelineIssue,
    PipelineRun,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import PipelineExecutionError
from mewbo_api.apps.pipeline_tracker import _APP_DATA_TOOL_ID, AppPipelineRunTracker
from mewbo_api.apps.plugin.app_data import APP_DATA_TOOL_ID
from mewbo_api.apps.store import JsonAppStore, JsonPipelineRunStore, new_run_key

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)
MAINTAINER = "maint-1"
TRIGGER = "trig-1"


class FakeFailureHandler:
    """Records ``handle_pipeline_failure(app, issue)`` calls (the lifecycle stand-in)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, PipelineIssue]] = []

    def handle_pipeline_failure(self, app: AppSpec, issue: PipelineIssue) -> None:
        self.calls.append((app.app_id, issue))

    @property
    def issues(self) -> list[PipelineIssue]:
        """Just the issues, for tests that don't care which app dispatched."""
        return [issue for _, issue in self.calls]


class FakeRunStarter:
    """Records ``start_app_run`` calls; returns the widened landed signal."""

    def __init__(self, *, landed: str = "started") -> None:
        self.calls: list[tuple[str, str]] = []
        self._landed = landed

    def start_app_run(self, session_id: str, message: str) -> str:
        self.calls.append((session_id, message))
        return self._landed


@dataclass
class _FakeResult:
    output: Any
    evaluated_at: datetime
    cache: str = "miss"
    docs_written: dict[str, int] = field(default_factory=dict)


class _FakeRunner:
    """Fake ``AppPipelineRunner`` for code-fire tests (records calls; echoes params)."""

    def __init__(self, *, raises=None, cache="miss", docs_written=None):  # noqa: ANN001
        self.calls: list[tuple[str, str, dict]] = []
        self._raises = raises
        self._cache = cache
        self._docs = docs_written or {}

    def execute(self, app, pipeline, params, *, now, dry_run=False):  # noqa: ANN001, ANN201
        self.calls.append((app.app_id, pipeline.name, dict(params)))
        if self._raises is not None:
            raise self._raises
        return _FakeResult(
            output={"echo": params}, evaluated_at=now, cache=self._cache,
            docs_written=dict(self._docs),
        )

    @staticmethod
    def params_hash(params: dict) -> str:  # noqa: ANN001
        from mewbo_api.apps.pipeline_runner import AppPipelineRunner

        return AppPipelineRunner.params_hash(params)


def _app(
    *, pipelines, maintainer=MAINTAINER, app_id="app-x", status="live", collections=(), policy=None
) -> AppSpec:
    return AppSpec(
        app_id=app_id,
        title="App",
        owner_session_id="owner",
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(files={"app.py": "import streamlit as st"}),
        pipelines=pipelines,
        collections=[CollectionSpec(name=n, json_schema={"type": "object"}) for n in collections],
        maintainer_session_id=maintainer,
        status=status,
        **({"policies": policy} if policy is not None else {}),
    )


def _pipeline(
    *, name="p", allowlist=None, trigger_ref: str | None = TRIGGER, cursor=None
) -> PipelineSpec:
    return PipelineSpec(
        name=name,
        wake_prompt="wake",
        trigger_ref=trigger_ref,
        tools_allowlist=allowlist or [],
        cursor=cursor or {},
    )


def _code_pipeline(*, name="p", entrypoint="pipelines/p.py") -> PipelineSpec:
    return PipelineSpec(
        name=name, wake_prompt="unused", on_demand=True, mode="code", entrypoint=entrypoint
    )


def _make(tmp_path, *, failure_handler=None, runner=None, run_starter=None, clock=None):
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    tracker = AppPipelineRunTracker(
        run_store=run_store,
        app_store=app_store,
        failure_handler=failure_handler,
        pipeline_runner=runner,
        run_starter=run_starter,
        now_fn=clock or (lambda: NOW),
    )
    return tracker, app_store, run_store


class _Clock:
    """An advancing injected clock — successive ledger rows need distinct ``started_at``."""

    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> datetime:
        self.ticks += 1
        return NOW + timedelta(minutes=10 * self.ticks)


# ---------------------------------------------------------------------------
# Open (trigger-fire seam)
# ---------------------------------------------------------------------------


class TestOpen:
    def test_fire_opens_a_running_run_keyed_on_the_trigger(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline(cursor={"since": "t0"})]))

        tracker.open_run(MAINTAINER, TRIGGER)

        run = run_store.get_open("app-x", "p")
        assert run is not None
        assert run.status == "running"
        assert run.trigger_id == TRIGGER
        assert run.session_run_id == MAINTAINER
        assert run.cursor_before == {"since": "t0"}  # snapshotted from the pipeline

    def test_non_app_trigger_is_a_noop(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, "some-other-trigger")
        assert run_store.get_open("app-x", "p") is None

    def test_empty_trigger_is_a_noop(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, "")
        assert run_store.list_runs("app-x") == []

    def test_second_fire_does_not_stack_a_second_open_run(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, TRIGGER)
        tracker.open_run(MAINTAINER, TRIGGER)
        assert len(run_store.list_runs("app-x")) == 1


# ---------------------------------------------------------------------------
# Close (session-end seam)
# ---------------------------------------------------------------------------


class TestClose:
    def test_run_end_closes_the_open_run_succeeded(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, TRIGGER)

        tracker.close_runs(MAINTAINER, None)

        run = run_store.list_runs("app-x")[0]
        assert run.status == "succeeded"
        assert run.ended_at == NOW
        assert run.error is None
        assert run_store.get_open("app-x", "p") is None
        # No app_data write hooked this run before close — the succeeded-but-wrote-
        # nothing honesty gap (logged, never raised).
        assert run.wrote_nothing is True

    def test_run_end_with_writes_recorded_is_not_flagged_wrote_nothing(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, TRIGGER)
        open_run = run_store.get_open("app-x", "p")
        open_run.record_write("tasks", 2)
        run_store.save(open_run)

        tracker.close_runs(MAINTAINER, None)

        run = run_store.list_runs("app-x")[0]
        assert run.status == "succeeded"
        assert run.wrote_nothing is False

    def test_run_end_with_error_closes_failed_and_dispatches_policy(self, tmp_path):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, TRIGGER)

        tracker.close_runs(MAINTAINER, "boom")

        run = run_store.list_runs("app-x")[0]
        assert run.status == "failed"
        assert run.error == "boom"
        # The failure policy is dispatched exactly once, with the app + the issue.
        assert handler.calls == [("app-x", PipelineIssue.run_failed("p", "boom"))]

    def test_non_app_session_end_is_a_noop(self, tmp_path):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.close_runs("not-a-maintainer", "boom")  # must not raise
        assert handler.calls == []

    def test_no_open_run_closes_nothing(self, tmp_path):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.close_runs(MAINTAINER, "boom")  # a run end with no pipeline run open
        assert run_store.list_runs("app-x") == []
        assert handler.calls == []


# ---------------------------------------------------------------------------
# I3 — least-privilege tool scope for an unattended fire
# ---------------------------------------------------------------------------


class TestSweepOrphanedRuns:
    """Startup honesty sweep: a run left ``running`` across a process restart has
    no live maintainer session behind it anymore and would otherwise sit forever."""

    def test_sweeps_running_runs_to_failed_interrupted(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, TRIGGER)  # left "running" — no close ever fires

        later = NOW + timedelta(hours=1)
        count = tracker.sweep_orphaned_runs(later)

        assert count == 1
        run = run_store.list_runs("app-x")[0]
        assert run.status == "failed"
        assert run.ended_at == later
        assert run.error == "interrupted: process restart"

    def test_leaves_already_closed_runs_untouched(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline()]))
        tracker.open_run(MAINTAINER, TRIGGER)
        tracker.close_runs(MAINTAINER, None)  # settles normally before the "restart"

        count = tracker.sweep_orphaned_runs(NOW + timedelta(hours=1))

        assert count == 0
        run = run_store.list_runs("app-x")[0]
        assert run.status == "succeeded"  # untouched

    def test_no_apps_is_a_noop(self, tmp_path):
        tracker, *_ = _make(tmp_path)
        assert tracker.sweep_orphaned_runs(NOW) == 0

    def test_sweeps_across_multiple_apps(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path)
        app_store.save(
            _app(app_id="app-a", pipelines=[_pipeline(trigger_ref="trig-a")], maintainer="maint-a")
        )
        app_store.save(
            _app(app_id="app-b", pipelines=[_pipeline(trigger_ref="trig-b")], maintainer="maint-b")
        )
        tracker.open_run("maint-a", "trig-a")
        tracker.open_run("maint-b", "trig-b")

        count = tracker.sweep_orphaned_runs(NOW + timedelta(hours=1))

        assert count == 2
        assert run_store.list_runs("app-a")[0].status == "failed"
        assert run_store.list_runs("app-b")[0].status == "failed"


class TestPipelineScope:
    def test_non_empty_allowlist_is_authoritative_plus_app_data(self, tmp_path):
        tracker, app_store, _ = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline(allowlist=["web_search", "read_file"])]))
        scope = tracker.pipeline_scope(TRIGGER)
        assert scope == (["web_search", "read_file", "app_data"], True)

    def test_allowlist_already_naming_app_data_is_not_duplicated(self, tmp_path):
        tracker, app_store, _ = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline(allowlist=["app_data", "web_search"])]))
        allow, strict = tracker.pipeline_scope(TRIGGER)
        assert allow == ["app_data", "web_search"]
        assert strict is True

    def test_empty_allowlist_is_least_privilege_app_data_only(self, tmp_path):
        # Phase 1 security flip: an empty allowlist is no longer permissive —
        # an undeclared unattended fire is scoped to app_data ONLY (must declare more).
        tracker, app_store, _ = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline(allowlist=[])]))
        assert tracker.pipeline_scope(TRIGGER) == (["app_data"], True)

    def test_non_app_trigger_has_no_scope(self, tmp_path):
        tracker, app_store, _ = _make(tmp_path)
        app_store.save(_app(pipelines=[_pipeline(allowlist=["web_search"])]))
        assert tracker.pipeline_scope("unknown") is None
        assert tracker.pipeline_scope("") is None


class TestFirePipelineCode:
    """``fire_pipeline`` on a code pipeline — runs the engine, ALWAYS ledgers."""

    def test_code_success_ledgers_and_returns_result(self, tmp_path):
        runner = _FakeRunner(docs_written={"tasks": 2})
        tracker, app_store, run_store = _make(tmp_path, runner=runner)
        app = _app(pipelines=[_code_pipeline()])
        app_store.save(app)

        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)

        assert outcome.ok and outcome.mode == "code"
        assert outcome.result is not None and outcome.result.docs_written == {"tasks": 2}
        # An explicit fire ALWAYS ledgers (require_effect=False), even a write.
        runs = run_store.list_runs("app-x")
        assert len(runs) == 1
        assert runs[0].kind == "on_request" and runs[0].status == "succeeded"

    def test_code_no_write_success_still_ledgers(self, tmp_path):
        # Unlike the anti-spam REST invoke (require_effect=True), an explicit fire
        # ledgers even a no-write render.
        runner = _FakeRunner(docs_written={})
        tracker, app_store, run_store = _make(tmp_path, runner=runner)
        app = _app(pipelines=[_code_pipeline()])
        app_store.save(app)
        tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert len(run_store.list_runs("app-x")) == 1

    def test_code_failure_ledgers_but_never_dispatches_repair(self, tmp_path):
        handler = FakeFailureHandler()
        runner = _FakeRunner(raises=PipelineExecutionError("runtime", "blew up"))
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler, runner=runner)
        app = _app(pipelines=[_code_pipeline()])
        app_store.save(app)

        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)

        assert not outcome.ok and outcome.refusal == "code_failed"
        assert "blew up" in outcome.message
        # A failure IS ledgered, but a manual fire never auto-repairs.
        assert run_store.list_runs("app-x")[0].status == "failed"
        assert handler.calls == []

    def test_code_unwired_runner_is_not_configured(self, tmp_path):
        tracker, app_store, _ = _make(tmp_path, runner=None)
        app = _app(pipelines=[_code_pipeline()])
        app_store.save(app)
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert not outcome.ok and outcome.refusal == "not_configured"


class TestFirePipelineAgentic:
    """``fire_pipeline`` on an agentic pipeline — guards, open row, wake."""

    def _tracker(self, tmp_path, *, starter=None, status="live", maintainer=MAINTAINER):
        tracker, app_store, run_store = _make(
            tmp_path, run_starter=starter or FakeRunStarter()
        )
        app = _app(pipelines=[_pipeline(trigger_ref=None)], status=status, maintainer=maintainer)
        app_store.save(app)
        return tracker, app_store, run_store, app

    def test_success_opens_row_and_wakes_with_wake_prompt(self, tmp_path):
        starter = FakeRunStarter(landed="started")
        tracker, _, run_store, app = self._tracker(tmp_path, starter=starter)

        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)

        assert outcome.ok and outcome.mode == "agentic" and outcome.landed == "started"
        # The row is opened FIRST (kind on_request, no trigger), then the wake fires.
        run = run_store.get_open("app-x", "p")
        assert run is not None and run.kind == "on_request" and run.trigger_id is None
        assert run.session_run_id == MAINTAINER
        assert starter.calls == [(MAINTAINER, "wake")]  # the pipeline's OWN wake_prompt

    def test_steered_landed_signal_is_surfaced(self, tmp_path):
        starter = FakeRunStarter(landed="steered")
        tracker, _, _, app = self._tracker(tmp_path, starter=starter)
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert outcome.ok and outcome.landed == "steered"

    def test_not_live_is_refused(self, tmp_path):
        tracker, _, _, app = self._tracker(tmp_path, status="paused")
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert not outcome.ok and outcome.refusal == "not_live"

    def test_no_maintainer_is_refused(self, tmp_path):
        tracker, _, _, app = self._tracker(tmp_path, maintainer=None)
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert not outcome.ok and outcome.refusal == "no_maintainer"

    def test_already_running_is_refused(self, tmp_path):
        tracker, _, run_store, app = self._tracker(tmp_path)
        tracker.open_on_request_run(app, app.pipelines[0], now=NOW)  # a run is already open
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert not outcome.ok and outcome.refusal == "already_running"

    def test_cooldown_is_refused_with_retry_after(self, tmp_path):
        tracker, _, run_store, app = self._tracker(tmp_path)
        # A CLOSED run 100s ago -> not "already running", but inside the 300s cooldown.
        recent = PipelineRun.open(
            run_key=new_run_key(), app_id="app-x", pipeline_name="p",
            now=NOW - timedelta(seconds=100),
        )
        recent.close(now=NOW - timedelta(seconds=90), status="succeeded")
        run_store.save(recent)

        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert not outcome.ok and outcome.refusal == "cooldown"
        assert outcome.retry_after_seconds == 200  # 300 - 100

    def test_past_cooldown_fires(self, tmp_path):
        tracker, _, run_store, app = self._tracker(tmp_path)
        old = PipelineRun.open(
            run_key=new_run_key(), app_id="app-x", pipeline_name="p",
            now=NOW - timedelta(seconds=400),
        )
        old.close(now=NOW - timedelta(seconds=390), status="succeeded")
        run_store.save(old)
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert outcome.ok  # 400s > the 300s cooldown

    def test_unwired_starter_is_not_configured(self, tmp_path):
        tracker, app_store, _ = _make(tmp_path, run_starter=None)
        app = _app(pipelines=[_pipeline(trigger_ref=None)])
        app_store.save(app)
        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
        assert not outcome.ok and outcome.refusal == "not_configured"

    def test_refused_wake_closes_the_row_and_refuses(self, tmp_path):
        # When start_app_run reports "refused" the just-opened row has no run behind
        # it — it must be closed failed (never stranded running), and the fire must
        # report a wake_refused refusal (so the wire never carries status:"refused").
        starter = FakeRunStarter(landed="refused")
        tracker, _, run_store, app = self._tracker(tmp_path, starter=starter)

        outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)

        assert not outcome.ok and outcome.refusal == "wake_refused"
        assert run_store.get_open("app-x", "p") is None  # not stranded running
        runs = run_store.list_runs("app-x")
        assert len(runs) == 1 and runs[0].status == "failed"
        assert "wake refused" in (runs[0].error or "")

    def test_concurrent_fires_open_exactly_one_running_row(self, tmp_path):
        # The lock makes get_open + open atomic: N simultaneous fires -> exactly ONE
        # opens the row (and wakes); the rest see it and 409 already_running. Without
        # the lock this strands a second running row close_runs never settles.
        tracker, _, run_store, app = self._tracker(tmp_path)
        results: list = []
        collect_lock = threading.Lock()

        def _fire() -> None:
            outcome = tracker.fire_pipeline(app, app.pipelines[0], now=NOW)
            with collect_lock:
                results.append(outcome)

        threads = [threading.Thread(target=_fire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r.ok]
        assert len(oks) == 1
        assert all(r.refusal == "already_running" for r in results if not r.ok)
        running = [r for r in run_store.list_runs("app-x") if r.status == "running"]
        assert len(running) == 1


class TestCloseRunsKindAware:
    """A FAILED on_request close must NOT auto-repair; a scheduled one still does."""

    def test_on_request_failed_close_skips_dispatch(self, tmp_path):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler)
        app = _app(pipelines=[_pipeline(trigger_ref=None)])
        app_store.save(app)
        tracker.open_on_request_run(app, app.pipelines[0], now=NOW)

        tracker.close_runs(MAINTAINER, "boom")

        assert run_store.list_runs("app-x")[0].status == "failed"  # still ledgered
        assert handler.calls == []  # ...but no auto-repair for a manual fire

    def test_scheduled_failed_close_still_dispatches(self, tmp_path):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler)
        app_store.save(_app(pipelines=[_pipeline()]))  # trigger_ref=TRIGGER
        tracker.open_run(MAINTAINER, TRIGGER)  # a scheduled fire (kind defaults scheduled)

        tracker.close_runs(MAINTAINER, "boom")

        assert run_store.list_runs("app-x")[0].status == "failed"
        assert handler.calls == [("app-x", PipelineIssue.run_failed("p", "boom"))]

    def test_mixed_dispatches_once_when_any_scheduled_failed(self, tmp_path):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(tmp_path, failure_handler=handler)
        app = _app(pipelines=[_pipeline(name="sched"), _pipeline(name="manual", trigger_ref=None)])
        app_store.save(app)
        tracker.open_run(MAINTAINER, TRIGGER)  # opens the scheduled "sched" run
        tracker.open_on_request_run(app, app.pipelines[1], now=NOW)  # the on_request "manual" run

        tracker.close_runs(MAINTAINER, "boom")

        assert {r.status for r in run_store.list_runs("app-x")} == {"failed"}
        # exactly once, naming the SCHEDULED pipeline (not the manual one)
        assert handler.calls == [("app-x", PipelineIssue.run_failed("sched", "boom"))]


def test_app_data_tool_id_constants_match():
    # pipeline_tracker keeps its own copy (to avoid importing the heavier plugin
    # module) that MUST match the plugin's — the unenforced "must match" pair the
    # module docstring calls out.
    assert _APP_DATA_TOOL_ID == APP_DATA_TOOL_ID


class TestIntegrityDispatch:
    """A run that closes GREEN while silently writing nothing must reach the policy.

    The detect→repair loop: ``unwritten_collections`` made the violation visible,
    this seam makes it actionable — through the SAME ``on_pipeline_failure``
    dispatch a raising run uses, and WITHOUT restating a succeeded run as failed.
    """

    @staticmethod
    def _cycle(tracker, run_store, writes, *, trigger=TRIGGER, pipeline="p"):
        """One full SCHEDULED agentic cycle: fire, record *writes*, end the run."""
        tracker.open_run(MAINTAINER, trigger)
        run = run_store.get_open("app-x", pipeline)
        for name, count in writes.items():
            run.record_write(name, count)
        run_store.save(run)
        tracker.close_runs(MAINTAINER, None)

    def _wire(self, tmp_path, *, policy=None, collections=("records",)):
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(
            tmp_path, failure_handler=handler, clock=_Clock()
        )
        app_store.save(_app(pipelines=[_pipeline()], collections=collections, policy=policy))
        return handler, tracker, app_store, run_store

    def test_a_regressed_collection_dispatches_without_failing_the_run(self, tmp_path):
        handler, tracker, _, run_store = self._wire(tmp_path)
        self._cycle(tracker, run_store, {"records": 5})  # a healthy baseline run
        assert handler.issues == []

        self._cycle(tracker, run_store, {})  # ...then it stops writing

        assert handler.issues == [PipelineIssue.unwritten("p", ["records"])]
        # THE point: the run keeps telling the truth. Restating it as `failed`
        # would corrupt freshness/last-success, which is the honesty this rides on.
        assert [r.status for r in run_store.list_runs("app-x")] == ["succeeded", "succeeded"]

    def test_a_first_run_that_writes_nothing_does_not_dispatch(self, tmp_path):
        # No baseline: an app whose upstream is simply empty must not auto-repair.
        handler, tracker, _, run_store = self._wire(tmp_path)
        self._cycle(tracker, run_store, {})
        assert handler.issues == []

    def test_a_collection_never_populated_never_dispatches(self, tmp_path):
        # 'records' is declared but no run has ever filled it — legitimately empty,
        # every cycle, forever. This is the false-positive that would repair-storm.
        handler, tracker, _, run_store = self._wire(
            tmp_path, collections=("records", "digest")
        )
        for _ in range(5):
            self._cycle(tracker, run_store, {"digest": 1})
        assert handler.issues == []

    def test_repeated_fires_of_a_broken_pipeline_dispatch_exactly_once(self, tmp_path):
        # THE anti-spam bound. A repair that does not fix the break must not
        # re-dispatch on every scheduled fire from then on.
        handler, tracker, _, run_store = self._wire(tmp_path)
        self._cycle(tracker, run_store, {"records": 5})
        for _ in range(6):
            self._cycle(tracker, run_store, {})

        assert handler.issues == [PipelineIssue.unwritten("p", ["records"])]

    def test_a_recovery_then_a_second_break_dispatches_again(self, tmp_path):
        handler, tracker, _, run_store = self._wire(tmp_path)
        self._cycle(tracker, run_store, {"records": 5})
        self._cycle(tracker, run_store, {})  # break (dispatch 1)
        self._cycle(tracker, run_store, {})  # still broken (silent)
        self._cycle(tracker, run_store, {"records": 5})  # repaired
        self._cycle(tracker, run_store, {})  # breaks again (dispatch 2)

        assert handler.issues == [
            PipelineIssue.unwritten("p", ["records"]),
            PipelineIssue.unwritten("p", ["records"]),
        ]

    def test_a_manual_fire_never_dispatches(self, tmp_path):
        # Parity with the manual-failure law: a user-triggered run surfaces its
        # outcome, it never auto-repairs or auto-pauses the app.
        handler, tracker, app_store, run_store = self._wire(tmp_path)
        self._cycle(tracker, run_store, {"records": 5})  # baseline
        app = app_store.get("app-x")

        # Later than the baseline cycle — list_runs is newest-first.
        tracker.open_on_request_run(app, app.pipelines[0], now=NOW + timedelta(hours=1))
        tracker.close_runs(MAINTAINER, None)

        latest = run_store.list_runs("app-x")[0]
        assert (latest.kind, latest.status) == ("on_request", "succeeded")
        assert latest.docs_written == {}  # it regressed 'records' on the data...
        assert handler.issues == []  # ...and still must never auto-repair

    def test_an_unwired_failure_handler_is_a_clean_no_op(self, tmp_path):
        tracker, app_store, run_store = _make(tmp_path, clock=_Clock())
        app_store.save(_app(pipelines=[_pipeline()], collections=("records",)))
        self._cycle(tracker, run_store, {"records": 5})
        self._cycle(tracker, run_store, {})  # must not raise

    def test_a_second_pipelines_collection_is_not_blamed_on_this_one(self, tmp_path):
        # Collections are declared APP-wide but runs are per-PIPELINE. Pipeline 'p'
        # has never written 'other', so it must never be reported as p's regression
        # — the false positive a live data-store read would produce on every app
        # with more than one pipeline.
        handler = FakeFailureHandler()
        tracker, app_store, run_store = _make(
            tmp_path, failure_handler=handler, clock=_Clock()
        )
        app_store.save(
            _app(
                pipelines=[_pipeline(), _pipeline(name="q", trigger_ref="trig-2")],
                collections=("records", "other"),
            )
        )
        self._cycle(tracker, run_store, {"records": 5})
        self._cycle(tracker, run_store, {"records": 5})

        assert handler.issues == []
