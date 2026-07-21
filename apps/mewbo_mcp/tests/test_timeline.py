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

from mewbo_mcp.timeline import (
    build_timeline,
    compute_turn_token_usage,
    extract_question_events,
    extract_recovery_events,
    extract_trigger_events,
)


def _user(text: str, ts: str = "t0", **payload) -> dict:
    return {"type": "user", "ts": ts, "payload": {"text": text, **payload}}


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


def _trigger_armed(ts: str = "t1", **payload) -> dict:
    return {"type": "trigger_armed", "ts": ts, "payload": payload}


def _trigger_fired(ts: str = "t1", **payload) -> dict:
    return {"type": "trigger_fired", "ts": ts, "payload": payload}


def _session_terminated(ts: str = "t9") -> dict:
    return {"type": "session_terminated", "ts": ts, "payload": {}}


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
    """Once an assistant event closes the turn, a trailing completion opens nothing.

    The completion still contributes its outcome: this is the ORDINARY shape of
    a finished run (final assistant event, then the completion), and reading the
    reason only off a completion-CLOSED turn is what left every turn of a
    normally-shaped session reporting no outcome at all.
    """
    events = [
        _user("hi"),
        _assistant("answer"),
        _completion("stop", ts="t10"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].assistant_text == "answer"
    assert turns[0].done_reason == "stop"


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


def test_user_attachments_project_onto_the_turn():
    """A user event's ``attachments`` list is carried onto its Turn."""
    descriptor = {
        "id": "a1",
        "filename": "spec.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "stored_name": "a1_spec.pdf",
        "uploaded_at": "2026-07-03T00:00:00+00:00",
        "parsed": True,
    }
    events = [_user("see attached", attachments=[descriptor]), _assistant("ok")]
    turns = build_timeline(events)
    assert turns[0].attachments == [descriptor]


def test_user_without_attachments_stays_empty():
    """A turn opened by a plain user event (no attachments key) is clean."""
    events = [_user("hi"), _assistant("hello")]
    turns = build_timeline(events)
    assert turns[0].attachments == []


def test_user_malformed_attachments_coerced_to_empty():
    """A non-list/non-dict ``attachments`` value degrades to ``[]``, never raises."""
    events = [_user("hi", attachments="not-a-list"), _assistant("hello")]
    turns = build_timeline(events)
    assert turns[0].attachments == []
    events2 = [_user("hi", attachments=["not-a-dict", 42]), _assistant("hello")]
    turns2 = build_timeline(events2)
    assert turns2[0].attachments == []


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
# Interrupted turns — a turn the next prompt superseded before it concluded
# (TS parity: utils/timeline.ts materialises the same turn as a `run_failed`
# entry with reason "interrupted" instead of discarding its body)
# ---------------------------------------------------------------------------


def test_turn_superseded_by_next_prompt_is_flagged_interrupted():
    """A turn with no assistant AND no completion before the next user prompt."""
    events = [
        _user("do the long thing", ts="t0"),
        _tool_result("shell", "step one", ts="t1"),
        _tool_result("shell", "step two", ts="t2"),
        _user("what happened?", ts="t5"),
        _assistant("The run was cut short.", ts="t6"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 2
    first = turns[0]
    assert first.interrupted is True
    assert first.closed is False
    # Synthesised from the ABSENCE of a closure — there is no completion event
    # for this turn, so "interrupted" is the only outcome there is to report.
    assert first.done_reason == "interrupted"
    # Nothing was concluded, so no text is invented for it.
    assert first.assistant_text == ""
    # Its body survives — the whole point of materialising it.
    assert first.step_count == 2
    assert turns[1].interrupted is False
    assert turns[1].closed is True


def test_trailing_open_turn_is_not_interrupted():
    """The last turn of a still-running session is open, not terminal.

    Both have ``closed=False``; only the superseded one is ``interrupted``.
    """
    turns = build_timeline([_user("go"), _tool_result("shell")])
    assert len(turns) == 1
    assert turns[0].closed is False
    assert turns[0].interrupted is False


def test_concluded_turns_are_never_interrupted():
    """A turn closed by either an assistant or a completion stays clean."""
    events = [
        _user("q1"),
        _assistant("a1"),
        _user("q2"),
        _completion("error", ts="t8"),
        _user("q3"),
        _assistant("a3"),
    ]
    turns = build_timeline(events)
    assert [t.interrupted for t in turns] == [False, False, False]


def test_consecutive_interrupted_turns_each_keep_their_own_body():
    """Back-to-back interrupted turns do not merge or lose events."""
    events = [
        _user("one", ts="t0"),
        _tool_result("shell", ts="t1"),
        _user("two", ts="t2"),
        _tool_result("shell", ts="t3"),
        _tool_result("shell", ts="t4"),
        _user("three", ts="t5"),
        _assistant("finally", ts="t6"),
    ]
    turns = build_timeline(events)
    assert [t.interrupted for t in turns] == [True, True, False]
    assert [t.step_count for t in turns] == [1, 2, 0]


# ---------------------------------------------------------------------------
# extract_recovery_events — user-driven retry / continue between turns
# (TS parity: utils/timeline.ts `recovery` TimelineEntry role, parsed BEFORE
# the open-turn gate because the runtime records these BETWEEN turns)
# ---------------------------------------------------------------------------


def _recovery(action: str, ts: str = "t5") -> dict:
    return {"type": "recovery", "ts": ts, "payload": {"action": action}}


def test_recovery_between_turns_is_kept_not_dropped():
    """A continue recorded between a failed turn and its resumption is a marker.

    This is the ONLY shape the runtime produces, so gating on an open turn
    would drop every one of them.
    """
    events = [
        _user("task"),
        _completion("error", ts="t2"),
        _recovery("continue", ts="t5"),
        _user("continue prompt", ts="t6"),
        _assistant("recovered!", ts="t7"),
    ]
    markers = extract_recovery_events(events)
    assert len(markers) == 1
    assert markers[0].action == "continue"
    assert markers[0].ts == "t5"
    assert markers[0].turn_index is None  # no turn open between the two


def test_recovery_retry_action_is_carried():
    markers = extract_recovery_events(
        [_user("task"), _completion("error", ts="t2"), _recovery("retry", ts="t3")]
    )
    assert [m.action for m in markers] == ["retry"]


def test_recovery_mid_turn_gets_the_open_turn_index():
    """Defensive: a marker arriving with a turn open is stamped with its index."""
    markers = extract_recovery_events([_user("task"), _recovery("continue", ts="t1")])
    assert markers[0].turn_index == 1


def test_engine_halt_recovery_is_not_a_conversation_marker():
    """``halt_no_progress`` is trace detail, not a user-driven recovery."""
    events = [
        _user("task"),
        {"type": "recovery", "ts": "t1", "payload": {"action": "halt_no_progress"}},
        _assistant("stopped early"),
    ]
    assert extract_recovery_events(events) == []


def test_recovery_malformed_action_yields_no_marker():
    """A missing/non-string action degrades to nothing, never raises."""
    assert extract_recovery_events([{"type": "recovery", "ts": "t1", "payload": {}}]) == []
    assert extract_recovery_events(
        [{"type": "recovery", "ts": "t1", "payload": {"action": 7}}]
    ) == []


def test_extract_recovery_events_independent_of_build_timeline():
    """Turn reconstruction is unaffected by interleaved recovery events."""
    events = [
        _user("q1"),
        _tool_result("shell", "ran ls"),
        _completion("error", ts="t3"),
        _recovery("continue", ts="t4"),
        _user("q2", ts="t5"),
        _assistant("a2", ts="t6"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 2
    assert turns[0].step_count == 1
    assert turns[1].assistant_text == "a2"
    assert len(extract_recovery_events(events)) == 1


def test_extract_recovery_events_empty_transcript():
    assert extract_recovery_events([_user("hi"), _assistant("hello")]) == []


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


# ---------------------------------------------------------------------------
# extract_trigger_events — trigger_armed / trigger_fired / session_terminated
# (TS parity: utils/timeline.ts parseTriggerEvent + the
# trigger/session_terminated TimelineEntry roles, parsed BEFORE the open-turn
# gate so a marker arriving between turns is never silently dropped)
# ---------------------------------------------------------------------------


def test_trigger_armed_mid_turn_gets_the_open_turn_index():
    """A trigger armed while a turn is open is tagged with that turn's index."""
    events = [
        _user("do the thing"),
        _trigger_armed(ts="t1", trigger_id="tr1", kind="time.cron", summary="Armed a cron wake"),
        _assistant("done"),
    ]
    triggers, terminated = extract_trigger_events(events)
    assert terminated is None
    assert len(triggers) == 1
    marker = triggers[0]
    assert marker.action == "armed"
    assert marker.turn_index == 1
    assert marker.trigger_id == "tr1"
    assert marker.kind == "time.cron"
    assert marker.summary == "Armed a cron wake"
    assert marker.ts == "t1"


def test_trigger_fired_between_turns_is_an_orphan():
    """A trigger firing BETWEEN turns (no turn open) gets turn_index=None, not dropped.

    This is the common case: a fired trigger wakes an idle session, typically
    BEFORE the re-engagement's own `user` event opens the next turn. Silently
    dropping it (matching the prior open-turn gate) would lose exactly the
    reason the session woke up.
    """
    events = [
        _user("q1"),
        _assistant("a1"),
        _trigger_fired(ts="t5", trigger_id="tr2", kind="time.at", payload_summary="Fired at noon"),
        _user("q2"),
        _assistant("a2"),
    ]
    triggers, terminated = extract_trigger_events(events)
    assert terminated is None
    assert len(triggers) == 1
    marker = triggers[0]
    assert marker.action == "fired"
    assert marker.turn_index is None
    assert marker.summary == "Fired at noon"


def test_trigger_fired_reads_payload_summary_not_summary():
    """`fired` reads `payload_summary`; a stray `summary` field on it is ignored (TS parity)."""
    events = [_trigger_fired(summary="wrong field", payload_summary="right field")]
    triggers, _terminated = extract_trigger_events(events)
    assert triggers[0].summary == "right field"


def test_trigger_kind_falls_back_to_generic_label():
    """A missing/empty `kind` falls back to the literal "trigger" (TS parity)."""
    events = [_trigger_armed(trigger_id="tr3")]
    triggers, _terminated = extract_trigger_events(events)
    assert triggers[0].kind == "trigger"

    events2 = [_trigger_armed(trigger_id="tr4", kind="")]
    triggers2, _terminated2 = extract_trigger_events(events2)
    assert triggers2[0].kind == "trigger"


def test_trigger_marker_defensive_coercion():
    """A non-string trigger_id/kind and an empty/whitespace summary degrade cleanly."""
    events = [_trigger_armed(trigger_id=42, kind=7, summary="   ")]
    triggers, _terminated = extract_trigger_events(events)
    marker = triggers[0]
    assert marker.trigger_id is None
    assert marker.kind == "trigger"
    assert marker.summary is None


def test_session_terminated_marker_captures_ts():
    """A `session_terminated` event becomes a TerminationMarker with its ts."""
    events = [_user("hi"), _assistant("hello"), _session_terminated(ts="t99")]
    triggers, terminated = extract_trigger_events(events)
    assert triggers == []
    assert terminated is not None
    assert terminated.ts == "t99"


def test_session_terminated_is_turn_independent():
    """Unlike a trigger marker, `session_terminated` mid-turn is still captured
    (it never attaches to a turn — TS always uses the fixed
    `turnId: "session-terminated"`, never the currently-open turn)."""
    events = [_user("q1"), _session_terminated(ts="t2"), _assistant("a1")]
    _triggers, terminated = extract_trigger_events(events)
    assert terminated is not None
    assert terminated.ts == "t2"


def test_session_terminated_last_wins_if_multiple():
    """Termination is absorbing; a defensive last-wins pick if a transcript ever has >1."""
    events = [_session_terminated(ts="t1"), _session_terminated(ts="t2")]
    _triggers, terminated = extract_trigger_events(events)
    assert terminated is not None
    assert terminated.ts == "t2"


def test_extract_trigger_events_is_independent_of_build_timeline():
    """Turn reconstruction is unaffected by interleaved trigger/termination events
    (a SEPARATE pass, not folded into build_timeline's loop — see module docstring)."""
    events = [
        _user("q1"),
        _trigger_armed(trigger_id="tr5", kind="webhook"),
        _tool_result("shell", "ran ls"),
        _assistant("a1"),
        _trigger_fired(trigger_id="tr5", kind="webhook"),
        _session_terminated(),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].step_count == 1  # only the tool_result counts as a step
    assert turns[0].assistant_text == "a1"
    triggers, terminated = extract_trigger_events(events)
    assert len(triggers) == 2
    assert terminated is not None


def test_extract_trigger_events_empty_transcript():
    """No trigger/termination events yields empty results, never raises."""
    triggers, terminated = extract_trigger_events([_user("hi"), _assistant("hello")])
    assert triggers == []
    assert terminated is None


# ---------------------------------------------------------------------------
# extract_question_events — user_question / user_question_answered
# (ask-user questions; TS parity: utils/timeline.ts question TimelineEntry role,
# user_question opens a pending card, user_question_answered settles it by
# call_id — mirroring the plan_proposed → plan_approved fold)
# ---------------------------------------------------------------------------

_Q_SINGLE = {
    "header": "Scope",
    "question": "How far should this go?",
    "options": [
        {"label": "Just this file", "description": None},
        {"label": "The whole module", "description": "Wider blast radius"},
    ],
    "multi_select": False,
}
_Q_FREE = {"header": "Name", "question": "What name?", "options": [], "multi_select": False}


def _user_question(call_id: str, questions=None, ts: str = "t1", **payload) -> dict:
    return {
        "type": "user_question",
        "ts": ts,
        "payload": {
            "call_id": call_id,
            "call_token": "secret-token",
            "questions": questions if questions is not None else [_Q_SINGLE],
            **payload,
        },
    }


def _user_question_answered(
    call_id: str, outcome: str, *, answers=None, answered_via=None, ts: str = "t2"
) -> dict:
    return {
        "type": "user_question_answered",
        "ts": ts,
        "payload": {
            "call_id": call_id,
            "outcome": outcome,
            "answered_via": answered_via,
            "answers": answers,
        },
    }


def test_question_pending_marker_gets_open_turn_index():
    """A user_question opens a pending marker stamped with the open turn's index."""
    events = [_user("start"), _user_question("c1", ts="t1")]
    markers = extract_question_events(events)
    assert len(markers) == 1
    m = markers[0]
    assert m.call_id == "c1"
    assert m.turn_index == 1
    assert m.status == "pending"
    assert m.answers is None
    assert m.answered_via is None
    assert m.questions == [_Q_SINGLE]
    assert m.ts == "t1"


def test_question_answered_settles_in_place():
    """user_question_answered settles the pending marker: status/answers/answered_via."""
    events = [
        _user("start"),
        _user_question("c1"),
        _user_question_answered(
            "c1",
            "answered",
            answers=[{"selected_indexes": [1], "text": None}],
            answered_via="console",
        ),
        _assistant("done"),
    ]
    markers = extract_question_events(events)
    assert len(markers) == 1
    m = markers[0]
    assert m.status == "answered"
    assert m.answers == [{"selected_indexes": [1], "text": None}]
    assert m.answered_via == "console"


def test_question_declined_has_no_answers():
    """A non-answered outcome settles status but attaches no answers (TS parity)."""
    for outcome in ("declined", "interrupted", "cancelled"):
        events = [
            _user("start"),
            _user_question("c1"),
            _user_question_answered("c1", outcome, answers=[{"text": "ignored"}]),
        ]
        m = extract_question_events(events)[0]
        assert m.status == outcome
        assert m.answers is None


def test_question_marker_omits_call_token():
    """The bearer secret never rides the read-only MCP projection."""
    m = extract_question_events([_user("hi"), _user_question("c1")])[0]
    assert not hasattr(m, "call_token")


def test_question_answered_without_pending_is_ignored():
    """An answered event with no matching pending call_id creates nothing (TS parity)."""
    events = [_user("hi"), _user_question_answered("ghost", "answered", answers=[])]
    assert extract_question_events(events) == []


def test_question_without_open_turn_is_dropped():
    """A question with no open turn is dropped (TS parity: folded after the gate).

    Unlike ``trigger_fired`` (which normally wakes a session BETWEEN turns and
    is retained), a question is only ever asked mid-run, so the console's inline
    fold drops a turn-less one — and so does this pass.
    """
    assert extract_question_events([_user("q1"), _assistant("a1"), _user_question("c1")]) == []
    assert extract_question_events([_user_question("c1")]) == []


def test_question_malformed_call_id_creates_no_marker():
    """A non-string/empty call_id degrades to no marker, never raises."""
    assert extract_question_events([_user("hi"), _user_question(42)]) == []  # type: ignore[arg-type]
    events = [_user("hi"), {"type": "user_question", "ts": "t1", "payload": {}}]
    assert extract_question_events(events) == []


def test_question_free_text_answer_rendered_verbatim():
    """A free-text answer settles with the text answer item intact."""
    events = [
        _user("start"),
        _user_question("c1", [_Q_FREE]),
        _user_question_answered(
            "c1",
            "answered",
            answers=[{"selected_indexes": None, "text": "Aurora"}],
            answered_via="cli",
        ),
    ]
    m = extract_question_events(events)[0]
    assert m.questions == [_Q_FREE]
    assert m.answers == [{"selected_indexes": None, "text": "Aurora"}]
    assert m.answered_via == "cli"


def test_extract_question_events_independent_of_build_timeline():
    """Turn reconstruction is unaffected by interleaved question events."""
    events = [
        _user("q1"),
        _user_question("c1"),
        _tool_result("shell", "ran ls"),
        _user_question_answered("c1", "answered", answers=[{"text": "yes"}]),
        _assistant("a1"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].step_count == 1  # only the tool_result is a step
    assert turns[0].assistant_text == "a1"
    markers = extract_question_events(events)
    assert len(markers) == 1
    assert markers[0].status == "answered"


def test_extract_question_events_empty_transcript():
    """No question events yields an empty list, never raises."""
    assert extract_question_events([_user("hi"), _assistant("hello")]) == []


# ---------------------------------------------------------------------------
# Projection fidelity — rules the canonical assembler owns, checked through the
# MCP shapes so a regrouping bug here cannot silently rewrite a conversation
# ---------------------------------------------------------------------------


def test_completion_placeholder_is_never_projected_as_model_text():
    """"(run ended)" is a rendering placeholder, not something a model returned.

    The assembler labels a non-failing completion closure with the assistant
    role, so the role alone cannot separate it from a real answer — only the
    presence of a ``done_reason`` can.
    """
    turns = build_timeline([_user("go"), _completion("stop")])
    assert turns[0].closed is True
    assert turns[0].done_reason == "stop"
    assert turns[0].assistant_text == ""


def test_failed_completion_projects_its_reason_and_no_text():
    turns = build_timeline([_user("go"), _completion("error", error="upstream died")])
    assert turns[0].closed is True
    assert turns[0].done_reason == "error"
    assert turns[0].assistant_text == ""


def test_synthetic_closure_upgrade_leaves_the_turn_intact():
    """A failure landing on the orchestrator's placeholder still yields ONE turn.

    The upgrade happens in place, so the turn keeps its metadata and the
    projection must not lose the turn or duplicate it.
    """
    events = [
        _user("go", ts="t0"),
        _assistant("(Run interrupted by error: upstream died)", ts="t8"),
        _completion("error", ts="t9", error="upstream died"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].closed is True
    assert turns[0].interrupted is False


def test_failure_on_real_prose_preserves_the_answer():
    """A boot sweep appends a completion to an orphaned run; the prose is the
    only copy of what it produced and must survive the projection."""
    events = [
        _user("go", ts="t0"),
        _assistant("Here is the real answer.", ts="t8"),
        _completion("error", ts="t9", error="run died"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].assistant_text == "Here is the real answer."


def test_open_turn_keeps_its_body():
    """The trailing turn owns no closing row, so its events come from the
    walk's leftover state rather than from an entry."""
    turns = build_timeline([_user("go"), _tool_result("shell"), _tool_result("file_edit")])
    assert len(turns) == 1
    assert turns[0].closed is False
    assert turns[0].interrupted is False
    assert turns[0].step_count == 2


def test_markers_do_not_create_or_close_turns():
    """Trigger/recovery/termination rows share a turn id without concluding it."""
    events = [
        _user("go", ts="t0"),
        _trigger_armed(ts="t1", trigger_id="tr1", kind="time.cron"),
        _tool_result("shell", ts="t2"),
        _assistant("done", ts="t3"),
        _recovery("retry", ts="t4"),
        _session_terminated(ts="t5"),
    ]
    turns = build_timeline(events)
    assert len(turns) == 1
    assert turns[0].step_count == 1
    assert turns[0].assistant_text == "done"
