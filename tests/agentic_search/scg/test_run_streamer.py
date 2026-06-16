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
from mewbo_api.agentic_search.scg.run_streamer import (
    ProbeTrace,
    ResultsProjection,
    RunEventStreamer,
)
from mewbo_api.agentic_search.schemas import (
    RunRecord,
    SearchResult,
    TraceAgent,
    TraceLine,
    utc_now_iso,
)
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
    """``ProbeTrace`` opens a lane with the SUB-QUERY line, not boilerplate.

    The brief leads with the leaf-executor SYSTEM PROMPT (run-797097e4b1: the
    real ``SUB-QUERY:`` lands ~7 KB in), so the lane must skip the boilerplate
    header and surface the probe's actual target.
    """
    payload = {
        "action": "start",
        "agent_id": "p",
        "agent_type": "scg-path-probe",
        "model": "claude-haiku-4-5",
        "detail": (
            "You are an scg-path-probe leaf executor. Follow the playbook.\n\n"
            "Probe ONE pathway\nSUB-QUERY: who owns billing\nPATHWAY: github#x\n"
        ),
    }
    line = ProbeTrace.line(payload)
    # The SUB-QUERY marker wins over the boilerplate header AND the generic
    # "Probe ONE pathway" line — it is the probe's actual target.
    assert line.text == "SUB-QUERY: who owns billing"
    assert line.done is False
    stop = ProbeTrace.line({"action": "stop", "agent_id": "p", "detail": "found owner"})
    assert stop.text == "found owner" and stop.done is True


def test_probe_trace_skips_boilerplate_without_sub_query() -> None:
    """With no SUB-QUERY/PATHWAY marker, the boilerplate header is still skipped."""
    payload = {
        "action": "start",
        "agent_id": "p",
        "detail": "You are an scg-path-probe leaf executor.\nProbe github#issues now",
    }
    assert ProbeTrace.line(payload).text == "Probe github#issues now"


def test_lane_name_is_agent_type_not_model() -> None:
    """The lane name is the agent KIND (``agent_type``), never the model string.

    Regression (run-797097e4b1): ``lane_name`` returned ``payload["model"]`` so
    every lane was labelled by its model (``claude-haiku-4-5``). It now reads
    ``agent_type`` (Lane A), falling back to the literal kind — never the model.
    """
    from mewbo_api.agentic_search.scg.run_streamer import ProbeTrace as PT

    with_type = {"agent_type": "scg-path-probe", "model": "claude-haiku-4-5"}
    assert PT.lane_name(with_type) == "scg-path-probe"
    assert PT.model(with_type) == "claude-haiku-4-5"
    # No agent_type (pre-Lane-A) → literal kind fallback, NEVER the model.
    legacy = {"model": "claude-haiku-4-5"}
    assert PT.lane_name(legacy) == "scg-path-probe"
    assert PT.model(legacy) == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# ResultsProjection — meta passthrough, dedup key, related_questions.
# ---------------------------------------------------------------------------


def _results_event(entries, *, agent_id="root", related=None):
    """An ``scg_results`` tool_result transcript event (transcript-as-transport)."""
    tool_input: dict = {"results": entries}
    if related is not None:
        tool_input["related_questions"] = related
    return {
        "type": "tool_result",
        "ts": utc_now_iso(),
        "payload": {
            "tool_id": "scg_results",
            "tool_input": tool_input,
            "success": True,
            "agent_id": agent_id,
        },
    }


def test_meta_passthrough_keeps_scalars_drops_blobs() -> None:
    """An entry's ``meta`` rides verbatim — SCALARS only, non-scalars dropped."""
    entry = {
        "source": "github",
        "kind": "code",
        "title": "repo",
        "url": "https://github.com/a/b",
        "meta": {
            "stars": 1200,
            "language": "Go",
            "archived": False,
            "owners": ["a", "b"],          # non-scalar → dropped
            "raw": {"token": "secret"},     # non-scalar blob → dropped
        },
    }
    [result] = ResultsProjection.parse("run-1", _results_event([entry])["payload"])
    assert result.meta == {"stars": 1200, "language": "Go", "archived": False}
    assert "owners" not in result.meta and "raw" not in result.meta


def test_meta_all_non_scalar_collapses_to_none() -> None:
    """A meta dict with no scalar values projects to ``None`` (suppressed)."""
    entry = {
        "source": "x", "kind": "docs", "title": "t",
        "meta": {"blob": {"k": "v"}, "list": [1, 2]},
    }
    [result] = ResultsProjection.parse("run-1", _results_event([entry])["payload"])
    assert result.meta is None


def test_dedup_key_url_collapses_scheme_and_trailing_slash() -> None:
    """The dedup key normalizes scheme + trailing slash + host case."""
    a = SearchResult(id="1", source="gh", kind="code", title="t",
                     url="https://GitHub.com/a/b/")
    b = SearchResult(id="2", source="gh", kind="docs", title="other",
                     url="http://github.com/a/b")
    assert ResultsProjection.dedup_key(a) == ResultsProjection.dedup_key(b)


def test_dedup_key_falls_back_to_title_source_without_url() -> None:
    """With no url, identity is normalized title + source."""
    a = SearchResult(id="1", source="GH", kind="code", title="The  Repo")
    b = SearchResult(id="2", source="gh", kind="docs", title="the repo")
    assert ResultsProjection.dedup_key(a) == ResultsProjection.dedup_key(b)


