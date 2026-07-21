#!/usr/bin/env python3
"""Tests for AgentTranscriptHub — the order-preserving transcript source.

The acceptance bar (the ORDERING INVARIANT): within one agent's transcript the
materialised order MUST equal core emission order — text → tool start → tool
result → text → spawn — with no bucketing, reordering, or collapsing. The
headline test feeds a scripted interleaved event sequence and asserts the
materialised order is identical. Other tests cover delta coalescing, the mutable
tool card (running → settled with elapsed), the no-pre error path, sub-agent
demux + spawn placement, token rollups, and the live RootSink call order.
"""

from __future__ import annotations

import threading
from typing import Any

from mewbo_cli.tui.agent_transcript_hub import (
    AgentTranscriptHub,
    FleetRow,
    Spawn,
    TextSpan,
    ToolCall,
)
from mewbo_cli.tui.seams import TranscriptItem

ROOT = "agent-root"
CHILD = "agent-child"


# --- fakes ---------------------------------------------------------------


class _FakeStep:
    """A minimal ActionStep stand-in for the pre_tool_use hook."""

    def __init__(self, tool_id: str, tool_input: Any, operation: str = "execute") -> None:
        self.tool_id = tool_id
        self.tool_input = tool_input
        self.operation = operation


class _RecordingSink:
    """Records every RootSink call so tests can assert live render order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def stream_delta(self, stream_id: str, delta: str) -> None:
        self.calls.append(("stream_delta", (stream_id, delta)))

    def stream_end(self, stream_id: str) -> None:
        self.calls.append(("stream_end", stream_id))

    def upsert_tool(self, card_id: str, item: TranscriptItem) -> None:
        self.calls.append(("upsert_tool", (card_id, item)))

    def spawn(self, item: TranscriptItem) -> None:
        self.calls.append(("spawn", item))

    def set_status(self, label: str) -> None:
        self.calls.append(("set_status", label))


class _Clock:
    """A deterministic monotonic clock so elapsed assertions are stable."""

    def __init__(self) -> None:
        self.t = 0.0

    def tick(self, dt: float = 1.0) -> None:
        self.t += dt

    def __call__(self) -> float:
        return self.t


# --- helpers -------------------------------------------------------------


def _delta(hub: AgentTranscriptHub, text: str, *, agent: str = ROOT, depth: int = 0,
           step: int = 0, sid: str = "s") -> None:
    hub.observe(
        sid,
        {"type": "agent_message_delta",
         "payload": {"text": text, "agent_id": agent, "depth": depth, "step": step}},
    )


def _tool_result(hub: AgentTranscriptHub, tool_id: str, tool_input: Any, *,
                 agent: str = ROOT, depth: int = 0, success: bool = True,
                 result: str = "ok", sid: str = "s") -> None:
    hub.observe(
        sid,
        {"type": "tool_result",
         "payload": {"tool_id": tool_id, "operation": "execute",
                     "tool_input": tool_input, "result": result,
                     "success": success, "agent_id": agent, "depth": depth}},
    )


def _kinds(hub: AgentTranscriptHub, agent: str = ROOT) -> list[str]:
    t = hub.transcript(agent)
    assert t is not None
    return [e.kind for e in t.entries]


# --- THE ordering invariant ---------------------------------------------


def test_ordering_invariant_is_exact_emission_order() -> None:
    """text → toolA start → toolA result → text → spawn child → toolC → result.

    Asserts the root transcript materialises in EXACT arrival order, never
    bucketed by type.
    """
    hub = AgentTranscriptHub()
    hub.set_active_session("s")
    # establish the root
    hub.observe("s", {"type": "llm_call_start",
                      "payload": {"agent_id": ROOT, "depth": 0, "step": 0}})

    _delta(hub, "Hello ", step=0)
    _delta(hub, "world", step=0)

    stepA = _FakeStep("bash", {"command": "ls"})
    hub.tool_started(stepA)
    _tool_result(hub, "bash", {"command": "ls"})

    _delta(hub, "more text", step=1)

    hub.observe("s", {"type": "sub_agent",
                      "payload": {"action": "start", "agent_id": CHILD,
                                  "parent_id": ROOT, "depth": 1,
                                  "status": "running", "agent_type": "researcher"}})

    stepC = _FakeStep("read", {"file_path": "/x"})
    hub.tool_started(stepC)
    _tool_result(hub, "read", {"file_path": "/x"})

    assert _kinds(hub) == ["text", "tool", "text", "spawn", "tool"]

    entries = hub.transcript(ROOT).entries  # type: ignore[union-attr]
    assert isinstance(entries[0], TextSpan) and entries[0].text == "Hello world"
    assert isinstance(entries[1], ToolCall) and entries[1].tool_id == "bash"
    assert isinstance(entries[2], TextSpan) and entries[2].text == "more text"
    assert isinstance(entries[3], Spawn) and entries[3].child_id == CHILD
    assert isinstance(entries[4], ToolCall) and entries[4].tool_id == "read"


def test_ordering_holds_across_turns() -> None:
    """A second turn appends after the first — never reordered or collapsed."""
    hub = AgentTranscriptHub()
    hub.set_active_session("s")
    hub.observe("s", {"type": "llm_call_start",
                      "payload": {"agent_id": ROOT, "depth": 0, "step": 0}})
    _delta(hub, "turn one", step=0)
    hub.observe("s", {"type": "agent_message",
                      "payload": {"agent_id": ROOT, "depth": 0, "text": "turn one"}})
    # second turn (same root agent id, later step)
    _delta(hub, "turn two", step=5)
    hub.tool_started(_FakeStep("bash", {"command": "echo"}))
    _tool_result(hub, "bash", {"command": "echo"})
    assert _kinds(hub) == ["text", "text", "tool"]


# --- delta coalescing ----------------------------------------------------


def test_consecutive_deltas_coalesce_into_one_span() -> None:
    hub = AgentTranscriptHub()
    _delta(hub, "a")
    _delta(hub, "b")
    _delta(hub, "c")
    assert _kinds(hub) == ["text"]
    assert hub.transcript(ROOT).entries[0].text == "abc"  # type: ignore[union-attr]


def test_new_step_opens_a_new_span() -> None:
    hub = AgentTranscriptHub()
    _delta(hub, "step0", step=0)
    _delta(hub, "step1", step=1)
    assert _kinds(hub) == ["text", "text"]


# --- the mutable tool card ----------------------------------------------


def test_tool_card_is_one_mutable_item_running_then_settled() -> None:
    clock = _Clock()
    hub = AgentTranscriptHub(clock=clock)
    hub.observe("anything", {"type": "llm_call_start",
                             "payload": {"agent_id": ROOT, "depth": 0}})
    hub.tool_started(_FakeStep("bash", {"command": "ls"}))
    entries = hub.transcript(ROOT).entries  # type: ignore[union-attr]
    assert len(entries) == 1
    card = entries[0]
    assert isinstance(card, ToolCall) and card.status == "running"
    clock.tick(1.5)
    _tool_result(hub, "bash", {"command": "ls"})
    # STILL one entry — mutated in place, not appended.
    assert len(hub.transcript(ROOT).entries) == 1  # type: ignore[union-attr]
    assert card.status == "done"
    assert card.result == "ok"
    assert card.elapsed == 1.5


def test_failed_tool_settles_to_error() -> None:
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.tool_started(_FakeStep("bash", {"command": "boom"}))
    _tool_result(hub, "bash", {"command": "boom"}, success=False, result="kaboom")
    card = hub.transcript(ROOT).entries[0]  # type: ignore[union-attr]
    assert card.status == "error"
    assert card.result == "kaboom"


def test_tool_result_without_pre_creates_settled_card() -> None:
    """An error path that never fired pre_tool_use still renders a settled card."""
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    _tool_result(hub, "edit", {"file_path": "/x"}, success=False, result="denied")
    entries = hub.transcript(ROOT).entries  # type: ignore[union-attr]
    assert len(entries) == 1
    assert entries[0].kind == "tool" and entries[0].status == "error"


# --- sub-agent demux -----------------------------------------------------


def test_sub_agent_creates_child_transcript_and_rolls_up_tokens() -> None:
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "sub_agent",
                      "payload": {"action": "start", "agent_id": CHILD,
                                  "parent_id": ROOT, "depth": 1, "status": "running",
                                  "input_tokens": 10, "output_tokens": 3,
                                  "agent_type": "probe", "model": "openai/x"}})
    child = hub.transcript(CHILD)
    assert child is not None
    assert child.parent_id == ROOT and child.depth == 1
    assert child.input_tokens == 10 and child.output_tokens == 3
    assert child.model == "openai/x"
    # the spawn marker lands in the PARENT's ordered log
    assert _kinds(hub, ROOT) == ["spawn"]


def test_sub_agent_stop_updates_status_without_duplicate_marker() -> None:
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    start = {"action": "start", "agent_id": CHILD, "parent_id": ROOT, "depth": 1,
             "status": "running"}
    stop = {"action": "stop", "agent_id": CHILD, "parent_id": ROOT, "depth": 1,
            "status": "completed", "input_tokens": 7, "output_tokens": 2}
    hub.observe("s", {"type": "sub_agent", "payload": start})
    hub.observe("s", {"type": "sub_agent", "payload": stop})
    assert _kinds(hub, ROOT) == ["spawn"]  # exactly one marker
    assert hub.transcript(CHILD).status == "completed"  # type: ignore[union-attr]


def test_child_text_does_not_leak_into_root_log() -> None:
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    _delta(hub, "root text", agent=ROOT, depth=0)
    _delta(hub, "child text", agent=CHILD, depth=1, step=0)
    assert _kinds(hub, ROOT) == ["text"]
    assert _kinds(hub, CHILD) == ["text"]
    assert hub.transcript(CHILD).entries[0].text == "child text"  # type: ignore[union-attr]


# --- session scoping -----------------------------------------------------


def test_observer_ignores_other_sessions() -> None:
    hub = AgentTranscriptHub()
    hub.set_active_session("mine")
    _delta(hub, "leak", sid="other")
    assert hub.transcript(ROOT) is None


# --- live sink ordering --------------------------------------------------


def test_live_sink_receives_calls_in_emission_order() -> None:
    sink = _RecordingSink()
    hub = AgentTranscriptHub(sink=sink)
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    _delta(hub, "hi", step=0)
    hub.observe("s", {"type": "agent_message",
                      "payload": {"agent_id": ROOT, "depth": 0, "text": "hi"}})
    hub.tool_started(_FakeStep("bash", {"command": "ls"}))
    _tool_result(hub, "bash", {"command": "ls"})
    names = [c[0] for c in sink.calls]
    # set_status (thinking) → stream_delta(hi) → stream_end → set_status(running)
    # → upsert_tool(running) → upsert_tool(settled)
    assert "stream_delta" in names
    assert names.index("stream_delta") < names.index("stream_end")
    assert names.index("stream_end") < names.index("upsert_tool")
    upserts = [c for c in sink.calls if c[0] == "upsert_tool"]
    assert len(upserts) == 2  # running then settled, SAME card id
    assert upserts[0][1][0] == upserts[1][1][0]
    assert upserts[0][1][1].payload["status"] == "running"
    assert upserts[1][1][1].payload["status"] == "done"


def test_non_streaming_message_still_renders_text() -> None:
    """A model that emits no deltas (only agent_message) still shows its text."""
    sink = _RecordingSink()
    hub = AgentTranscriptHub(sink=sink)
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "agent_message",
                      "payload": {"agent_id": ROOT, "depth": 0, "text": "no stream"}})
    assert _kinds(hub) == ["text"]
    assert hub.transcript(ROOT).entries[0].text == "no stream"  # type: ignore[union-attr]
    assert any(c[0] == "stream_delta" for c in sink.calls)


def test_completion_renders_task_result_when_nothing_streamed() -> None:
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "completion",
                      "payload": {"done": True, "done_reason": "completed",
                                  "task_result": "final answer"}})
    assert _kinds(hub) == ["text"]
    assert hub.transcript(ROOT).entries[0].text == "final answer"  # type: ignore[union-attr]
    assert hub.transcript(ROOT).status == "completed"  # type: ignore[union-attr]


def test_completion_blocked_code_overrides_completed_done_reason() -> None:
    """A ``blocked_code`` completion resolves to ``blocked``, never ``completed``.

    The loop leaves ``done_reason`` at ``"completed"`` for a run that hit an
    unrecovered repo/network/permission/quota wall, carrying the wall
    separately as ``blocked_code`` — the false-success regression: reading
    ``done_reason`` alone (the historical hub behaviour, a raw passthrough)
    rendered this straight through to the fleet panel as a clean green ✓.
    """
    clock = _Clock()
    hub = AgentTranscriptHub(clock=clock)
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    clock.tick(2.0)
    hub.observe("s", {"type": "completion",
                      "payload": {"done": True, "done_reason": "completed",
                                  "blocked_code": "repo_access"}})
    assert hub.transcript(ROOT).status == "blocked"  # type: ignore[union-attr]
    # ``blocked`` is not in the hypervisor's 4-state terminal set, so a naive
    # gate on that set (the pre-fix code) would never stamp ``stopped_at``,
    # leaving the fleet row's elapsed timer ticking forever on a dead run.
    assert hub.fleet_rows()[0].stopped_at == 2.0


def test_completion_unrecognized_blocked_code_is_ignored() -> None:
    """A code outside the closed ``_BLOCKED_CODES`` set never widens the status."""
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "completion",
                      "payload": {"done": True, "done_reason": "completed",
                                  "blocked_code": "not_a_real_code"}})
    assert hub.transcript(ROOT).status == "completed"  # type: ignore[union-attr]


def test_completion_halt_and_verification_failure_map_to_unmet_goal() -> None:
    """``halted_no_progress``/``verification_failed`` both resolve to ``unmet_goal``.

    Before this fix these rendered a bare "?" glyph (the fleet panel's
    unmapped-status fallback) — not a false success, but not a clear signal
    either. Mirrors the console's StatusBadge, which absorbs both into the
    same "Goal not met" pill.
    """
    for reason in ("halted_no_progress", "verification_failed", "unmet_goal"):
        hub = AgentTranscriptHub()
        hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
        hub.observe("s", {"type": "completion",
                          "payload": {"done": True, "done_reason": reason}})
        assert hub.transcript(ROOT).status == "unmet_goal"  # type: ignore[union-attr]


# --- fleet rollups (Phase 2) --------------------------------------


def test_llm_call_end_rolls_up_root_tokens() -> None:
    """The root's tokens come from the cumulative counts on ``llm_call_end``."""
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "llm_call_end",
                      "payload": {"agent_id": ROOT, "depth": 0, "model": "openai/m",
                                  "cumulative_input_tokens": 1200,
                                  "cumulative_output_tokens": 340}})
    t = hub.transcript(ROOT)
    assert t is not None
    assert t.input_tokens == 1200 and t.output_tokens == 340
    assert t.model == "openai/m"


