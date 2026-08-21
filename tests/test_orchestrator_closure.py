"""Tests for synthetic assistant closure events emitted on run termination.

The orchestrator must emit exactly one ``assistant`` event per user turn —
a real response when ``task_result`` is populated, otherwise a synthetic
closure marker (e.g. ``(Run interrupted by error: …)``) so the frontend
timeline can finalise turn metadata and the LLM's ``recent_events``
carries narrative closure into any subsequent recovery run.
"""

from __future__ import annotations

from unittest.mock import patch

from mewbo_core.classes import OrchestrationState, TaskQueue
from mewbo_core.loop.orchestrator import Orchestrator, _format_assistant_closure
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.session.session_store import SessionStore


async def _failing_loop_run(*_args, **_kwargs):
    """Replacement for ``ToolUseLoop.run`` that raises immediately."""
    raise RuntimeError("LLM call exceeded 180s ceiling")


_STALE_ERROR = "ERROR: Tool 'aider_shell_tool' timed out after 120.0s"


async def _successful_loop_run_with_stale_error(*_args, **_kwargs):
    """A run that recovered from a mid-run tool failure and finished clean.

    ``task_queue.last_error`` stays sticky (sole most-recent-failure
    diagnostic) even though the run completed successfully — the
    completion payload must not surface it as an active error.
    """
    task_queue = TaskQueue(action_steps=[])
    task_queue.task_result = "All done."
    task_queue.last_error = _STALE_ERROR
    state = OrchestrationState(goal="go")
    state.done = True
    state.done_reason = "completed"
    return task_queue, state


async def _halted_loop_run_with_error(*_args, **_kwargs):
    """A run that stopped short of its goal AND left a sticky error.

    Reaches ``_attach_failure_record`` (unlike ``_failing_loop_run``, which
    raises and is served by the handler's own payload), so it is the only
    fixture that exercises the non-``completed`` arm of the terminal-status
    gate — i.e. that withholding ``error`` from a success did not withhold it
    from a genuine failure too.
    """
    task_queue = TaskQueue(action_steps=[])
    task_queue.task_result = ""
    task_queue.last_error = _STALE_ERROR
    state = OrchestrationState(goal="go")
    state.done = True
    state.done_reason = "halted_no_progress"
    return task_queue, state


class TestFormatAssistantClosure:
    """Direct coverage for the closure formatter."""

    def test_error_closure_embeds_error_message(self) -> None:
        text = _format_assistant_closure("error", "litellm.Timeout: boom")
        assert text.startswith("(Run interrupted by error:")
        assert "litellm.Timeout: boom" in text

    def test_error_closure_truncates_huge_error_strings(self) -> None:
        huge = "x" * 5000
        text = _format_assistant_closure("error", huge)
        assert text.startswith("(Run interrupted by error:")
        assert len(text) < 700  # well under the 500-char budget + prefix
        assert text.endswith("…)")

    def test_error_closure_without_last_error_has_fallback(self) -> None:
        text = _format_assistant_closure("error", None)
        assert "unknown error" in text

    def test_max_steps_reached_closure(self) -> None:
        text = _format_assistant_closure("max_steps_reached", None)
        assert "step limit" in text.lower()

    def test_budget_exhausted_closure(self) -> None:
        text = _format_assistant_closure("budget_exhausted", None)
        assert "step budget" in text.lower()

    def test_canceled_closure(self) -> None:
        text = _format_assistant_closure("canceled", None)
        assert "canceled" in text.lower()

    def test_unknown_reason_closure(self) -> None:
        text = _format_assistant_closure("weird", None)
        assert "weird" in text