def test_related_questions_strings_only() -> None:
    """``related_questions`` reads the top-level list, dropping non-strings/blanks."""
    payload = _results_event(
        [], related=["how is auth wired?", "", 42, "what about rate limits?"]
    )["payload"]
    assert ResultsProjection.related_questions(payload) == [
        "how is auth wired?",
        "what about rate limits?",
    ]


# ---------------------------------------------------------------------------
# RunEventStreamer — dedup across emitters, probe digests, per-lane credit.
# ---------------------------------------------------------------------------


def _live_streamer(store):
    bus = SessionEventBus()
    streamer = RunEventStreamer(run_id="run-1", store=store, bus=bus)
    streamer.subscribe("sess-1")
    streamer.start()
    return bus, streamer


def _results(store, run_id="run-1"):
    return [
        e["result"]
        for e in store.load_run_events(run_id)
        if e.get("type") == "result"
    ]


def test_root_reemit_without_url_loses_to_probe_card(store) -> None:
    """A probe emits a url-bearing card; the root re-emits the same hit url-less.

    The EVIDENCE (run-797097e4b1): the root reconstructed the probe's repos from
    prose with NO url (different stable id, kind flipped to docs) → 5 cards for 3
    results. The semantic dedup (normalized url, else title+source) collapses the
    root's re-emit into the probe's card — FIRST emission wins.
    """
    _run(store)
    bus, streamer = _live_streamer(store)
    try:
        # The probe lane must exist before its tool_result classifies onto it.
        bus.publish("sess-1", _sub_agent("probe-a", "start", detail="probe github"))
        assert _wait_until(lambda: "agent_start" in _types(store))
        # Probe emits a url-bearing code card.
        bus.publish(
            "sess-1",
            _results_event(
                [{"source": "github", "kind": "code", "title": "acme/widgets",
                  "url": "https://github.com/acme/widgets"}],
                agent_id="probe-a",
            ),
        )
        assert _wait_until(lambda: len(_results(store)) == 1)
        # The ROOT re-emits the SAME repo reconstructed from prose: no url, docs.
        bus.publish(
            "sess-1",
            _results_event(
                [{"source": "github", "kind": "docs", "title": "acme/widgets"}],
                agent_id="root",
            ),
        )
        # Give the consumer a beat; the count must STAY 1 (the re-emit deduped).
        time.sleep(0.2)
        assert len(_results(store)) == 1
    finally:
        streamer.stop()


def test_probe_tool_digests_appear_payloads_do_not(store) -> None:
    """A probe's non-results tool call → a digest line; its result payload never.

    The EVIDENCE: a probe's only real data fetch
    (``mcp_github_search_repositories``) was dropped entirely. It now projects a
    secret-free digest line onto the probe's lane (tool_id + input hint), never
    the result payload.
    """
    _run(store)
    bus, streamer = _live_streamer(store)
    try:
        bus.publish("sess-1", _sub_agent("probe-a", "start", detail="probe github"))
        assert _wait_until(lambda: "agent_start" in _types(store))
        bus.publish(
            "sess-1",
            {
                "type": "tool_result",
                "ts": utc_now_iso(),
                "payload": {
                    "tool_id": "mcp_github_search_repositories",
                    "tool_input": {"query": "widgets", "k": 5},
                    "result": "SECRET-TOKEN-xyz repo dump",
                    "success": True,
                    "agent_id": "probe-a",
                },
            },
        )
        assert _wait_until(
            lambda: any(
                "mcp_github_search_repositories" in (e.get("line") or {}).get("text", "")
                for e in store.load_run_events("run-1")
                if e.get("type") == "agent_line"
            )
        )
        # The digest carries the tool_id + input hint, NEVER the result payload.
        digest = [
            e for e in store.load_run_events("run-1")
            if e.get("type") == "agent_line"
            and "mcp_github_search_repositories" in e["line"]["text"]
        ][0]
        assert "query=widgets" in digest["line"]["text"]
        assert "SECRET-TOKEN" not in digest["line"]["text"]
    finally:
        streamer.stop()


def test_probe_agent_done_credits_true_results_count(store) -> None:
    """A probe that emits 3 cards reports ``results_count=3`` on its agent_done.

    The EVIDENCE: probe ``agent_done`` said ``results_count=0`` despite 3 cards.
    """
    _run(store)
    bus, streamer = _live_streamer(store)
    try:
        bus.publish("sess-1", _sub_agent("probe-a", "start", detail="probe github"))
        assert _wait_until(lambda: "agent_start" in _types(store))
        bus.publish(
            "sess-1",
            _results_event(
                [
                    {"source": "github", "kind": "code", "title": f"r{n}",
                     "url": f"https://github.com/acme/r{n}"}
                    for n in range(3)
                ],
                agent_id="probe-a",
            ),
        )
        assert _wait_until(lambda: len(_results(store)) == 3)
        bus.publish(
            "sess-1",
            _sub_agent("probe-a", "stop", detail="completed", status="completed"),
        )
        assert _wait_until(lambda: "agent_done" in _types(store))
    finally:
        streamer.stop()
    done = [e for e in store.load_run_events("run-1") if e.get("type") == "agent_done"][0]
    assert done["agent_id"] == "probe-a"
    assert done["results_count"] == 3
    # No duplicates here, so the raw returned count equals the kept count.
    assert done["returned_count"] == 3
