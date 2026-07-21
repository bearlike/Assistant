"""Contract tests for the canonical turn assembler.

These are written from a consumer's viewpoint: feed a literal event log in,
assert the conversation a reader would see. The assembler is pure, so no
fixture, clock or store appears anywhere below — which is the point of it
living in core rather than behind an app's route.

The behavioral source of truth is the console's ``buildTimeline``
(``apps/mewbo_console/src/utils/timeline.ts``). Every rule it encodes has a
case here; the seeded shapes are the ones that were observed to break in
practice — a turn superseded before it concluded, a recovery recorded between
turns, and a failure completion landing on real prose rather than on the
orchestrator's placeholder.
"""

from __future__ import annotations

from mewbo_core.transcript_timeline import (
    TimelineEntry,
    TranscriptTimeline,
    TurnTokenUsage,
)


def _user(text: str, ts: str = "t0", **payload) -> dict:
    return {"type": "user", "ts": ts, "payload": {"text": text, **payload}}


def _assistant(text: str, ts: str = "t9") -> dict:
    return {"type": "assistant", "ts": ts, "payload": {"text": text}}


def _tool_result(tool_id: str, ts: str = "t1", **payload) -> dict:
    return {"type": "tool_result", "ts": ts, "payload": {"tool_id": tool_id, **payload}}


def _completion(done_reason: str, ts: str = "t9", **payload) -> dict:
    return {
        "type": "completion",
        "ts": ts,
        "payload": {"done_reason": done_reason, **payload},
    }


def _llm_call_end(ts: str = "t2", **payload) -> dict:
    return {"type": "llm_call_end", "ts": ts, "payload": payload}


def _roles(entries: list[TimelineEntry]) -> list[str]:
    return [e.role for e in entries]


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------


def test_user_assistant_pair_is_one_turn():
    entries = TranscriptTimeline.assemble([_user("hi"), _assistant("hello")])
    assert _roles(entries) == ["user", "assistant"]
    assert entries[0].content == "hi"
    assert entries[1].content == "hello"
    assert entries[0].turn_id == entries[1].turn_id == "turn-1"
    assert entries[1].turn is not None


def test_turn_ids_increment_across_prompts():
    entries = TranscriptTimeline.assemble(
        [_user("q1"), _assistant("a1"), _user("q2"), _assistant("a2")]
    )
    assert [e.turn_id for e in entries] == ["turn-1", "turn-1", "turn-2", "turn-2"]


def test_events_outside_a_turn_are_gated_out():
    """Body events before the first prompt, or after a turn closed, attach nowhere."""
    entries = TranscriptTimeline.assemble(
        [_tool_result("shell"), _user("hi"), _assistant("yo"), _tool_result("orphan")]
    )
    assert _roles(entries) == ["user", "assistant"]
    assert entries[1].turn is not None
    # The opening prompt and the closing event, and neither loose tool_result.
    assert [e["type"] for e in entries[1].turn.events] == ["user", "assistant"]


def test_completion_closes_a_turn_no_assistant_event_did():
    entries = TranscriptTimeline.assemble(
        [_user("go"), _tool_result("shell"), _completion("stop")]
    )
    assert _roles(entries) == ["user", "assistant"]
    assert entries[1].content == "(run ended)"
    assert entries[1].turn is not None
    # The body survives — the whole reason this fallback exists. The closing
    # event rides the turn too, so a reader sees what ended it.
    assert [e["type"] for e in entries[1].turn.events] == ["user", "tool_result", "completion"]


def test_canceled_completion_reads_either_spelling():
    for reason in ("canceled", "cancelled"):
        entries = TranscriptTimeline.assemble([_user("go"), _completion(reason)])
        assert entries[1].content == "(run canceled)"


def test_command_completion_surfaces_its_rendered_body():
    entries = TranscriptTimeline.assemble(
        [_user("/status"), _completion("command", text="All good.")]
    )
    assert entries[1].content == "All good."


def test_context_model_flows_into_the_next_turn():
    entries = TranscriptTimeline.assemble(
        [
            {"type": "context", "ts": "t0", "payload": {"model": "some-model"}},
            _user("hi"),
            _assistant("hello"),
        ]
    )
    assert entries[1].turn is not None
    assert entries[1].turn.model == "some-model"