def test_root_last_input_tokens_is_the_live_call_not_the_cumulative_total() -> None:
    """root_last_input_tokens() feeds the ctx gauge — the LAST call's size, not
    the session's cumulative billed total (the gauge was reading
    ``cumulative_input_tokens`` and reporting ~100% instead of ~4%)."""
    hub = AgentTranscriptHub()
    assert hub.root_last_input_tokens() == 0  # no root yet
    hub.observe("s", {"type": "llm_call_end",
                      "payload": {"agent_id": ROOT, "depth": 0,
                                  "input_tokens": 30000,
                                  "cumulative_input_tokens": 935117}})
    assert hub.root_last_input_tokens() == 30000
    hub.observe("s", {"type": "llm_call_end",
                      "payload": {"agent_id": ROOT, "depth": 0,
                                  "input_tokens": 40099,
                                  "cumulative_input_tokens": 975216}})
    # The live number is the LAST call's input_tokens, not the running total.
    assert hub.root_last_input_tokens() == 40099
    t = hub.transcript(ROOT)
    assert t is not None
    assert t.input_tokens == 975216  # cumulative rollup is untouched (cost/fleet)


def test_fleet_rows_summarise_root_and_children() -> None:
    """fleet_rows() projects every agent with model/tools/tokens/status rollups."""
    clock = _Clock()
    hub = AgentTranscriptHub(clock=clock)
    hub.observe("s", {"type": "llm_call_start",
                      "payload": {"agent_id": ROOT, "depth": 0, "model": "openai/root-m"}})
    hub.tool_started(_FakeStep("bash", {"command": "ls"}))
    _tool_result(hub, "bash", {"command": "ls"})
    hub.observe("s", {"type": "llm_call_end",
                      "payload": {"agent_id": ROOT, "depth": 0,
                                  "cumulative_input_tokens": 500,
                                  "cumulative_output_tokens": 100}})
    hub.observe("s", {"type": "sub_agent",
                      "payload": {"action": "start", "agent_id": CHILD,
                                  "parent_id": ROOT, "depth": 1, "status": "running",
                                  "agent_type": "researcher", "model": "openai/child-m",
                                  "input_tokens": 40, "output_tokens": 8}})

    rows = hub.fleet_rows()
    by_id = {r.agent_id: r for r in rows}
    assert set(by_id) == {ROOT, CHILD}

    root = by_id[ROOT]
    assert isinstance(root, FleetRow)
    assert root.is_root and root.label == "root"
    assert root.model == "openai/root-m"
    assert root.tool_count == 1
    assert root.input_tokens == 500 and root.output_tokens == 100
    assert root.tokens == 600

    child = by_id[CHILD]
    assert not child.is_root and child.label == "researcher"
    assert child.depth == 1 and child.parent_id == ROOT
    assert child.tokens == 48


