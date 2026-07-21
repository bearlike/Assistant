"""Tests for the search leg of the harness-resilience epic.

Covers, all scoped to ``apps/mewbo_api/src/mewbo_api/agentic_search/``:

* The optional per-run ``fallback_models`` ladder: the wire contract
  (``SearchRunCreateRequest``), the durable record (``RunRecord``), and the
  drive-time kwarg (``OrchestratedSearchRunner._drive`` → ``run_sync``).
* ``POST /runs`` is now a validated ``extra="forbid"`` Pydantic
  contract instead of ad hoc dict checks; a smuggled/typo'd field 400s instead
  of silently no-oping.
* ``OrchestratedSearchRunner.reconcile_after_recovery``: the ONE
  sanctioned post-terminal transition (``failed`` → ``completed``) for a run
  whose backing session recovered and actually finished after the record
  already settled wrong.
* Partial evidence (trace/results already gathered before a failure)
  is persisted onto the failed snapshot instead of discarded into an empty
  ``task_result``.

Conventions mirror ``test_orchestrated_runner.py`` (fake runtime, JSON store
under tmp dir, no LLM) and ``test_routes_scg.py`` (real Flask app + test
client, only the runner seam swapped). Sibling test helpers are reused via a
relative import — an established pattern in this suite for avoiding
namesake-fixture drift (see ``tests/CLAUDE.md``).
"""

from __future__ import annotations

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import store as store_mod
from mewbo_api.agentic_search.runner import EchoSearchRunner, set_search_runner
from mewbo_api.agentic_search.runs import SearchRun
from mewbo_api.agentic_search.scg.orchestrated_runner import OrchestratedSearchRunner
from mewbo_api.agentic_search.schemas import (
    RunRecord,
    SearchRunCreateRequest,
    WorkspaceInput,
)
from mewbo_api.agentic_search.store import JsonAgenticSearchStore
from pydantic import ValidationError

