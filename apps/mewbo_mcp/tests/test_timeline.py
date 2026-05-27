"""Parity tests for the Python port of ``buildTimeline``.

These mirror the turn-reconstruction and token-usage cases that
``apps/mewbo_console/src/utils/timeline.test.ts`` would cover. The fixtures
are deliberately small, fixed-timestamp event streams — same input shape the
console consumes — so the Python output structure must match the TS logic.

DRY note: source of truth for the algorithm is
``apps/mewbo_console/src/utils/timeline.ts``; the port lives in
``apps/mewbo_mcp/src/mewbo_mcp/timeline.py``.
"""

from __future__ import annotations

from mewbo_mcp.timeline import build_timeline, compute_turn_token_usage


def _user(text: str, ts: str = "t0") -> dict:
    return {"type": "user", "ts": ts, "payload": {"text": text}}


def _assistant(text: str, ts: str = "t9") -> dict:
    return {"type": "assistant", "ts": ts, "payload": {"text": text}}


def _tool_result(tool_id: str, summary: str = "", ts: str = "t1", **payload) -> dict:
    return {
        "type": "tool_result",
        "ts": ts,
        "payload": {"tool_id": tool_id, "summary": summary, **payload},
    }


def _llm_call_end(ts: str = "t2", **payload) -> dict:
    return {"type": "llm_call_end", "ts": ts, "payload": payload}


def _completion(done_reason: str, ts: str = "t9", **payload) -> dict:
    return {"type": "completion", "ts": ts, "payload": {"done_reason": done_reason, **payload}}


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------


def test_single_turn_user_assistant():
    """A user→assistant pair forms exactly one closed turn."""
    events = [_user("hi"), _assistant("hello")]
    turns = build_timeline(events)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.index == 1
    assert turn.turn_id == "turn-1"
    assert turn.user_text == "hi"
    assert turn.assistant_text == "hello"
    assert turn.closed is True
    assert turn.step_count == 0


def test_steps_are_tool_results():
    """Each tool_result inside a turn is one step; nothing else counts."""
    events = [
        _user("do it"),
        _tool_result("shell", "ran ls"),
        _llm_call_end(depth=0, input_tokens=10, output_tokens=5),
        _tool_result("file_edit", "patched x"),
        _assistant("done"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].step_count == 2
    assert [s["payload"]["tool_id"] for s in turns[0].steps] == ["shell", "file_edit"]


def test_completion_closes_open_turn():
    """A completion event closes a turn that has no terminating assistant event."""
    events = [_user("go"), _tool_result("shell"), _completion("error")]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].closed is True
    assert turns[0].done_reason == "error"
    assert turns[0].assistant_text == ""


def test_command_completion_surfaces_text():
    """A slash-command completion carries its rendered body as assistant_text."""
    events = [_user("/status"), _completion("command", text="All good.")]
    turns = build_timeline(events)
    assert turns[0].assistant_text == "All good."
    assert turns[0].done_reason == "command"


def test_assistant_wins_over_later_completion():
    """Once an assistant event closes the turn, a trailing completion opens nothing."""
    events = [
        _user("hi"),
        _assistant("answer"),
        _completion("stop", ts="t10"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].assistant_text == "answer"
    assert turns[0].done_reason is None  # assistant closed it before completion


def test_events_before_first_user_are_ignored():
    """Events before any user message (or between turns) do not attach anywhere."""
    events = [_tool_result("shell"), _user("hi"), _assistant("yo"), _tool_result("orphan")]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].step_count == 0  # neither orphan tool_result counts


def test_multiple_turns_indexed_sequentially():
    """Turn indices increment 1..N across multiple user→assistant pairs."""
    events = [
        _user("q1"),
        _assistant("a1"),
        _user("q2"),
        _assistant("a2"),
        _user("q3"),
        _assistant("a3"),
    ]
    turns = build_timeline(events)
    assert [t.index for t in turns] == [1, 2, 3]
    assert [t.user_text for t in turns] == ["q1", "q2", "q3"]