def test_fleet_row_timestamps_give_elapsed() -> None:
    """started_at is stamped first-seen; stopped_at on the terminal transition."""
    clock = _Clock()
    hub = AgentTranscriptHub(clock=clock)
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    clock.tick(3.0)
    hub.observe("s", {"type": "completion",
                      "payload": {"done": True, "done_reason": "completed"}})
    row = hub.fleet_rows()[0]
    assert row.started_at == 0.0
    assert row.stopped_at == 3.0
    assert row.status == "completed"


def test_items_for_projects_entries_in_order() -> None:
    """items_for() yields renderable items in EXACT emission order for drill-in."""
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    _delta(hub, "hello", step=0)
    hub.observe("s", {"type": "agent_message",
                      "payload": {"agent_id": ROOT, "depth": 0, "text": "hello"}})
    hub.tool_started(_FakeStep("bash", {"command": "ls"}))
    _tool_result(hub, "bash", {"command": "ls"})
    hub.observe("s", {"type": "sub_agent",
                      "payload": {"action": "start", "agent_id": CHILD, "parent_id": ROOT,
                                  "depth": 1, "status": "running", "agent_type": "probe"}})

    items = hub.items_for(ROOT)
    assert [it.kind for it in items] == ["assistant", "tool", "notice"]
    assert items[0].payload["text"] == "hello"
    assert items[1].payload["status"] == "done"
    assert "probe" in items[2].payload["text"]

    assert hub.items_for("nonexistent") == []