from .test_orchestrated_runner import (
    FakeRuntime,
    _completion,
    _enable_scg,
    _event_types,
    _ok_transcript,
    _run,
    _sub_agent,
    _ws,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """An isolated JSON store for the direct-runner tests (mirrors the sibling)."""
    return JsonAgenticSearchStore(root_dir=tmp_path)


@pytest.fixture(autouse=True)
def _reset_global_store_and_runner():
    """Reset the process-wide store singleton + runner override around each test.

    Only the route-level tests (which drive the real ``backend.app``) touch the
    global singleton; resetting unconditionally keeps this file isolated from
    the rest of the suite either way.
    """
    store_mod.reset_for_tests()
    set_search_runner(None)
    yield
    store_mod.reset_for_tests()
    set_search_runner(None)


def _auth() -> dict[str, str]:
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


def _any_workspace_id(client) -> str:
    body = client.get("/api/agentic_search/workspaces", headers=_auth()).get_json()
    return body["workspaces"][0]["id"]


class RecoveredRuntime:
    """A runtime stand-in for a session that recovered and genuinely completed.

    Distinct from :class:`FakeRuntime`: it reports a real (non-in-worker)
    ``summarize_session`` — the shape a post-hoc reconcile call observes, not
    the in-worker ``status="running"`` wrinkle ``FakeRuntime`` mirrors.
    """

    def __init__(self, transcript):
        self._transcript = list(transcript)

    def summarize_session(self, session_id, **_):
        return {
            "session_id": session_id,
            "status": "completed",
            "done_reason": "completed",
            "running": False,
        }

    def load_events(self, session_id, after=None):
        return list(self._transcript)


class StillFailedRuntime:
    """A runtime stand-in whose session never actually recovered."""

    def summarize_session(self, session_id, **_):
        return {
            "session_id": session_id,
            "status": "failed",
            "done_reason": "error",
            "running": False,
        }

    def load_events(self, session_id, after=None):
        return []


# ---------------------------------------------------------------------------
# fallback_models: wire contract + record + drive kwarg
# ---------------------------------------------------------------------------


def test_search_run_create_request_fallback_models_normalizes():
    """A list of ids normalizes to a clean tuple; blanks are trimmed."""
    req = SearchRunCreateRequest(
        workspace_id="ws-1",
        query="q",
        fallback_models=["  anthropic/claude-haiku-4-5  ", "", "openai/gpt-oss-120b"],
    )
    assert req.fallback_models == (
        "anthropic/claude-haiku-4-5",
        "openai/gpt-oss-120b",
    )


def test_search_run_create_request_fallback_models_absent_is_none():
    req = SearchRunCreateRequest(workspace_id="ws-1", query="q")
    assert req.fallback_models is None


def test_search_run_create_request_fallback_models_empty_after_filter_is_none():
    """An all-blank list collapses to None — same as omitting it entirely."""
    req = SearchRunCreateRequest(
        workspace_id="ws-1", query="q", fallback_models=["   ", ""]
    )
    assert req.fallback_models is None


def test_search_run_create_request_fallback_models_wrong_type_rejected():
    """Unlike model/project, a malformed fallback_models is a real 400."""
    with pytest.raises(ValidationError):
        SearchRunCreateRequest(
            workspace_id="ws-1", query="q", fallback_models="not-a-list"
        )


def test_run_record_fallback_models_defaults_to_none():
    run = RunRecord(run_id="run-y", session_id="sess-y", workspace_id="ws-1", query="q")
    assert run.fallback_models is None


def test_run_record_fallback_models_round_trips_through_json_store(store):
    run = RunRecord(
        run_id="run-x",
        session_id="sess-x",
        workspace_id="ws-1",
        query="q",
        fallback_models=("model-a", "model-b"),
    )
    store.create_run(run)
    loaded = store.get_run("run-x")
    assert loaded.fallback_models == ("model-a", "model-b")


def test_search_run_start_persists_fallback_models(store):
    """SearchRun.start() (the run-creation façade) records the opted-in ladder."""
    ws = store.create_workspace(WorkspaceInput(name="WS", sources=[]))
    SearchRun.start(
        workspace_id=ws.id,
        query="hello",
        store=store,
        fallback_models=("model-a", "model-b"),
    )
    runs = store.list_runs(ws.id)
    assert len(runs) == 1
    assert runs[0].fallback_models == ("model-a", "model-b")


def test_search_run_start_fallback_models_defaults_none(store):
    ws = store.create_workspace(WorkspaceInput(name="WS", sources=[]))
    SearchRun.start(workspace_id=ws.id, query="hello", store=store)
    runs = store.list_runs(ws.id)
    assert runs[0].fallback_models is None


def test_fallback_models_passed_to_drive(store, monkeypatch):
    """The run record's ladder rides ``run_sync(fallback_models=...)`` verbatim."""
    _enable_scg(monkeypatch)
    run = _run(store)
    run = run.model_copy(
        update={"fallback_models": ("anthropic/claude-haiku-4-5", "openai/gpt-oss-120b")}
    )
    store.update_run(run.run_id, fallback_models=run.fallback_models)
    runtime = FakeRuntime(_ok_transcript())
    OrchestratedSearchRunner().start(run, _ws(), store=store, runtime=runtime)
    assert runtime.run_sync_kwargs["fallback_models"] == (
        "anthropic/claude-haiku-4-5",
        "openai/gpt-oss-120b",
    )


def test_fallback_models_none_by_default_at_drive(store, monkeypatch):
    """Absent on the record ⇒ None at the drive ⇒ inherit config policy."""
    _enable_scg(monkeypatch)
    runtime = FakeRuntime(_ok_transcript())
    OrchestratedSearchRunner().start(_run(store), _ws(), store=store, runtime=runtime)
    assert runtime.run_sync_kwargs["fallback_models"] is None


# ---------------------------------------------------------------------------
# POST /runs is now a validated extra="forbid" contract
# ---------------------------------------------------------------------------


def test_post_run_accepts_and_echoes_fallback_models():
    seen: dict[str, object] = {}

    class _CaptureRunner(EchoSearchRunner):
        def start(self, run, workspace, *, store, runtime=None, source_platform=None):
            seen["fallback_models"] = run.fallback_models
            return super().start(
                run, workspace, store=store, runtime=runtime,
                source_platform=source_platform,
            )

    set_search_runner(_CaptureRunner())
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={
            "workspace_id": ws_id,
            "query": "q",
            "fallback_models": ["anthropic/claude-haiku-4-5", "openai/gpt-oss-120b"],
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert seen["fallback_models"] == (
        "anthropic/claude-haiku-4-5",
        "openai/gpt-oss-120b",
    )


def test_post_run_fallback_models_absent_is_none_on_record():
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]
    record = store_mod.get_store().get_run(run_id)
    assert record.fallback_models is None


def test_post_run_fallback_models_wrong_type_400s():
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "fallback_models": "not-a-list"},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_post_run_rejects_unknown_field():
    """extra="forbid" turns a smuggled/typo'd field into a clean 400 —
    the old ad hoc dict parsing silently no-oped on this."""
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "sesion_id": "smuggled-typo"},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_post_run_missing_workspace_id_400s():
    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/runs",
        json={"query": "q"},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_post_run_blank_query_400s():
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "   "},
        headers=_auth(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Post-recovery amend: failed → completed, never any other direction
# ---------------------------------------------------------------------------


def test_reconcile_after_recovery_amends_failed_to_completed(store, monkeypatch):
    """A run settled ``failed`` is amended once its session actually completes."""
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store),
        _ws(),
        store=store,
        runtime=FakeRuntime([_completion("", done_reason="error", error="boom")]),
    )
    record = store.get_run("run-1")
    assert record.status == "failed"
    assert record.session_id == "sess-scg-1"  # a real session, not the tag

    recovered_ts = "2026-01-01T00:00:00+00:00"
    recovered_transcript = [
        _sub_agent(
            "probe-a", "start", detail="probe github#search_issues",
        ),
        _sub_agent(
            "probe-a", "stop", detail="completed", status="completed",
            summary="EVIDENCE (pathway: github#search_issues): recovered evidence.",
        ),
        {
            "type": "completion",
            "ts": recovered_ts,
            "payload": {
                "done": True,
                "done_reason": "completed",
                "task_result": "Recovered answer. [github#search_issues]",
            },
        },
    ]
    amended = OrchestratedSearchRunner().reconcile_after_recovery(
        record, store=store, runtime=RecoveredRuntime(recovered_transcript)
    )

    assert amended is not None
    assert amended.status == "completed"
    assert amended.answer.tldr.startswith("Recovered answer")

    final = store.get_run("run-1")
    assert final.status == "completed"
    assert final.payload.status == "completed"
    # The completion event's OWN timestamp, never wall clock — the recovery
    # settled long after the run actually finished.
    assert final.completed_at == recovered_ts


