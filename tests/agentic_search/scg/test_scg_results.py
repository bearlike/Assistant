"""Tests for the #95 coordinator lane + ``scg_results`` result-emit mechanism.

Four workstreams, four concerns:

1. **Root-inline transcript** — a run whose root agent inlined ALL work (only
   ``tool_result`` events on the root, ZERO ``sub_agent`` events) incl. an
   ``scg_results`` emit and a completion with ``task_result``: the coordinator
   lane is streamed/reconciled, the ``result`` events + ``payload.results`` are
   populated, ``total_ms > 0``, and the synthesis metrics come from the results.
2. **Probe transcript byte-stability** — the existing probe fixtures still
   produce the same probe lanes + metrics; the coordinator lane is added only
   when the root has tool activity (these fixtures have none), so slot order is
   deterministic.
3. **``scg_results`` tool unit** — validation (``extra="forbid"``, relevance
   bounds), return shape (transcript-as-transport ``{ok, count}``).
4. **Live streamer + settle dedup** — a result streamed live is not re-emitted
   at settle (stable-id dedup).

Conventions mirror ``test_orchestrated_runner.py`` (the FakeRuntime + JSON store,
no LLM, no real runtime) and ``test_run_streamer.py`` (a real in-process bus).
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from mewbo_api.agentic_search.scg.orchestrated_runner import OrchestratedSearchRunner
from mewbo_api.agentic_search.scg.run_streamer import (
    CoordinatorTrace,
    ResultsProjection,
    RunEventStreamer,
)
from mewbo_api.agentic_search.schemas import RunRecord, Workspace, utc_now_iso
from mewbo_api.agentic_search.store import JsonAgenticSearchStore
from mewbo_core.session_event_bus import SessionEventBus
from mewbo_graph.plugins.scg.results import ScgResultsArgs, ScgResultsTool

# ---------------------------------------------------------------------------
# Shared fakes — faithful to the REAL engine event shapes (orchestrator.py /
# tool_use_loop.py): a root ``tool_result`` payload is {tool_id, operation,
# tool_input, result, success, summary, agent_id, model}.
# ---------------------------------------------------------------------------


def _tool_result(tool_id, tool_input, *, success=True, result="ok", agent_id="root"):
    """A REAL-shaped root ``tool_result`` transcript event (tool_use_loop.py)."""
    return {
        "type": "tool_result",
        "ts": utc_now_iso(),
        "payload": {
            "tool_id": tool_id,
            "operation": "execute",
            "tool_input": tool_input,
            "result": result,
            "success": success,
            "summary": result,
            "agent_id": agent_id,
            "model": "openai/gpt-5.4-nano",
        },
    }


def _completion(text, done_reason="completed", error=None):
    """The REAL engine completion payload shape (no ``text`` key)."""
    payload = {"done": True, "done_reason": done_reason, "task_result": text}
    if error is not None:
        payload["error"] = error
        payload["last_error"] = error
    return {"type": "completion", "ts": utc_now_iso(), "payload": payload}


def _results_input(entries):
    """The ``scg_results`` tool_input shape (what the tool validated + echoed)."""
    return {"results": entries}


class FakeRuntime:
    """A SessionRuntime stand-in replaying a canned transcript (no LLM)."""

    def __init__(self, transcript):
        self._transcript = list(transcript)
        self.context_events = []
        self.run_sync_kwargs = None
        self.cancel_events: dict[str, threading.Event] = {}

    def resolve_session(self, *, session_tag=None, **_):
        return "sess-scg-1"

    def append_context_event(self, session_id, context):
        self.context_events.append((session_id, context))

    def run_sync(self, **kwargs):
        self.run_sync_kwargs = kwargs
        return None

    def load_events(self, session_id, after=None):
        return list(self._transcript)

    def summarize_session(self, session_id, **_):
        completion = {}
        for rec in self._transcript:
            if rec.get("type") == "completion":
                completion = rec.get("payload") or {}
        return {
            "session_id": session_id,
            "status": "running",
            "done_reason": completion.get("done_reason"),
            "running": True,
        }

    def start_command(self, session_id, target):
        event = threading.Event()
        self.cancel_events[session_id] = event
        target(event)
        return True

    def cancel(self, session_id):
        event = self.cancel_events.get(session_id)
        if event is None:
            return False
        event.set()
        return True


def _ws():
    return Workspace(id="ws-1", name="Test WS", sources=["github", "linear"])


def _run(store, *, started_offset_s=2):
    """A ``running`` run record with a started_at in the PAST so total_ms > 0."""
    from datetime import datetime, timedelta, timezone

    started = (
        datetime.now(timezone.utc) - timedelta(seconds=started_offset_s)
    ).isoformat()
    run = RunRecord(
        run_id="run-1",
        session_id="agentic_search:run:run-1",
        workspace_id="ws-1",
        query="which issues match?",
        status="running",
        tier="fast",
        created_at=started,
        started_at=started,
        source_ids=["github", "linear"],
        allowed_tools=["github_search", "linear_search"],
    )
    store.create_run(run)
    return run


@pytest.fixture
def store(tmp_path):
    return JsonAgenticSearchStore(root_dir=tmp_path)


def _enable_scg(monkeypatch):
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.orchestrated_runner.ScgConfig.enabled",
        staticmethod(lambda: True),
    )


def _events(store, run_id="run-1", kind=None):
    evs = store.load_run_events(run_id)
    return [e for e in evs if kind is None or e.get("type") == kind]


def _types(store, run_id="run-1"):
    return [e.get("type") for e in store.load_run_events(run_id)]


# ---------------------------------------------------------------------------
# 1. Root-inline transcript — coordinator lane + results + honest metrics.
# ---------------------------------------------------------------------------


def _root_inline_transcript():
    """A root that inlined ALL work: route + memory + an scg_results emit.

    ZERO ``sub_agent`` events — the fast-tier failure mode from the issue. The
    ``scg_results`` emit carries two grounded cards; the completion carries the
    synthesized answer.
    """
    return [
        {"type": "user", "ts": utc_now_iso(), "payload": {"text": "query: ...\ntier: Fast"}},
        _tool_result("scg_route", {"query": "which issues match?", "k": 2}),
        _tool_result("scg_memory", {"operation": "read", "query": "issues"}),
        _tool_result(
            "scg_results",
            _results_input(
                [
                    {
                        "title": "Issue #12 — login fails",
                        "source": "github",
                        "snippet": "filed last week",
                        "kind": "tickets",
                        "relevance": 0.9,
                        "confidence": 0.8,
                    },
                    {
                        "title": "ENG-7 — login regression",
                        "source": "linear",
                        "snippet": "same root cause",
                        "kind": "tickets",
                        "relevance": 0.7,
                    },
                ]
            ),
        ),
        _completion("Two issues match: github#12 and linear ENG-7."),
    ]


def test_root_inline_streams_coordinator_lane_and_results(store, monkeypatch):
    """A root-inline run gets a coordinator lane + result events + payload.results.

    The #95 root cause: the streamer only handled ``sub_agent``, so a run that
    inlined all work produced trace:[], results:[], total_ms:0 next to a real
    ~3-minute elapsed. Now the root's ``tool_result`` activity becomes ONE
    coordinator lane and the ``scg_results`` emit becomes the run's results.
    """
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(_root_inline_transcript())
    )

    types = _types(store)
    # Coordinator lane opened + closed (NO probe sub_agents in this transcript).
    starts = _events(store, kind="agent_start")
    assert [e["agent_id"] for e in starts] == ["coordinator"]
    assert starts[0]["name"] == "scg-search"
    assert starts[0]["source_id"] == ""
    assert types.count("agent_done") == 1
    done = _events(store, kind="agent_done")[0]
    assert done["agent_id"] == "coordinator"
    assert done["empty"] is False  # the run produced results

    # The coordinator's digest lines are secret-free (tool_id + hint + ok), and
    # the scg_results line is the summary, NOT the entry payload.
    lines = [e["line"]["text"] for e in _events(store, kind="agent_line")]
    assert any(t.startswith("scg_route(") and t.endswith(" ok") for t in lines)
    assert "emitted 2 results" in lines
    assert all("filed last week" not in t for t in lines)  # no result snippet leak

    # The two emitted cards became ``result`` events + payload.results.
    results = _events(store, kind="result")
    assert len(results) == 2
    assert {r["result"]["source"] for r in results} == {"github", "linear"}
    assert results[0]["result"]["id"] == "r-run-1-0"  # stable id scheme
    payload = store.get_run("run-1").payload
    assert [r.source for r in payload.results] == ["github", "linear"]
    assert payload.results[0].kind == "tickets"

    # Honest total_ms: a started_at ~2s in the past → > 0 (not the old 0).
    assert payload.total_ms > 0
    done_ev = _events(store, kind="run_done")[0]
    assert done_ev["total_ms"] > 0


def test_root_inline_metrics_from_results(store, monkeypatch):
    """No probes ran → confidence = mean folded score, sources = distinct sources.

    The folded score is ``relevance`` (carrying ``confidence`` when relevance is
    absent). Two cards: 0.9 + 0.7 → mean 0.8; two distinct sources → 2.
    """
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(_root_inline_transcript())
    )

    answer = store.get_run("run-1").payload.answer
    assert answer.confidence == 0.8  # (0.9 + 0.7) / 2
    assert answer.sources_count == 2  # github + linear
    assert answer.tldr.startswith("Two issues match")
    ready = _events(store, kind="answer_ready")[0]
    assert ready["answer"]["sources_count"] == 2


# ---------------------------------------------------------------------------
# 2. Probe transcript byte-stability — coordinator lane absent, slots stable.
# ---------------------------------------------------------------------------


def _sub_agent(agent_id, action, *, detail="", status=None, summary=None):
    payload = {
        "action": action,
        "agent_id": agent_id,
        "parent_id": "root",
        "depth": 1,
        "model": "scg-path-probe",
        "detail": detail,
        "status": status or action,
    }
    if summary is not None:
        payload["summary"] = summary
    return {"type": "sub_agent", "ts": utc_now_iso(), "payload": payload}


def _probe_only_transcript():
    """The existing-shape probe transcript: two probes, NO root tool_result."""
    return [
        _sub_agent("probe-a", "start", detail="probe github#search_issues"),
        _sub_agent(
            "probe-a",
            "stop",
            status="completed",
            summary="EVIDENCE (pathway: github#search_issues): two issues last week.",
        ),
        _sub_agent("probe-b", "start", detail="probe linear#search"),
        _sub_agent(
            "probe-b",
            "stop",
            status="completed",
            summary="NO DATA on pathway linear#search for: matching issues",
        ),
        _completion("Two open issues match. [github#search_issues]"),
    ]


def test_probe_only_transcript_unchanged_no_coordinator(store, monkeypatch):
    """A pure-probe run keeps its lanes + metrics; NO coordinator lane is added.

    The coordinator lane opens only on a root ``tool_result``; a transcript with
    only ``sub_agent`` events has none, so the probe slots and the data-bearing
    metrics are exactly what #86 shipped — deterministic, byte-stable.
    """
    _enable_scg(monkeypatch)
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(_probe_only_transcript())
    )

    starts = _events(store, kind="agent_start")
    # Exactly the two probe lanes, in event order — no coordinator.
    assert [e["agent_id"] for e in starts] == ["probe-a", "probe-b"]
    assert [e["slot"] for e in starts] == [0, 1]
    assert "coordinator" not in {e["agent_id"] for e in starts}
    # No scg_results emit → no result events.
    assert _events(store, kind="result") == []

    # Metrics unchanged from #86: 1 of 2 probes data-bearing.
    answer = store.get_run("run-1").payload.answer
    assert answer.confidence == 0.5
    assert answer.sources_count == 1  # one data-bearing probe lane


def test_mixed_run_coordinator_plus_probes(store, monkeypatch):
    """A run with BOTH root tool activity AND probes: lanes interleave by order.

    The coordinator opens on the first root ``tool_result`` (slot by event
    arrival); probes keep their own lanes. Confidence stays the probe ratio (the
    coordinator is never counted as a probe); sources unions probe lanes + result
    sources.
    """
    _enable_scg(monkeypatch)
    transcript = [
        _tool_result("scg_route", {"query": "q", "k": 3}),
        _sub_agent("probe-a", "start", detail="probe github"),
        _sub_agent(
            "probe-a", "stop", status="completed",
            summary="EVIDENCE (pathway: github#search): one hit.",
        ),
        _tool_result(
            "scg_results",
            _results_input([{"title": "X", "source": "github", "relevance": 0.6}]),
        ),
        _completion("One hit. [github]"),
    ]
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(transcript)
    )

    starts = _events(store, kind="agent_start")
    # Slots reflect MERGED transcript order: the coordinator's first tool_result
    # (scg_route) precedes probe-a's spawn, so coordinator=slot 0, probe-a=slot 1.
    # The console lays lanes out by slot, so that ordinal is the determinism that
    # matters (settle may emit the probe agent_start first, but the slots are
    # event-order — exactly what a live interleaved stream would assign).
    slot_by_agent = {e["agent_id"]: e["slot"] for e in starts}
    assert slot_by_agent == {"coordinator": 0, "probe-a": 1}
    answer = store.get_run("run-1").payload.answer
    # 1 data-bearing probe / 1 probe → confidence 1.0 (NOT diluted by the
    # coordinator lane); sources = {probe-a lane} ∪ {github} → both keyed, but
    # the github result source + the probe lane are distinct grounds → 2.
    assert answer.confidence == 1.0
    assert answer.sources_count == 2


# ---------------------------------------------------------------------------
# 3. scg_results tool unit — validation + return shape.
# ---------------------------------------------------------------------------


def _step(tool_input):
    return SimpleNamespace(tool_id="scg_results", operation="execute", tool_input=tool_input)


def _run_tool(tool_input):
    tool = ScgResultsTool(session_id="sess-x")
    speaker = asyncio.run(tool.handle(_step(tool_input)))
    return ast.literal_eval(speaker.content)


def test_scg_results_validates_and_echoes_count():
    """The tool validates entries and returns ``{ok, count}`` (no store write)."""
    out = _run_tool(
        _results_input(
            [
                {"title": "A", "source": "github", "relevance": 0.5},
                {"title": "B", "source": "linear", "kind": "code", "confidence": 0.4},
            ]
        )
    )
    assert out == {"ok": True, "count": 2}


def test_scg_results_empty_is_valid():
    """An empty emit is valid (the run produced no groundable hits)."""
    assert _run_tool(_results_input([])) == {"ok": True, "count": 0}
    assert _run_tool({}) == {"ok": True, "count": 0}  # results defaults to []


def test_scg_results_forbids_extra_keys():
    """``extra="forbid"`` rejects an unknown entry key (a hallucinated field)."""
    out = _run_tool(
        _results_input([{"title": "A", "source": "github", "bogus": "x"}])
    )
    assert "error" in out and out["error"]["code"] == "validation"


def test_scg_results_relevance_bounds():
    """``relevance`` must be 0..1 — an out-of-range value is a validation error."""
    out = _run_tool(
        _results_input([{"title": "A", "source": "github", "relevance": 1.5}])
    )
    assert "error" in out and out["error"]["code"] == "validation"


def test_scg_results_requires_title_and_source():
    """``title`` + ``source`` are required, min-length 1 (ungrounded card rejected)."""
    out = _run_tool(_results_input([{"title": "A"}]))  # missing source
    assert "error" in out and out["error"]["code"] == "validation"
    out2 = _run_tool(_results_input([{"title": "", "source": "github"}]))  # blank title
    assert "error" in out2 and out2["error"]["code"] == "validation"


def test_scg_results_args_model_directly():
    """Direct model validation: defaults + kind whitelist."""
    args = ScgResultsArgs.model_validate(
        _results_input([{"title": "A", "source": "s"}])
    )
    entry = args.results[0]
    assert entry.kind == "docs"  # default
    assert entry.relevance == 0.0
    assert entry.confidence is None
    assert entry.url is None
    assert entry.meta is None  # default
    with pytest.raises(Exception):  # noqa: B017 — bad kind is a ValidationError
        ScgResultsArgs.model_validate(
            _results_input([{"title": "A", "source": "s", "kind": "nope"}])
        )


# ---------------------------------------------------------------------------
# 3b. scg_results ``meta`` + ``related_questions`` (Lane A wire contract).
#
# A card carries every quantitative/enumerable FACT in ``meta`` (stars/version/
# year…) so the snippet stays prose; the run carries 2–4 follow-up queries in
# ``related_questions`` instead of conversational "If you want, I can…" offers.
# ---------------------------------------------------------------------------


def test_scg_results_meta_accepts_scalar_facts():
    """``meta`` holds str/int/float/bool facts and survives validation."""
    args = ScgResultsArgs.model_validate(
        _results_input(
            [
                {
                    "title": "torvalds/linux",
                    "source": "github",
                    "snippet": "the Linux kernel source tree",
                    "meta": {
                        "stars": 178000,
                        "forks": 53000,
                        "language": "C",
                        "archived": False,
                        "score": 0.97,
                    },
                }
            ]
        )
    )
    meta = args.results[0].meta
    assert meta == {
        "stars": 178000,
        "forks": 53000,
        "language": "C",
        "archived": False,
        "score": 0.97,
    }


def test_scg_results_meta_rejects_too_many_keys():
    """>12 keys is an over-budget fingerprint → validation error (model retries)."""
    out = _run_tool(
        _results_input(
            [
                {
                    "title": "A",
                    "source": "github",
                    "meta": {f"k{i}": i for i in range(13)},
                }
            ]
        )
    )
    assert "error" in out and out["error"]["code"] == "validation"


def test_scg_results_meta_rejects_long_string_value():
    """A string value over 200 chars is rejected (no silent truncation)."""
    out = _run_tool(
        _results_input(
            [{"title": "A", "source": "github", "meta": {"note": "x" * 201}}]
        )
    )
    assert "error" in out and out["error"]["code"] == "validation"


def test_scg_results_meta_rejects_long_key():
    """A meta key over 40 chars is rejected."""
    out = _run_tool(
        _results_input(
            [{"title": "A", "source": "github", "meta": {"k" * 41: 1}}]
        )
    )
    assert "error" in out and out["error"]["code"] == "validation"


def test_scg_results_meta_rejects_nested_value():
    """A non-scalar meta value (dict/list) is rejected — facts, not structure."""
    out = _run_tool(
        _results_input(
            [{"title": "A", "source": "github", "meta": {"k": {"nested": 1}}}]
        )
    )
    assert "error" in out and out["error"]["code"] == "validation"


def test_scg_results_related_questions_accepted():
    """Run-level ``related_questions`` (≤5) validate alongside results."""
    args = ScgResultsArgs.model_validate(
        {
            "results": [{"title": "A", "source": "github"}],
            "related_questions": [
                "What are its most active forks?",
                "Which maintainers merged recently?",
            ],
        }
    )
    assert args.related_questions == [
        "What are its most active forks?",
        "Which maintainers merged recently?",
    ]


def test_scg_results_related_questions_caps():
    """>5 questions OR a >140-char question is a validation error."""
    out_count = _run_tool(
        {"related_questions": [f"q{i}?" for i in range(6)]}
    )
    assert "error" in out_count and out_count["error"]["code"] == "validation"
    out_len = _run_tool({"related_questions": ["x" * 141]})
    assert "error" in out_len and out_len["error"]["code"] == "validation"


def test_scg_results_extra_top_level_key_still_forbidden():
    """``extra="forbid"`` is intact — a hallucinated top-level key is rejected."""
    out = _run_tool({"results": [], "bogus_top": 1})
    assert "error" in out and out["error"]["code"] == "validation"


# ---------------------------------------------------------------------------
# 4. Live streamer + settle dedup — no doubled result events.
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _run_streamer_record(store):
    run = RunRecord(
        run_id="run-1",
        session_id="sess-1",
        workspace_id="ws-1",
        query="q",
        status="running",
        created_at=utc_now_iso(),
        started_at=utc_now_iso(),
    )
    store.create_run(run)
    return run


def test_live_result_not_re_emitted_at_settle(store):
    """A result streamed live is NOT re-emitted by settle reconciliation.

    The stable id (``r-<run_id8>-<n>``) is the dedup key: ``_emit_results`` skips
    an id already on the wire, so ``reconcile_results`` over the SAME parsed
    entries is a no-op — exactly the consulted-state pattern the lanes use.
    """
    _run_streamer_record(store)
    bus = SessionEventBus()
    streamer = RunEventStreamer(run_id="run-1", store=store, bus=bus)
    streamer.subscribe("sess-1")
    streamer.start()
    try:
        bus.publish(
            "sess-1",
            _tool_result(
                "scg_results",
                _results_input([{"title": "A", "source": "github", "relevance": 0.5}]),
            ),
        )
        assert _wait_until(lambda: _types(store).count("result") == 1)
    finally:
        streamer.stop()

    # Settle reconciles the SAME entries (as _build_results would) — no double.
    from mewbo_api.agentic_search.scg.run_streamer import ResultsProjection as RP

    payload = _tool_result(
        "scg_results",
        _results_input([{"title": "A", "source": "github", "relevance": 0.5}]),
    )["payload"]
    streamer.reconcile_results(RP.parse("run-1", payload))
    assert _types(store).count("result") == 1  # still ONE — deduped by stable id

    # The coordinator lane streamed live; settle's agent_done closes it once.
    assert streamer.coordinator_opened() is True
    streamer.reconcile_coordinator([], has_data=True)
    assert _types(store).count("agent_done") == 1
    # A second reconcile is idempotent (done flag) — no second agent_done.
    streamer.reconcile_coordinator([], has_data=True)
    assert _types(store).count("agent_done") == 1


def test_coordinator_trace_line_is_secret_free():
    """``CoordinatorTrace.line`` never echoes the raw result; only tool_id+hint+ok."""
    payload = {
        "tool_id": "scg_route",
        "tool_input": {"query": "who owns billing", "k": 3},
        "result": "SECRET-TOKEN-abc123 and other sensitive data",
        "success": True,
    }
    line = CoordinatorTrace.line(payload)
    assert "SECRET-TOKEN" not in line.text
    assert line.text.startswith("scg_route(")
    assert line.text.endswith(" ok")
    assert "query=who owns billing" in line.text

    # An error marker + the scg_results summary line.
    err = CoordinatorTrace.line(
        {"tool_id": "scg_route", "tool_input": {}, "success": False}
    )
    assert err.text == "scg_route error"
    summary = CoordinatorTrace.line(
        {"tool_id": "scg_results", "tool_input": {"results": [1, 2, 3]}, "success": True}
    )
    assert summary.text == "emitted 3 results"


def test_results_projection_drops_malformed_entries():
    """A malformed entry is dropped, not fatal; valid entries still project.

    The lenient read side: a non-dict entry (caught by the type guard) and an
    entry whose ``kind`` isn't in the wire ``ResultKindLiteral`` (caught by the
    ``SearchResult`` validation) are both skipped; the valid entry still projects
    with its positionally-stable id.
    """
    payload = {
        "tool_id": "scg_results",
        "tool_input": {
            "results": [
                {"title": "good", "source": "github", "relevance": 0.5},
                "not-a-dict",
                {"title": "bad", "source": "linear", "kind": "not-a-real-kind"},
            ]
        },
    }
    out = ResultsProjection.parse("run-1", payload)
    # Only the one valid entry survives; its id is positionally stable.
    assert len(out) == 1
    assert out[0].title == "good"
    assert out[0].id == "r-run-1-0"


# ---------------------------------------------------------------------------
# 5. Probe-emitted result cards (#102) — agent-aware projection.
#
# A child loop INHERITS the parent's event_logger (core AgentContext.child), so
# probe tool_results ride THIS session's transcript/bus stamped with the probe's
# agent_id (the #95 "probes run in their own sessions" premise was wrong —
# verified live). These tests lock the corrected classification: a probe's
# scg_results emit becomes attributed result cards; its other tool calls never
# pollute the coordinator lane; live and settle mint identical probe-salted ids.
# ---------------------------------------------------------------------------


def test_probe_emit_streams_attributed_results_live(store):
    """A probe's live ``scg_results`` emit becomes probe-salted result events.

    The probe lane is known before its first tool call (the spawn's ``start``
    precedes the child loop), so the streamer classifies the tool_result by
    ``agent_id`` ∈ probe lanes: the emit projects with ``r-<run8>-<agent8>-<n>``
    ids, the coordinator lane never opens, and a non-results probe tool call
    emits nothing (the lane stays lifecycle-only — #86).
    """
    _run_streamer_record(store)
    bus = SessionEventBus()
    streamer = RunEventStreamer(run_id="run-1", store=store, bus=bus)
    streamer.subscribe("sess-1")
    streamer.start()
    try:
        bus.publish("sess-1", _sub_agent("probe-a", "start", detail="probe github"))
        # A probe connector call: classified as probe → projected NOWHERE.
        bus.publish(
            "sess-1",
            _tool_result("mcp_github_search", {"q": "login"}, agent_id="probe-a"),
        )
        # The probe's result emit: attributed, probe-salted ids, confidence wired.
        bus.publish(
            "sess-1",
            _tool_result(
                "scg_results",
                _results_input(
                    [
                        {
                            "title": "Issue #12",
                            "source": "github",
                            "relevance": 0.9,
                            "confidence": 0.7,
                        }
                    ]
                ),
                agent_id="probe-a",
            ),
        )
        assert _wait_until(lambda: _types(store).count("result") == 1)
    finally:
        streamer.stop()

    results = _events(store, kind="result")
    assert results[0]["result"]["id"] == "r-run-1-probe-a-0"
    assert results[0]["result"]["confidence"] == 0.7
    assert results[0]["result"]["relevance"] == 0.9
    # No coordinator lane (no ROOT tool_result arrived), and the probe's
    # connector call produced no digest line beyond its lifecycle line.
    starts = _events(store, kind="agent_start")
    assert [e["agent_id"] for e in starts] == ["probe-a"]
    assert streamer.coordinator_opened() is False

    # Settle parity: the SAME transcript event parses to the SAME id → deduped.
    payload = _tool_result(
        "scg_results",
        _results_input(
            [{"title": "Issue #12", "source": "github", "relevance": 0.9,
              "confidence": 0.7}]
        ),
        agent_id="probe-a",
    )["payload"]
    streamer.reconcile_results(
        ResultsProjection.parse("run-1", payload, emitter="probe-a")
    )
    assert _types(store).count("result") == 1  # still ONE


def test_probe_tool_results_never_pollute_coordinator(store, monkeypatch):
    """Settle classifies tool_results by probe lane — the #95 mislabel is fixed.

    A mixed transcript (root scg_route + a probe's connector calls): the
    coordinator lane digests ONLY the root's tool call; the probe's calls are
    excluded from its lines AND from the lane-slot scan, exactly as the live
    streamer classifies them.
    """
    _enable_scg(monkeypatch)
    transcript = [
        _tool_result("scg_route", {"query": "q", "k": 2}, agent_id="root"),
        _sub_agent("probe-a", "start", detail="probe github"),
        _tool_result("mcp_github_search", {"q": "login"}, agent_id="probe-a"),
        _tool_result("mcp_github_get_issue", {"id": 12}, agent_id="probe-a"),
        _sub_agent(
            "probe-a", "stop", status="completed",
            summary="EVIDENCE (pathway: github#search): one hit.",
        ),
        _completion("One hit. [github]"),
    ]
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(transcript)
    )

    coord_lines = [
        e["line"]["text"]
        for e in _events(store, kind="agent_line")
        if e["agent_id"] == "coordinator"
    ]
    assert any(t.startswith("scg_route(") for t in coord_lines)
    assert not any("mcp_github" in t for t in coord_lines)
    # Slots: coordinator first (root scg_route precedes the spawn), probe second.
    slot_by_agent = {
        e["agent_id"]: e["slot"] for e in _events(store, kind="agent_start")
    }
    assert slot_by_agent == {"coordinator": 0, "probe-a": 1}


def test_mixed_root_and_probe_emits_no_id_collision(store, monkeypatch):
    """Root + probe emits coexist: probe-salted ids keep both sets of cards.

    Before #102 a probe emit minted the SAME ``r-<run8>-<n>`` ids as the root's
    → the dedup silently dropped one set. Now the probe's cards carry the agent
    suffix, both survive, and the metrics' source union sees both sources.
    """
    _enable_scg(monkeypatch)
    transcript = [
        _sub_agent("probe-a", "start", detail="probe github"),
        _tool_result(
            "scg_results",
            _results_input(
                [{"title": "Issue #12", "source": "github", "relevance": 0.8}]
            ),
            agent_id="probe-a",
        ),
        _sub_agent(
            "probe-a", "stop", status="completed",
            summary="EVIDENCE (pathway: github#search): issue 12.",
        ),
        _tool_result(
            "scg_results",
            _results_input(
                [{"title": "ENG-7", "source": "linear", "confidence": 0.6}]
            ),
            agent_id="root",
        ),
        _completion("Two hits. [github, linear]"),
    ]
    OrchestratedSearchRunner().start(
        _run(store), _ws(), store=store, runtime=FakeRuntime(transcript)
    )

    payload = store.get_run("run-1").payload
    ids = [r.id for r in payload.results]
    assert ids == ["r-run-1-probe-a-0", "r-run-1-0"]  # transcript order, no clash
    by_id = {r.id: r for r in payload.results}
    # The probe's explicit rank is kept; the root entry's confidence both folds
    # into relevance (no explicit rank) AND rides the wire verbatim.
    assert by_id["r-run-1-probe-a-0"].relevance == 0.8
    assert by_id["r-run-1-probe-a-0"].confidence is None
    assert by_id["r-run-1-0"].relevance == 0.6
    assert by_id["r-run-1-0"].confidence == 0.6
    # Metrics: 1 data-bearing probe / 1 probe → 1.0; sources = probe lane ∪
    # {github, linear} result sources → 3 distinct grounds.
    answer = payload.answer
    assert answer.confidence == 1.0
    assert answer.sources_count == 3


def test_confidence_rides_the_wire_beside_an_explicit_rank():
    """``confidence`` is surfaced verbatim and never overwrites ``relevance``."""
    payload = {
        "tool_id": "scg_results",
        "tool_input": _results_input(
            [
                {"title": "A", "source": "github", "relevance": 0.9,
                 "confidence": 0.4},
                {"title": "B", "source": "github", "confidence": 0.5},
                {"title": "C", "source": "github"},
            ]
        ),
    }
    out = ResultsProjection.parse("run-1", payload)
    assert (out[0].relevance, out[0].confidence) == (0.9, 0.4)  # rank kept
    assert (out[1].relevance, out[1].confidence) == (0.5, 0.5)  # folded + wired
    assert (out[2].relevance, out[2].confidence) == (0.0, None)  # nothing invented