# --- authoritative todos ingest + throughput + card suppression ----


def _todos(hub: AgentTranscriptHub, items: list[dict], *, agent: str = ROOT,
           source: str = "agent", sid: str = "s") -> None:
    hub.observe(
        sid,
        {"type": "todos",
         "payload": {"items": items, "source": source, "agent_id": agent}},
    )


def test_todos_event_populates_root_dock() -> None:
    """A ``todos`` event drives ``root_todos`` (the Plan dock's source)."""
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    assert hub.root_todos().has_items is False  # nothing emitted → empty (never faked)

    _todos(hub, [
        {"label": "read", "status": "completed"},
        {"label": "write", "status": "in_progress"},
        {"label": "test", "status": "pending"},
    ])
    state = hub.root_todos()
    assert [it.label for it in state.items] == ["read", "write", "test"]
    # completed → done (panel vocabulary); exactly one in_progress → current.
    assert [it.state for it in state.items] == ["done", "in_progress", "pending"]
    assert state.done == 1 and state.total == 3
    assert state.current == "write"


def test_todos_reemit_replaces_the_whole_list() -> None:
    """Re-emit-each-update: the latest event fully replaces the displayed list."""
    hub = AgentTranscriptHub()
    _todos(hub, [{"label": "a", "status": "in_progress"}])
    _todos(hub, [{"label": "a", "status": "completed"}, {"label": "b", "status": "in_progress"}])
    state = hub.root_todos()
    assert [it.label for it in state.items] == ["a", "b"]
    assert state.done == 1