def test_turn_duration_is_a_raw_span_not_a_label():
    entries = TranscriptTimeline.assemble(
        [
            _user("hi", ts="2020-01-01T00:00:00+00:00"),
            _assistant("hello", ts="2020-01-01T00:00:02+00:00"),
        ]
    )
    assert entries[1].turn is not None
    assert entries[1].turn.duration_ms == 2000


def test_unparseable_timestamps_yield_no_duration():
    entries = TranscriptTimeline.assemble([_user("hi", ts="t0"), _assistant("hello", ts="t9")])
    assert entries[1].turn is not None
    assert entries[1].turn.duration_ms is None


# ---------------------------------------------------------------------------
# The interrupted-turn flush — a turn the next prompt superseded
# ---------------------------------------------------------------------------


def test_turn_superseded_by_the_next_prompt_materialises_with_its_body():
    """The seeded shape: body events, NO closure, then another user event."""
    entries = TranscriptTimeline.assemble(
        [
            _user("do the long thing", ts="t0"),
            _tool_result("shell", ts="t1"),
            _tool_result("shell", ts="t2"),
            _user("what happened?", ts="t5"),
            _assistant("It was cut short.", ts="t6"),
        ]
    )
    assert _roles(entries) == ["user", "run_failed", "user", "assistant"]
    flushed = entries[1]
    assert flushed.run_failure is not None
    assert flushed.run_failure.reason == "interrupted"
    # No closure arrived, so there is no prose to show and none is invented.
    assert flushed.content == ""
    assert flushed.run_failure.text == ""
    # Its body survives intact — the whole point of materialising it.
    assert flushed.turn is not None
    assert len(flushed.turn.events) == 3
    assert flushed.ts == "t2"


def test_trailing_open_turn_is_not_flushed():
    """A still-running turn has no following prompt, so it stays open."""
    entries = TranscriptTimeline.assemble([_user("go"), _tool_result("shell")])
    assert _roles(entries) == ["user"]


def test_concluded_turns_are_never_flushed_as_interrupted():
    entries = TranscriptTimeline.assemble(
        [
            _user("q1"),
            _assistant("a1"),
            _user("q2"),
            _completion("stop", ts="t8"),
            _user("q3"),
            _assistant("a3"),
        ]
    )
    assert "run_failed" not in _roles(entries)


def test_consecutive_interrupted_turns_keep_their_own_bodies():
    entries = TranscriptTimeline.assemble(
        [
            _user("one", ts="t0"),
            _tool_result("shell", ts="t1"),
            _user("two", ts="t2"),
            _tool_result("shell", ts="t3"),
            _tool_result("shell", ts="t4"),
            _user("three", ts="t5"),
            _assistant("finally", ts="t6"),
        ]
    )
    flushed = [e for e in entries if e.role == "run_failed"]
    assert len(flushed) == 2
    assert [len(e.turn.events) for e in flushed if e.turn] == [2, 3]


# ---------------------------------------------------------------------------
# Roles hoisted above the open-turn gate
# ---------------------------------------------------------------------------


def test_trigger_fired_between_turns_is_kept():
    """The common case: a fired trigger wakes an idle session BETWEEN turns."""
    entries = TranscriptTimeline.assemble(
        [
            _user("q1"),
            _assistant("a1"),
            {
                "type": "trigger_fired",
                "ts": "t5",
                "payload": {"trigger_id": "tr1", "kind": "time.at", "payload_summary": "woke"},
            },
            _user("q2"),
            _assistant("a2"),
        ]
    )
    assert _roles(entries) == ["user", "assistant", "trigger", "user", "assistant"]
    marker = entries[2]
    assert marker.trigger is not None
    assert marker.trigger.action == "fired"
    assert marker.trigger.summary == "woke"
    # No turn was open, so it is parked on the standalone bucket.
    assert marker.turn_id == "triggers"


def test_trigger_armed_mid_turn_belongs_to_that_turn():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            {
                "type": "trigger_armed",
                "ts": "t1",
                "payload": {"trigger_id": "tr1", "kind": "time.cron", "summary": "armed"},
            },
            _assistant("done"),
        ]
    )
    assert entries[1].trigger is not None
    assert entries[1].trigger.action == "armed"
    assert entries[1].trigger.summary == "armed"
    assert entries[1].turn_id == "turn-1"