def test_context_event_sets_turn_model():
    """A context model event flows into the next opened turn's model."""
    events = [
        {"type": "context", "ts": "t0", "payload": {"model": "gpt-x"}},
        _user("hi"),
        _assistant("hello"),
    ]
    turns = build_timeline(events)
    assert turns[0].model == "gpt-x"


# ---------------------------------------------------------------------------
# Token usage — PEAK input / SUM output, sub-agent isolation
# ---------------------------------------------------------------------------


def test_token_usage_peak_input_sum_output():
    """Root input is the PEAK across calls; output is the SUM (TS parity)."""
    events = [
        _llm_call_end(depth=0, input_tokens=100, output_tokens=10),
        _llm_call_end(depth=0, input_tokens=250, output_tokens=20),  # grew (tool stacked)
        _llm_call_end(depth=0, input_tokens=180, output_tokens=5),
    ]
    usage = compute_turn_token_usage(events)
    assert usage is not None
    assert usage.input_tokens == 250  # peak, not 530
    assert usage.output_tokens == 35  # sum
    assert usage.billed_input_tokens == 530  # cumulative billable


def test_token_usage_sub_agents_summed_peaks():
    """Sub-agent input sums per-agent peaks; sub-agent count is distinct ids."""
    events = [
        _llm_call_end(depth=0, input_tokens=100, output_tokens=10),
        _llm_call_end(depth=1, agent_id="a", input_tokens=50, output_tokens=4),
        _llm_call_end(depth=1, agent_id="a", input_tokens=80, output_tokens=6),  # peak for a
        _llm_call_end(depth=1, agent_id="b", input_tokens=30, output_tokens=2),
    ]
    usage = compute_turn_token_usage(events)
    assert usage is not None
    assert usage.sub_input_tokens == 80 + 30  # peak(a)=80, peak(b)=30
    assert usage.sub_output_tokens == 12
    assert usage.sub_agent_count == 2


def test_token_usage_none_when_empty():
    """No measurable token activity yields None (matches the TS undefined)."""
    assert compute_turn_token_usage([_user("hi"), _assistant("yo")]) is None


def test_token_usage_cache_and_reasoning_rollup():
    """Cache + reasoning fields sum across calls."""
    events = [
        _llm_call_end(
            depth=0,
            input_tokens=100,
            output_tokens=10,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=5,
            reasoning_output_tokens=3,
        ),
        _llm_call_end(
            depth=0,
            input_tokens=120,
            output_tokens=8,
            cache_read_input_tokens=15,
            reasoning_output_tokens=7,
        ),
    ]
    usage = compute_turn_token_usage(events)
    assert usage is not None
    assert usage.cache_creation_tokens == 20
    assert usage.cache_read_tokens == 20
    assert usage.reasoning_tokens == 10


def test_turn_token_usage_to_dict_camelcase():
    """to_dict emits the camelCase wire shape consumed by callers."""
    usage = compute_turn_token_usage(
        [_llm_call_end(depth=0, input_tokens=10, output_tokens=2)]
    )
    assert usage is not None
    d = usage.to_dict()
    assert d["inputTokens"] == 10
    assert d["outputTokens"] == 2
    assert set(d) == {
        "inputTokens",
        "outputTokens",
        "subInputTokens",
        "subOutputTokens",
        "subAgentCount",
        "cacheCreationTokens",
        "cacheReadTokens",
        "reasoningTokens",
        "billedInputTokens",
    }


def test_turn_token_usage_attached_to_turn():
    """A turn's token_usage() reads only that turn's llm_call_end events."""
    events = [
        _user("q1"),
        _llm_call_end(depth=0, input_tokens=100, output_tokens=10),
        _assistant("a1"),
        _user("q2"),
        _llm_call_end(depth=0, input_tokens=999, output_tokens=99),
        _assistant("a2"),
    ]
    turns = build_timeline(events)
    u1 = turns[0].token_usage()
    u2 = turns[1].token_usage()
    assert u1 is not None and u1.input_tokens == 100
    assert u2 is not None and u2.input_tokens == 999