def test_update_todos_makes_no_transcript_card() -> None:
    """``update_todos`` is suppressed — no card, no tool_count (dock, not spam)."""
    hub = AgentTranscriptHub()
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.tool_started(_FakeStep("update_todos", {"todos": []}))
    _tool_result(hub, "update_todos", {"todos": []})
    # A real tool DOES render a card; update_todos does not.
    assert _kinds(hub) == []
    t = hub.transcript(ROOT)
    assert t is not None and t.tool_count == 0


def test_fleet_row_carries_stall_throughput() -> None:
    """A running agent silent past the stall threshold surfaces a stall facet."""
    from mewbo_cli.tui.status.throughput_meter import Phase

    clock = _Clock()
    hub = AgentTranscriptHub(clock=clock)
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    # Advance the clock far past the stall window with NO further events.
    clock.tick(120.0)
    row = next(r for r in hub.fleet_rows() if r.is_root)
    assert row.throughput is not None
    assert row.throughput.phase is Phase.STALLED


def test_root_activity_label_reflects_phase() -> None:
    """The foot-activity label is derived from the ROOT throughput meter."""
    clock = _Clock()
    hub = AgentTranscriptHub(clock=clock)
    assert hub.root_activity_label() == "working"  # no root yet
    hub.observe("s", {"type": "llm_call_start",
                      "payload": {"agent_id": ROOT, "depth": 0, "model": "gpt-4o"}})
    assert hub.root_activity_label() == "uploading…"
    _delta(hub, "hello there world", step=0)
    clock.tick(0.5)
    _delta(hub, "hello there world", step=0)
    assert "tok/s" in hub.root_activity_label()