def test_armed_and_fired_read_different_summary_fields():
    """A crossed read yields a marker with no explanation of why the session woke."""
    fired = TranscriptTimeline.assemble(
        [
            {
                "type": "trigger_fired",
                "ts": "t1",
                "payload": {"summary": "wrong field", "payload_summary": "right field"},
            }
        ]
    )
    assert fired[0].trigger is not None
    assert fired[0].trigger.summary == "right field"


def test_trigger_kind_falls_back_to_a_generic_label():
    entries = TranscriptTimeline.assemble(
        [{"type": "trigger_armed", "ts": "t1", "payload": {"kind": ""}}]
    )
    assert entries[0].trigger is not None
    assert entries[0].trigger.kind == "trigger"


def test_session_terminated_is_kept_and_never_attached_to_a_turn():
    entries = TranscriptTimeline.assemble(
        [_user("q1"), {"type": "session_terminated", "ts": "t2", "payload": {}}, _assistant("a1")]
    )
    assert _roles(entries) == ["user", "session_terminated", "assistant"]
    assert entries[1].turn_id == "session-terminated"


def test_user_recovery_between_turns_becomes_a_marker():
    """The seeded shape: a retry/continue recorded after a failed turn closed."""
    for action in ("retry", "continue"):
        entries = TranscriptTimeline.assemble(
            [
                _user("task"),
                _completion("error", ts="t2"),
                {"type": "recovery", "ts": "t5", "payload": {"action": action}},
                _user("resumed", ts="t6"),
                _assistant("recovered", ts="t7"),
            ]
        )
        assert _roles(entries) == ["user", "run_failed", "recovery", "user", "assistant"]
        assert entries[2].recovery is not None
        assert entries[2].recovery.action == action
        assert entries[2].turn_id == "recovery"


def test_engine_halt_recovery_is_not_a_conversation_marker():
    """``halt_no_progress`` is the loop's own trace detail, not a user action.

    It must fall THROUGH to the turn's events rather than be hoisted — a marker
    for it would claim the user recovered a run when nobody did.
    """
    entries = TranscriptTimeline.assemble(
        [
            _user("task"),
            {"type": "recovery", "ts": "t1", "payload": {"action": "halt_no_progress"}},
            _assistant("stopped early"),
        ]
    )
    assert "recovery" not in _roles(entries)
    assert entries[1].turn is not None
    # It joined the turn's events instead of vanishing.
    assert any(e.get("type") == "recovery" for e in entries[1].turn.events)


def test_malformed_recovery_action_yields_no_marker():
    for payload in ({}, {"action": 7}, {"action": "nonsense"}):
        entries = TranscriptTimeline.assemble(
            [_user("task"), {"type": "recovery", "ts": "t1", "payload": payload}]
        )
        assert "recovery" not in _roles(entries)


def _compacted(ts: str = "t5", **payload) -> dict:
    return {"type": "context_compacted", "ts": ts, "payload": payload}


def test_compaction_between_turns_is_a_marker():
    entries = TranscriptTimeline.assemble(
        [
            _user("q1"),
            _assistant("a1"),
            _compacted(ts="t5", depth=0, mode="auto", tokens_saved=4200),
            _user("q2"),
            _assistant("a2"),
        ]
    )
    assert _roles(entries) == ["user", "assistant", "compaction", "user", "assistant"]
    marker = entries[2]
    assert marker.compaction is not None
    assert marker.compaction.mode == "auto"
    assert marker.compaction.tokens_saved == 4200
    assert marker.turn_id == "compaction"


def test_reactive_compaction_renders_inside_the_open_turn():
    """Unlike recovery, this is NOT restricted to arriving between turns.

    A mid-loop compaction fires with a turn open and must render where the
    horizon actually moved.
    """
    entries = TranscriptTimeline.assemble(
        [
            _user("go", ts="t0"),
            _tool_result("shell", ts="t1"),
            _compacted(ts="t2", depth=0, mode="reactive"),
            _assistant("done", ts="t3"),
        ]
    )
    assert _roles(entries) == ["user", "compaction", "assistant"]
    assert entries[1].turn_id == "turn-1"
    assert entries[1].compaction is not None
    assert entries[1].compaction.mode == "reactive"