def test_reconcile_after_recovery_noop_when_session_still_failed(store, monkeypatch):
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store),
        _ws(),
        store=store,
        runtime=FakeRuntime([_completion("", done_reason="error", error="boom")]),
    )
    record = store.get_run("run-1")
    result = OrchestratedSearchRunner().reconcile_after_recovery(
        record, store=store, runtime=StillFailedRuntime()
    )
    assert result is None
    assert store.get_run("run-1").status == "failed"


def test_reconcile_after_recovery_refuses_non_failed_record(store, monkeypatch):
    """A completed/cancelled record is final — never touched, runtime never called."""
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(_ok_transcript())
    )
    record = store.get_run("run-1")
    assert record.status == "completed"

    class _NeverTouch:
        def summarize_session(self, *a, **k):
            raise AssertionError("must not be called for a non-failed record")

        def load_events(self, *a, **k):
            raise AssertionError("must not be called for a non-failed record")

    result = OrchestratedSearchRunner().reconcile_after_recovery(
        record, store=store, runtime=_NeverTouch()
    )
    assert result is None


def test_reconcile_after_recovery_refuses_placeholder_session(store, monkeypatch):
    """A fail-fast run (no real session ever resolved) has nothing to recover."""
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.orchestrated_runner.ScgConfig.enabled",
        staticmethod(lambda: False),
    )
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(_ok_transcript())
    )
    record = store.get_run("run-1")
    assert record.status == "failed"
    assert record.session_id.startswith("agentic_search:")

    class _NeverTouch:
        def summarize_session(self, *a, **k):
            raise AssertionError("must not be called for a placeholder session")

        def load_events(self, *a, **k):
            raise AssertionError("must not be called for a placeholder session")

    result = OrchestratedSearchRunner().reconcile_after_recovery(
        record, store=store, runtime=_NeverTouch()
    )
    assert result is None