# --- deferred sink: the deadlock law (never invoke the sink under _lock) ------


def test_sink_is_never_invoked_while_the_state_lock_is_held() -> None:
    """The confirmed deadlock trigger must run the sink with ``_lock`` FREE.

    The regression: ``observe(llm_call_start)`` held ``_lock`` while calling the
    sink's ``set_status``, whose ``call_from_thread`` callback re-entered the
    hub's read API and self-deadlocked cross-thread. We prove the sink now runs
    off the lock by acquiring ``_lock`` from a FOREIGN thread inside every sink
    call — ``_lock`` is an ``RLock``, so a same-thread check would spuriously
    pass via reentrancy; a different thread cannot acquire a held lock.
    """
    hub_box: dict[str, AgentTranscriptHub] = {}
    lock_free_on_each_call: list[bool] = []

    def _probe_lock_is_free() -> None:
        result: list[bool] = []

        def _grab() -> None:
            acquired = hub_box["hub"]._lock.acquire(blocking=False)
            result.append(acquired)
            if acquired:
                hub_box["hub"]._lock.release()

        t = threading.Thread(target=_grab)
        t.start()
        t.join()
        lock_free_on_each_call.append(result[0])

    class _LockProbingSink:
        """Every RootSink method probes that ``_lock`` is free when it runs."""

        def stream_delta(self, stream_id: str, delta: str) -> None:
            _probe_lock_is_free()

        def stream_end(self, stream_id: str) -> None:
            _probe_lock_is_free()

        def upsert_tool(self, card_id: str, item: Any) -> None:
            _probe_lock_is_free()

        def spawn(self, item: Any) -> None:
            _probe_lock_is_free()

        def set_status(self, label: str) -> None:
            _probe_lock_is_free()

    hub = AgentTranscriptHub(sink=_LockProbingSink())
    hub_box["hub"] = hub

    # The EXACT deadlock trigger — llm_call_start → _on_llm_start → set_status —
    # plus a fuller turn so stream/tool/settle sink paths are all probed.
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    _delta(hub, "hi", step=0)
    hub.observe("s", {"type": "agent_message",
                      "payload": {"agent_id": ROOT, "depth": 0, "text": "hi"}})
    hub.tool_started(_FakeStep("bash", {"command": "ls"}))
    _tool_result(hub, "bash", {"command": "ls"})

    assert lock_free_on_each_call, "sink was never invoked — the trigger did not fire"
    assert all(lock_free_on_each_call), "hub._lock was held during a sink call (deadlock risk)"


