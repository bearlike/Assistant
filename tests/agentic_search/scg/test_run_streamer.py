"""Tests for :class:`RunEventStreamer` — the #77 LIVE run-event projector.

The root-cause fix for "the console sits on Starting search… for the whole run":
the streamer subscribes to the backing session's ``SessionEventBus`` and projects
each ``sub_agent`` event onto the run event log AS it is published — not in one
end-of-run burst. These tests drive a real (in-process) bus + a real JSON run
store, publish events with the consumer running, and assert the run log grows
WHILE the "drive" is still in flight.
"""

from __future__ import annotations

import time

import pytest
from mewbo_api.agentic_search.scg.run_streamer import ProbeTrace, RunEventStreamer
from mewbo_api.agentic_search.schemas import RunRecord, TraceAgent, TraceLine, utc_now_iso
from mewbo_api.agentic_search.store import JsonAgenticSearchStore
from mewbo_core.session_event_bus import SessionEventBus


@pytest.fixture
def store(tmp_path) -> JsonAgenticSearchStore:
    return JsonAgenticSearchStore(root_dir=tmp_path)


def _run(store: JsonAgenticSearchStore) -> RunRecord:
    now = utc_now_iso()
    run = RunRecord(
        run_id="run-1",
        session_id="sess-1",
        workspace_id="ws-1",
        query="q",
        status="running",
        created_at=now,
        started_at=now,
    )
    store.create_run(run)
    return run


def _sub_agent(agent_id: str, action: str, *, detail: str = "", status: str | None = None):
    return {
        "type": "sub_agent",
        "payload": {
            "action": action,
            "agent_id": agent_id,
            "parent_id": "root",
            "model": "scg-path-probe",
            "detail": detail,
            "status": status or action,
        },
    }


def _types(store: JsonAgenticSearchStore, run_id: str = "run-1") -> list[str]:
    return [e.get("type") for e in store.load_run_events(run_id)]


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_events_appear_live_during_the_run(store) -> None:
    """A probe's events land on the run log AS published — not at the end.

    Drives the bus the way a real session would: publish ``start`` mid-run, assert
    the run log already carries ``agent_start`` BEFORE the run completes, then
    publish ``stop`` and assert ``agent_done`` lands live too.
    """
    _run(store)
    bus = SessionEventBus()
    streamer = RunEventStreamer(run_id="run-1", store=store, bus=bus)
    streamer.subscribe("sess-1")
    streamer.start()
    try:
        # First probe starts — published while the "drive" is still running.
        bus.publish("sess-1", _sub_agent("probe-a", "start", detail="probe github#issues"))
        assert _wait_until(lambda: "agent_start" in _types(store)), (
            "agent_start must appear LIVE — the run is still in flight, not settled"
        )
        # The lane opened with the probe's pathway, not a bare header.
        line = [e for e in store.load_run_events("run-1") if e.get("type") == "agent_line"][0]
        assert "probe github#issues" in line["line"]["text"]

        # Probe stops — agent_done lands live too.
        bus.publish("sess-1", _sub_agent("probe-a", "stop", detail="found 2", status="completed"))
        assert _wait_until(
            lambda: "agent_done" in _types(store)
        ), "agent_done must appear live on the stop event"
    finally:
        streamer.stop()

    types = _types(store)
    assert types.count("agent_start") == 1
    assert types.count("agent_done") == 1


def test_reconcile_flushes_missing_agents_only(store) -> None:
    """Settle reconciliation emits only agents the live stream did not stream.

    A fast run whose ``sub_agent`` events were never drained live: the streamer
    streamed ``probe-a`` but ``probe-b`` only exists in the settle-time trace.
    ``reconcile_missing`` must flush ``probe-b`` (full start→line→done) and leave
    ``probe-a`` untouched — no duplicates.
    """
    _run(store)
    bus = SessionEventBus()
    streamer = RunEventStreamer(run_id="run-1", store=store, bus=bus)
    streamer.subscribe("sess-1")
    streamer.start()
    bus.publish("sess-1", _sub_agent("probe-a", "start", detail="a"))
    bus.publish("sess-1", _sub_agent("probe-a", "stop", detail="done a", status="completed"))
    assert _wait_until(lambda: _types(store).count("agent_done") == 1)
    streamer.stop()

    # The settle-time trace knows about BOTH probes.
    trace = [
        TraceAgent(
            id="probe-a", agent_id="probe-a", name="scg-path-probe", source_id="root", slot=0,
            lines=[TraceLine(t_ms=0, text="done a", done=True)],
        ),
        TraceAgent(
            id="probe-b", agent_id="probe-b", name="scg-path-probe", source_id="root", slot=1,
            lines=[TraceLine(t_ms=0, text="b never streamed", done=True)],
        ),
    ]
    streamer.reconcile_missing(trace)

    starts = [
        e for e in store.load_run_events("run-1") if e.get("type") == "agent_start"
    ]
    # probe-a streamed live; probe-b reconciled — exactly one start each, no dup.
    assert {e["agent_id"] for e in starts} == {"probe-a", "probe-b"}
    assert len(starts) == 2


def test_probe_trace_projection_condenses_start_brief() -> None:
    """``ProbeTrace`` opens a lane with the brief's first substantive line."""
    payload = {
        "action": "start",
        "agent_id": "p",
        "model": "scg-path-probe",
        "detail": "\n\nProbe ONE pathway\nSUB-QUERY: who owns billing\n...",
    }
    line = ProbeTrace.line(payload)
    assert line.text == "Probe ONE pathway"  # first non-empty line, not the blank head
    assert line.done is False
    stop = ProbeTrace.line({"action": "stop", "agent_id": "p", "detail": "found owner"})
    assert stop.text == "found owner" and stop.done is True