def _make_orchestrator(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    return Orchestrator(session_store=store), store


def _assistant_events(store, session_id):
    return [e for e in store.load_transcript(session_id) if e.get("type") == "assistant"]


def _completion_events(store, session_id):
    return [e for e in store.load_transcript(session_id) if e.get("type") == "completion"]


class TestRunFailureEmitsClosure:
    """Failed runs must write a synthetic assistant event before completion."""

    def test_exception_path_emits_closure_before_completion(self, tmp_path) -> None:
        """ToolUseLoop raising mid-run → closure event before completion."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        # Replace the loop's ``run`` with a coroutine that raises on
        # await — matches what happens when ``asyncio.wait_for`` trips
        # the ``llm_call_timeout`` ceiling inside ``ToolUseLoop``.
        with patch.object(ToolUseLoop, "run", _failing_loop_run):
            orch.run(user_query="hi", session_id=session_id, max_iters=1)

        transcript = store.load_transcript(session_id)
        # Find the completion event + the assistant event preceding it.
        completion_idx = next(i for i, e in enumerate(transcript) if e.get("type") == "completion")
        # Closure assistant must exist and sit immediately before completion.
        prev = transcript[completion_idx - 1]
        assert prev["type"] == "assistant"
        assert prev["payload"]["text"].startswith("(Run interrupted by error:")
        assert "LLM call exceeded 180s ceiling" in prev["payload"]["text"]
        # Completion payload reflects the error.
        assert transcript[completion_idx]["payload"]["done_reason"] == "error"

    def test_exactly_one_assistant_per_turn_on_failure(self, tmp_path) -> None:
        """The failed turn must produce exactly one assistant event."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _failing_loop_run):
            orch.run(user_query="go", session_id=session_id, max_iters=1)

        # Exactly one assistant event, exactly one completion event.
        assert len(_assistant_events(store, session_id)) == 1
        assert len(_completion_events(store, session_id)) == 1


class TestCompletionPayloadErrorGating:
    """A sticky ``last_error`` is CARRIED and bounded on a clean run — but the
    render-triggering ``error`` key is withheld from one."""

    def test_successful_completion_still_reports_its_sticky_error(self, tmp_path) -> None:
        """The RECORD is carried on a success; the error CARD trigger is not.

        ``error`` is the one key a client renders as a user-facing error card,
        so emitting it from recovered-tool residue put an error under a
        complete, correct answer. Withholding it does not weaken the honesty
        guarantee that kept these keys unconditional: ``last_error`` and
        ``error_detail`` still ride the payload, so a LAUNDERED run — a halt
        presenting as success — is still contradicted by its own record. Only
        the render trigger goes; the status derivation stays the honesty layer
        above it.
        """
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _successful_loop_run_with_stale_error):
            orch.run(user_query="go", session_id=session_id, max_iters=1)

        payload = _completion_events(store, session_id)[0]["payload"]
        assert payload["done_reason"] == "completed"
        # The card trigger is absent — this is the whole point.
        assert "error" not in payload
        # ...but the record is carried, and bounded through RunError like every
        # other emission path, so the status stays falsifiable.
        assert payload["last_error"] == _STALE_ERROR
        assert payload["error_detail"]["kind"] == "tool_failure"

    def test_unachieved_completion_with_sticky_error_still_carries_error(self, tmp_path) -> None:
        """Withholding ``error`` from a SUCCESS must not withhold it from a halt.

        This is the arm ``test_non_success_completion_keeps_error`` cannot
        reach: that one raises, so the exception handler builds the payload and
        ``_attach_failure_record`` never runs. A halt returns normally, so this
        is the only fixture that puts a non-``completed`` terminal status
        through the gate.
        """
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _halted_loop_run_with_error):
            orch.run(user_query="go", session_id=session_id, max_iters=1)

        payload = _completion_events(store, session_id)[0]["payload"]
        assert payload["done_reason"] == "halted_no_progress"
        assert payload["error"] == _STALE_ERROR
        assert payload["last_error"] == _STALE_ERROR

    def test_non_success_completion_keeps_error(self, tmp_path) -> None:
        """A non-"completed" done_reason must still carry the error keys."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _failing_loop_run):
            orch.run(user_query="go", session_id=session_id, max_iters=1)

        completion = _completion_events(store, session_id)[0]
        assert completion["payload"]["done_reason"] == "error"
        assert completion["payload"]["error"] == "LLM call exceeded 180s ceiling"
        assert completion["payload"]["last_error"] == "LLM call exceeded 180s ceiling"