def test_concurrent_flush_delivers_in_strict_enqueue_order() -> None:
    """Two threads enqueue while ONE drains — the sink still receives every action
    in exact enqueue order (single-drainer FIFO).

    A gate parks the drainer mid-delivery (inside the first action) so the second
    thread's burst is provably enqueued BEHIND the first and drained by the still-
    active drainer — never reordered, never dropped, never double-delivered.
    """
    delivering = threading.Event()
    gate = threading.Event()
    received: list[tuple[str, Any]] = []

    class _GatedSink:
        def stream_delta(self, stream_id: str, delta: str) -> None:
            received.append(("stream_delta", delta))
            if delta == "a0":
                # Park the drainer here, holding the single-drainer guard, until
                # the second thread has queued its burst behind us.
                delivering.set()
                gate.wait(timeout=5.0)

        def stream_end(self, stream_id: str) -> None:
            received.append(("stream_end", stream_id))

        def upsert_tool(self, card_id: str, item: Any) -> None:
            received.append(("upsert_tool", item.payload.get("status")))

        def spawn(self, item: Any) -> None:
            received.append(("spawn", None))

        def set_status(self, label: str) -> None:
            received.append(("set_status", label))

    hub = AgentTranscriptHub(sink=_GatedSink())

    # Thread A emits one delta whose DELIVERY blocks in the sink (it becomes the
    # drainer and parks on the gate), enqueuing exactly one action.
    thread_a = threading.Thread(target=lambda: _delta(hub, "a0", step=0))
    thread_a.start()
    assert delivering.wait(timeout=5.0), "drainer never entered the sink"

    # Main thread now enqueues a burst; each observe() flushes but returns at once
    # (A is the drainer), so these queue up deterministically BEHIND a0. Each new
    # step closes the prior span (stream_end) then opens a new one (stream_delta).
    _delta(hub, "b1", step=1)
    _delta(hub, "b2", step=2)
    _delta(hub, "b3", step=3)

    gate.set()
    thread_a.join(timeout=5.0)
    assert not thread_a.is_alive()

    assert received == [
        ("stream_delta", "a0"),
        ("stream_end", "agent-root:0"),
        ("stream_delta", "b1"),
        ("stream_end", "agent-root:1"),
        ("stream_delta", "b2"),
        ("stream_end", "agent-root:2"),
        ("stream_delta", "b3"),
    ]