def test_search_run_facade_reconcile_after_recovery(store, monkeypatch):
    """The public ``SearchRun.reconcile_after_recovery`` entry point wires through."""
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store),
        _ws(),
        store=store,
        runtime=FakeRuntime([_completion("", done_reason="error", error="boom")]),
    )
    recovered_transcript = [
        {
            "type": "completion",
            "ts": "2026-01-01T00:00:00+00:00",
            "payload": {
                "done": True,
                "done_reason": "completed",
                "task_result": "Recovered. [x]",
            },
        },
    ]
    set_search_runner(OrchestratedSearchRunner())
    payload = SearchRun.reconcile_after_recovery(
        "run-1", store=store, runtime=RecoveredRuntime(recovered_transcript)
    )
    assert payload is not None
    assert payload.status == "completed"


def test_search_run_facade_reconcile_noop_for_echo_runner(store):
    """The echo runner has no real backing session — the façade degrades to None."""
    run = _run(store)
    store.update_run(run.run_id, status="failed")
    set_search_runner(EchoSearchRunner())
    result = SearchRun.reconcile_after_recovery(
        run.run_id, store=store, runtime=object()
    )
    assert result is None


def test_search_run_facade_reconcile_unknown_run(store):
    set_search_runner(OrchestratedSearchRunner())
    result = SearchRun.reconcile_after_recovery(
        "no-such-run", store=store, runtime=object()
    )
    assert result is None


# ---------------------------------------------------------------------------
# Partial evidence persists on a failed snapshot instead of vanishing
# ---------------------------------------------------------------------------


def test_agent_error_persists_partial_evidence_on_snapshot(store, monkeypatch):
    """A session with real probe evidence that ends done_reason=error keeps it.

    The EVIDENCE: grounded probe evidence discarded into ``task_result=""``
    on a failed run — the snapshot must not throw away what the event log
    already shows.
    """
    _enable_scg(monkeypatch)
    transcript = [
        _sub_agent("probe-a", "start", detail="probe github#search_issues"),
        _sub_agent(
            "probe-a", "stop", detail="completed", status="completed",
            summary="EVIDENCE (pathway: github#search_issues): found two issues.",
        ),
        _completion("", done_reason="error", error="rate limited after synthesis"),
    ]
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(transcript)
    )
    record = store.get_run("run-1")
    assert record.status == "failed"
    assert record.payload.partial is True
    assert len(record.payload.trace) == 1
    assert record.payload.trace[0].agent_id == "probe-a"
    assert "EVIDENCE" in record.payload.trace[0].result


def test_worker_exception_has_no_partial_evidence(store, monkeypatch):
    """A raw mid-drive exception (``_settle`` never reached) still fails cleanly.

    Scoped deliberately: the partial-persistence fix targets ``_settle``'s
    failed-status branch (a session that ran a real turn but ended
    ``done_reason=error`` — the well-evidenced case). A ``run_sync`` exception
    that never reaches ``_settle`` at all is unchanged, preserving the existing
    ``test_worker_failure_settles_failed`` contract (event log stays exactly
    ``["run_started", "error"]``) — see this file's module docstring / the
    fleet report for the follow-up if that path also needs salvage.
    """
    _enable_scg(monkeypatch)

    class Boom(FakeRuntime):
        def run_sync(self, **kwargs):
            raise RuntimeError("boom before anything ran")

    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=Boom(_ok_transcript())
    )
    record = store.get_run("run-1")
    assert record.status == "failed"
    assert record.payload.partial is False
    assert record.payload.trace == []
    assert record.payload.results == []
    assert _event_types(store, "run-1") == ["run_started", "error"]


def test_fail_fast_has_no_partial(store, monkeypatch):
    """A pre-session fail-fast (SCG disabled) carries no partial evidence."""
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.orchestrated_runner.ScgConfig.enabled",
        staticmethod(lambda: False),
    )
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(_ok_transcript())
    )
    record = store.get_run("run-1")
    assert record.status == "failed"
    assert record.payload.partial is False
    assert record.payload.trace == []
    assert record.payload.results == []
