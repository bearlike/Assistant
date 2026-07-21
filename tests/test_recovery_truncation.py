"""Recovery truncation: a ``continue`` must never delete durable prior work.

``resolve_recovery_query("continue")`` deletes a stale prior continue attempt so
the transcript stops accreting duplicate recovery prompts. The staleness test is
the whole game: cut too eagerly and a finished turn is physically gone from the
store. These tests pin the boundary from the caller's side — every assertion is
about the transcript the STORE holds afterwards, never about internals.
"""

from mewbo_core.config import set_config_override
from mewbo_core.context import ContextBuilder
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore


def _runtime(tmp_path) -> tuple[SessionStore, SessionRuntime, str]:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    return store, runtime, runtime.resolve_session()


def _texts(transcript: list[dict]) -> list[str]:
    return [e["payload"]["text"] for e in transcript if e["type"] == "user"]


# ---------------------------------------------------------------------------
# The durable-data-loss contract
# ---------------------------------------------------------------------------


def test_continue_keeps_completed_turns_that_follow_a_recovery_marker(tmp_path):
    """A recovery marker followed by FINISHED turns is not stale.

    The marker only records that a continue was once triggered; it says nothing
    about what came after. When the attempt it started went on to complete a
    turn (and another turn after that), cutting at the marker physically
    deletes both — real, user-visible data loss.
    """
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(sid, {"type": "user", "payload": {"text": "original task"}})
    store.append_event(sid, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "completed"}}
    )
    # A prior continue — and this time it produced real, finished work.
    store.append_event(sid, {"type": "recovery", "payload": {"action": "continue"}})
    store.append_event(sid, {"type": "user", "payload": {"text": "recovered turn"}})
    store.append_event(
        sid, {"type": "tool_result", "payload": {"tool_id": "scg_memory", "result": "ok"}}
    )
    store.append_event(sid, {"type": "assistant", "payload": {"text": "a2"}})
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "completed"}}
    )

    runtime.resolve_recovery_query(sid, "continue")

    transcript = store.load_transcript(sid)
    assert _texts(transcript) == ["original task", "recovered turn"]
    assert sum(1 for e in transcript if e["type"] == "completion") == 2
    assert any(
        e["type"] == "tool_result" and e["payload"].get("tool_id") == "scg_memory"
        for e in transcript
    ), "completed turn's tool traces must survive"
    # Only the NEW audit marker is added; nothing is removed.
    assert sum(1 for e in transcript if e["type"] == "recovery") == 2


def test_continue_keeps_tool_traces_from_an_unfinished_attempt(tmp_path):
    """Tool work after the marker survives even with no completion.

    A run killed mid-flight emits its tool results but never a completion. Those
    traces are the expensive, irreplaceable part of the transcript, so an
    attempt that executed tools is not stale no matter how it ended.
    """
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(sid, {"type": "user", "payload": {"text": "original task"}})
    store.append_event(sid, {"type": "recovery", "payload": {"action": "continue"}})
    store.append_event(sid, {"type": "user", "payload": {"text": "recovered turn"}})
    store.append_event(
        sid, {"type": "tool_result", "payload": {"tool_id": "scg_memory", "result": "ok"}}
    )

    runtime.resolve_recovery_query(sid, "continue")

    transcript = store.load_transcript(sid)
    assert _texts(transcript) == ["original task", "recovered turn"]
    assert any(e["type"] == "tool_result" for e in transcript)


def test_continue_keeps_an_assistant_answer_with_no_completion(tmp_path):
    """An answer the user already read survives, completion or not.

    ``Orchestrator.run`` appends the assistant event and the completion as
    separate writes with a title-generation call between them, so a run that
    dies in that window leaves a real answer behind with no completion after
    it. Treating that as a bare unanswered re-prompt would delete text the
    user has already seen on screen.
    """
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(sid, {"type": "user", "payload": {"text": "original task"}})
    store.append_event(sid, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "completed"}}
    )
    store.append_event(sid, {"type": "recovery", "payload": {"action": "continue"}})
    store.append_event(sid, {"type": "user", "payload": {"text": "recovered turn"}})
    # The answer landed; the process died before the completion could follow.
    store.append_event(sid, {"type": "assistant", "payload": {"text": "the answer"}})

    runtime.resolve_recovery_query(sid, "continue")

    transcript = store.load_transcript(sid)
    assert _texts(transcript) == ["original task", "recovered turn"]
    assert any(
        e["type"] == "assistant" and e["payload"]["text"] == "the answer" for e in transcript
    ), "an assistant answer with no completion behind it must survive"


def test_continue_drops_an_attempt_that_produced_nothing(tmp_path):
    """The stale case current behavior exists for: a bare synthetic re-prompt.

    The prior continue appended its marker and its re-prompt, then produced no
    tool work and never finished. Nothing durable is lost by cutting it, and
    keeping it would accrete duplicate recovery prompts.
    """
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(sid, {"type": "user", "payload": {"text": "real work"}})
    store.append_event(sid, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "completed"}}
    )
    store.append_event(sid, {"type": "recovery", "payload": {"action": "continue"}})
    store.append_event(sid, {"type": "user", "payload": {"text": "stale continue prompt"}})

    runtime.resolve_recovery_query(sid, "continue")

    transcript = store.load_transcript(sid)
    assert _texts(transcript) == ["real work"]
    assert sum(1 for e in transcript if e["type"] == "completion") == 1
    # The stale marker went with it; only the fresh one remains.
    assert sum(1 for e in transcript if e["type"] == "recovery") == 1