def test_sub_agent_compaction_never_reaches_the_conversation():
    """A sub-agent narrows its OWN isolated context, not what the root receives.

    Surfacing it here would attribute a sub-context's narrowing to the thread
    being read.
    """
    entries = TranscriptTimeline.assemble(
        [
            _user("go", ts="t0"),
            _compacted(ts="t1", depth=1, mode="reactive", tokens_saved=9000),
            _assistant("done", ts="t2"),
        ]
    )
    assert "compaction" not in _roles(entries)


def test_sub_agent_compaction_is_consumed_not_folded_into_the_turn():
    """The skip is total: it does not fall through into the turn's events.

    Recovery deliberately DOES fall through; compaction deliberately does not.
    Asserting the difference keeps a future edit from unifying the two.
    """
    entries = TranscriptTimeline.assemble(
        [_user("go", ts="t0"), _compacted(ts="t1", depth=2), _assistant("done", ts="t2")]
    )
    assert entries[1].turn is not None
    assert not any(e.get("type") == "context_compacted" for e in entries[1].turn.events)


def test_compaction_mode_defaults_to_auto():
    """An event recorded before `mode` existed still reads sensibly."""
    entries = TranscriptTimeline.assemble([_compacted(ts="t1", depth=0)])
    assert entries[0].compaction is not None
    assert entries[0].compaction.mode == "auto"
    assert entries[0].compaction.tokens_saved is None


def test_compaction_missing_depth_is_treated_as_root():
    """Depth is absent on events predating the field; those are root events."""
    entries = TranscriptTimeline.assemble([_compacted(ts="t1", mode="auto")])
    assert _roles(entries) == ["compaction"]


# ---------------------------------------------------------------------------
# The synthetic-vs-real closure distinction on a failure completion
# ---------------------------------------------------------------------------


def test_failure_completion_upgrades_a_synthetic_closure_in_place():
    """The orchestrator's placeholder carries nothing the failure record lacks."""
    entries = TranscriptTimeline.assemble(
        [
            _user("go", ts="t0"),
            _assistant("(Run interrupted by error: upstream died)", ts="t8"),
            _completion("error", ts="t9", error="upstream died"),
        ]
    )
    # Upgraded IN PLACE — one entry per turn, so the turn metadata stays put.
    assert _roles(entries) == ["user", "run_failed"]
    upgraded = entries[1]
    assert upgraded.content == ""
    assert upgraded.run_failure is not None
    assert upgraded.run_failure.reason == "error"
    assert upgraded.run_failure.text == "upstream died"
    assert upgraded.turn is not None  # metadata survived the upgrade
    assert upgraded.ts == "t9"


def test_failure_completion_on_real_prose_preserves_the_answer():
    """A boot sweep appends a terminal completion to an orphaned run.

    The assistant text is then the only copy of what the run produced, so the
    failure is surfaced BESIDE it rather than blanking it.
    """
    entries = TranscriptTimeline.assemble(
        [
            _user("go", ts="t0"),
            _assistant("Here is the real answer.", ts="t8"),
            _completion("error", ts="t9", error="run died"),
        ]
    )
    assert _roles(entries) == ["user", "assistant", "run_failed"]
    assert entries[1].content == "Here is the real answer."
    assert entries[2].run_failure is not None
    assert entries[2].run_failure.text == "run died"
    # The turn meta stays on the assistant entry — only one footer's worth.
    assert entries[1].turn is not None
    assert entries[2].turn is None
    assert entries[2].turn_id == entries[1].turn_id


def test_prose_merely_opening_with_a_parenthetical_is_not_a_placeholder():
    """The match is anchored at BOTH ends for exactly this case."""
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            _assistant("(Run the tests first.) Then deploy.", ts="t8"),
            _completion("error", ts="t9", error="died"),
        ]
    )
    assert _roles(entries) == ["user", "assistant", "run_failed"]
    assert entries[1].content == "(Run the tests first.) Then deploy."


def test_a_non_failing_completion_after_a_closure_changes_nothing():
    entries = TranscriptTimeline.assemble(
        [_user("go"), _assistant("answer", ts="t8"), _completion("stop", ts="t9")]
    )
    assert _roles(entries) == ["user", "assistant"]
    assert entries[1].content == "answer"


def test_a_failure_cannot_reach_back_past_a_settled_turn():
    """Once a turn's completion is seen, a later one must not rewrite it."""
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            _assistant("(Run stopped: x)", ts="t8"),
            _completion("stop", ts="t9"),
            _completion("error", ts="t10", error="late"),
        ]
    )
    assert _roles(entries) == ["user", "assistant"]
    assert entries[1].content == "(Run stopped: x)"


def test_failure_reason_comes_from_done_reason_not_the_closure_text():
    """``max_steps_reached`` is a failure; the text is a formatting detail."""
    entries = TranscriptTimeline.assemble(
        [_user("go"), _completion("max_steps_reached", ts="t9", error="ran out")]
    )
    assert entries[1].role == "run_failed"
    assert entries[1].run_failure is not None
    assert entries[1].run_failure.reason == "max_steps_reached"


def test_classified_error_detail_is_preferred_over_the_legacy_string():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            _completion(
                "error",
                ts="t9",
                error="legacy text",
                error_detail={
                    "kind": "rate_limited",
                    "title": "Rate limited",
                    "provider": "some-model",
                    "detail": "slow down",
                    "detail_chars": 9,
                    "truncated": False,
                },
            ),
        ]
    )
    failure = entries[1].run_failure
    assert failure is not None
    assert failure.detail is not None
    assert failure.detail.kind == "rate_limited"
    assert failure.text == "slow down"


def test_unrecognized_error_kind_degrades_rather_than_raising():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            _completion(
                "error",
                ts="t9",
                error_detail={"kind": "not-a-kind", "title": "x", "detail": "d"},
            ),
        ]
    )
    failure = entries[1].run_failure
    assert failure is not None
    assert failure.detail is not None
    assert failure.detail.kind == "unknown"


def test_malformed_error_detail_degrades_to_the_legacy_string():
    entries = TranscriptTimeline.assemble(
        [_user("go"), _completion("error", ts="t9", error="boom", error_detail="not-a-dict")]
    )
    failure = entries[1].run_failure
    assert failure is not None
    assert failure.detail is None
    assert failure.text == "boom"


# ---------------------------------------------------------------------------
# Parity gaps the MCP port used to render nothing for
# ---------------------------------------------------------------------------


def test_plan_is_rendered_and_settled_by_its_approval():
    entries = TranscriptTimeline.assemble(
        [
            _user("plan it"),
            {
                "type": "plan_proposed",
                "ts": "t1",
                "payload": {"revision": 2, "content": "# Plan", "summary": "do it"},
            },
            {"type": "plan_approved", "ts": "t2", "payload": {"revision": 2}},
            _assistant("done"),
        ]
    )
    assert "plan" in _roles(entries)
    plan = next(e for e in entries if e.role == "plan").plan
    assert plan is not None
    assert plan.revision == 2
    assert plan.status == "approved"
    assert plan.plan_content == "# Plan"


def test_plan_rejection_settles_the_matching_revision_only():
    entries = TranscriptTimeline.assemble(
        [
            _user("plan it"),
            {"type": "plan_proposed", "ts": "t1", "payload": {"revision": 1, "content": "a"}},
            {"type": "plan_proposed", "ts": "t2", "payload": {"revision": 2, "content": "b"}},
            {"type": "plan_rejected", "ts": "t3", "payload": {"revision": 1}},
        ]
    )
    plans = [e.plan for e in entries if e.role == "plan" and e.plan]
    assert [(p.revision, p.status) for p in plans] == [(1, "rejected"), (2, "pending")]


def test_todos_upsert_into_one_card_per_turn():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            {
                "type": "todos",
                "ts": "t1",
                "payload": {"items": [{"label": "step one", "status": "pending"}]},
            },
            {
                "type": "todos",
                "ts": "t2",
                "payload": {
                    "items": [
                        {"label": "step one", "status": "done"},
                        {"label": "step two", "status": "in_progress"},
                    ],
                    "source": "agent",
                },
            },
            _assistant("done"),
        ]
    )
    todo_entries = [e for e in entries if e.role == "todos"]
    assert len(todo_entries) == 1  # updated in place, not stacked
    todos = todo_entries[0].todos
    assert todos is not None
    # The CLI's `done` spelling is normalized so either producer renders alike.
    assert [(i.label, i.status) for i in todos.items] == [
        ("step one", "completed"),
        ("step two", "in_progress"),
    ]
    assert todos.source == "agent"