def test_continue_with_no_recovery_marker_truncates_nothing(tmp_path):
    """No prior marker means nothing is stale — the existing invariant."""
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(sid, {"type": "user", "payload": {"text": "T1"}})
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "completed"}}
    )
    store.append_event(sid, {"type": "user", "payload": {"text": "T2"}})
    store.append_event(
        sid, {"type": "tool_result", "payload": {"tool_id": "scg_memory", "result": "ok"}}
    )

    runtime.resolve_recovery_query(sid, "continue")

    transcript = store.load_transcript(sid)
    assert _texts(transcript) == ["T1", "T2"]
    assert any(e["type"] == "tool_result" for e in transcript)


def test_retry_deletes_only_the_failed_turn(tmp_path):
    """Retry stays time-travel: the failed turn goes, prior turns stay."""
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(sid, {"type": "user", "payload": {"text": "first"}})
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "completed"}}
    )
    store.append_event(sid, {"type": "user", "payload": {"text": "second"}})
    store.append_event(
        sid, {"type": "tool_result", "payload": {"tool_id": "x", "result": "boom"}}
    )
    store.append_event(
        sid, {"type": "completion", "payload": {"done": True, "done_reason": "error"}}
    )

    assert runtime.resolve_recovery_query(sid, "retry") == "second"

    transcript = store.load_transcript(sid)
    assert _texts(transcript) == ["first"]
    assert sum(1 for e in transcript if e["type"] == "completion") == 1
    assert not any(e["type"] == "recovery" for e in transcript)


# ---------------------------------------------------------------------------
# Mixed offset spellings — ISO strings do not sort chronologically
# ---------------------------------------------------------------------------
#
# ``Z`` (0x5A) sorts ABOVE ``+`` (0x2B), so two events stamped in the same
# second by producers that spell UTC differently compare in the wrong order.
# Both seams below must key off append order, which is the transcript's actual
# ground truth, rather than off a string comparison.


def test_continue_survives_mixed_offset_spellings(tmp_path):
    """A same-second ``Z``/``+00:00`` mix must not widen the cut.

    Scanning for the first event whose ts string sorts at or above the marker's
    stops early here — ``…03Z`` sorts above ``…03+00:00`` — and takes the
    completed turn with it.
    """
    store, runtime, sid = _runtime(tmp_path)
    store.append_event(
        sid, {"type": "user", "payload": {"text": "real work"}, "ts": "2026-01-01T00:00:01+00:00"}
    )
    store.append_event(
        sid,
        {
            "type": "tool_result",
            "payload": {"tool_id": "scg_memory", "result": "ok"},
            "ts": "2026-01-01T00:00:02Z",
        },
    )
    store.append_event(
        sid,
        {
            "type": "completion",
            "payload": {"done": True, "done_reason": "completed"},
            "ts": "2026-01-01T00:00:03Z",
        },
    )
    store.append_event(
        sid,
        {
            "type": "recovery",
            "payload": {"action": "continue"},
            "ts": "2026-01-01T00:00:03+00:00",
        },
    )
    store.append_event(
        sid,
        {
            "type": "user",
            "payload": {"text": "stale continue prompt"},
            "ts": "2026-01-01T00:00:04Z",
        },
    )

    runtime.resolve_recovery_query(sid, "continue")

    transcript = store.load_transcript(sid)
    assert "real work" in _texts(transcript)
    assert any(e["type"] == "completion" for e in transcript), (
        "the completed turn must survive a same-second offset-spelling mix"
    )
    assert any(e["type"] == "tool_result" for e in transcript)


def test_compaction_boundary_survives_mixed_offset_spellings(tmp_path):
    """A post-boundary event must stay visible whatever its offset spelling.

    Filtering on ``ts > boundary_ts`` as strings drops a post-boundary event
    stamped ``…03+00:00`` when the boundary itself is stamped ``…03Z``.
    """
    set_config_override(
        {"context": {"recent_event_limit": 8, "selection_enabled": False}, "llm": {}}
    )
    store = SessionStore(root_dir=str(tmp_path))
    builder = ContextBuilder(store)
    sid = store.create_session()
    store.append_event(
        sid, {"type": "user", "payload": {"text": "old task"}, "ts": "2026-01-01T00:00:01+00:00"}
    )
    store.append_event(
        sid,
        {
            "type": "context_compacted",
            "payload": {
                "mode": "user",
                "model": "m",
                "tokens_before": 100,
                "tokens_saved": 80,
                "tokens_after": 20,
                "events_summarized": 1,
                "summary": "old work summarized",
                "fallback": False,
            },
            "ts": "2026-01-01T00:00:03Z",
        },
    )
    store.save_summary(sid, "old work summarized")
    store.append_event(
        sid, {"type": "user", "payload": {"text": "new task"}, "ts": "2026-01-01T00:00:03+00:00"}
    )

    snapshot = builder.build(sid, "new task", model_name=None)

    texts = [e.get("payload", {}).get("text", "") for e in snapshot.recent_events]
    assert "new task" in texts, "post-boundary event must not be dropped by a spelling mix"
    assert "old task" not in texts, "pre-boundary events stay inside the summary"