def test_empty_or_labelless_todos_are_never_fabricated():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            {"type": "todos", "ts": "t1", "payload": {"items": []}},
            {"type": "todos", "ts": "t2", "payload": {"items": [{"label": "   "}]}},
        ]
    )
    assert "todos" not in _roles(entries)


def test_todos_are_scoped_per_turn():
    entries = TranscriptTimeline.assemble(
        [
            _user("q1"),
            {"type": "todos", "ts": "t1", "payload": {"items": [{"label": "a"}]}},
            _assistant("a1"),
            _user("q2"),
            {"type": "todos", "ts": "t3", "payload": {"items": [{"label": "b"}]}},
            _assistant("a2"),
        ]
    )
    todo_entries = [e for e in entries if e.role == "todos"]
    assert len(todo_entries) == 2
    assert [e.turn_id for e in todo_entries] == ["turn-1", "turn-2"]


def test_widget_is_rendered():
    entries = TranscriptTimeline.assemble(
        [
            _user("chart it"),
            {"type": "widget_ready", "ts": "t1", "payload": {"widget_id": "w1", "kind": "chart"}},
            _assistant("done"),
        ]
    )
    assert "widget" in _roles(entries)
    widget = next(e for e in entries if e.role == "widget").widget
    assert widget is not None
    assert widget["widget_id"] == "w1"


def test_question_opens_pending_and_settles_in_place():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            {
                "type": "user_question",
                "ts": "t1",
                "payload": {
                    "call_id": "c1",
                    "call_token": "secret-token",
                    "questions": [{"header": "Scope", "question": "How far?"}],
                },
            },
            {
                "type": "user_question_answered",
                "ts": "t2",
                "payload": {
                    "call_id": "c1",
                    "outcome": "answered",
                    "answers": [{"selected_indexes": [0]}],
                    "answered_via": "console",
                },
            },
            _assistant("done"),
        ]
    )
    question_entries = [e for e in entries if e.role == "question"]
    assert len(question_entries) == 1  # settled in place, not a second card
    question = question_entries[0].question
    assert question is not None
    assert question.status == "answered"
    assert question.answers == [{"selected_indexes": [0]}]
    assert question.answered_via == "console"


def test_question_never_carries_the_answer_credential():
    """The call_token is a bearer secret; a read projection must not mint it."""
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            {
                "type": "user_question",
                "ts": "t1",
                "payload": {"call_id": "c1", "call_token": "secret-token", "questions": []},
            },
        ]
    )
    question = next(e for e in entries if e.role == "question").question
    assert question is not None
    assert "call_token" not in question.model_dump()
    assert "secret-token" not in question.model_dump_json()


def test_non_answered_outcomes_carry_no_answers():
    for outcome in ("declined", "interrupted", "cancelled"):
        entries = TranscriptTimeline.assemble(
            [
                _user("go"),
                {"type": "user_question", "ts": "t1", "payload": {"call_id": "c1"}},
                {
                    "type": "user_question_answered",
                    "ts": "t2",
                    "payload": {
                        "call_id": "c1",
                        "outcome": outcome,
                        "answers": [{"text": "ignored"}],
                    },
                },
            ]
        )
        question = next(e for e in entries if e.role == "question").question
        assert question is not None
        assert question.status == outcome
        assert question.answers is None


def test_question_answered_with_no_pending_card_is_ignored():
    entries = TranscriptTimeline.assemble(
        [
            _user("go"),
            {
                "type": "user_question_answered",
                "ts": "t1",
                "payload": {"call_id": "ghost", "outcome": "answered"},
            },
        ]
    )
    assert "question" not in _roles(entries)


def test_question_outside_a_turn_is_gated_out():
    """A question is only ever asked mid-run, so a turn-less one is impossible."""
    entries = TranscriptTimeline.assemble(
        [
            _user("q1"),
            _assistant("a1"),
            {"type": "user_question", "ts": "t2", "payload": {"call_id": "c1"}},
        ]
    )
    assert "question" not in _roles(entries)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def test_user_event_attachments_ride_the_entry():
    descriptor = {"id": "a1", "filename": "spec.pdf", "size_bytes": 2048}
    entries = TranscriptTimeline.assemble(
        [_user("see attached", attachments=[descriptor]), _assistant("ok")]
    )
    assert entries[0].attachments == [descriptor]


def test_context_attachments_are_the_fallback_and_do_not_leak_forward():
    """A context event's attachments belong to the ONE turn it precedes."""
    descriptor = {"id": "a1", "filename": "old.pdf"}
    entries = TranscriptTimeline.assemble(
        [
            {"type": "context", "ts": "t0", "payload": {"attachments": [descriptor]}},
            _user("first"),
            _assistant("ok"),
            _user("second"),
            _assistant("ok again"),
        ]
    )
    assert entries[0].attachments == [descriptor]
    assert entries[2].attachments is None


def test_malformed_attachments_degrade_to_none():
    for value in ("not-a-list", ["not-a-dict", 42]):
        entries = TranscriptTimeline.assemble([_user("hi", attachments=value)])
        assert entries[0].attachments is None


# ---------------------------------------------------------------------------
# Token rollups
# ---------------------------------------------------------------------------


def test_input_is_peak_and_output_is_summed():
    """Summing input double-counts the baseline prompt once per step."""
    usage = TurnTokenUsage.from_events(
        [
            _llm_call_end(depth=0, input_tokens=100, output_tokens=10),
            _llm_call_end(depth=0, input_tokens=250, output_tokens=20),
            _llm_call_end(depth=0, input_tokens=180, output_tokens=5),
        ]
    )
    assert usage is not None
    assert usage.input_tokens == 250  # peak, not 530
    assert usage.output_tokens == 35
    assert usage.billed_input_tokens == 530  # the cost-side companion


def test_sub_agent_input_sums_per_agent_peaks():
    usage = TurnTokenUsage.from_events(
        [
            _llm_call_end(depth=1, agent_id="a", input_tokens=50, output_tokens=4),
            _llm_call_end(depth=1, agent_id="a", input_tokens=80, output_tokens=6),
            _llm_call_end(depth=1, agent_id="b", input_tokens=30, output_tokens=2),
        ]
    )
    assert usage is not None
    assert usage.sub_input_tokens == 110  # peak(a)=80 + peak(b)=30
    assert usage.sub_output_tokens == 12
    assert usage.sub_agent_count == 2


def test_cache_and_reasoning_are_summed():
    usage = TurnTokenUsage.from_events(
        [
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
    )
    assert usage is not None
    assert usage.cache_creation_tokens == 20
    assert usage.cache_read_tokens == 20
    assert usage.reasoning_tokens == 10


def test_no_token_activity_yields_none():
    assert TurnTokenUsage.from_events([_user("hi"), _assistant("yo")]) is None


def test_usage_is_scoped_to_its_own_turn():
    entries = TranscriptTimeline.assemble(
        [
            _user("q1"),
            _llm_call_end(depth=0, input_tokens=100, output_tokens=10),
            _assistant("a1"),
            _user("q2"),
            _llm_call_end(depth=0, input_tokens=999, output_tokens=99),
            _assistant("a2"),
        ]
    )
    turns = [e.turn for e in entries if e.turn]
    assert [t.token_usage.input_tokens for t in turns if t.token_usage] == [100, 999]


# ---------------------------------------------------------------------------
# Shape contracts
# ---------------------------------------------------------------------------


def test_emitted_models_forbid_unknown_fields():
    """extra="forbid" is what turns a renamed field into a loud failure."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TimelineEntry(id="x", role="user", turn_id="turn-1", bogus="smuggled")


def test_assemble_is_pure_and_repeatable():
    events = [_user("hi"), _tool_result("shell"), _assistant("hello")]
    snapshot = [dict(e) for e in events]
    first = TranscriptTimeline.assemble(events)
    second = TranscriptTimeline.assemble(events)
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]
    assert events == snapshot  # the input is never mutated


def test_turns_helper_returns_turn_metadata_only():
    turns = TranscriptTimeline.turns([_user("q1"), _assistant("a1"), _user("q2"), _assistant("a2")])
    assert [t.id for t in turns] == ["turn-1", "turn-2"]


def test_empty_transcript_yields_nothing():
    assert TranscriptTimeline.assemble([]) == []
