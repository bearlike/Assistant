"""Ask-user questions — api-side dispatch glue.

Sibling of ``device_tools.py``: ``mewbo_core.ask_user`` owns the
``SessionTool`` (``AskUserQuestionTool``) and the down-only DI seam
(``QuestionDispatcher``); this module owns the CONCRETE dispatcher — the
transport half that appends session events and blocks on a client's
HTTP-delivered answer. The cross-thread wait is a ``threading.Event`` —
NEVER an ``asyncio.Future`` — for the same reason ``device_tools.py``
documents: the session loop and the Flask answer route live on different
threads with different event loops.

Three deliberate differences from the device-tool bridge:

- **No timeout.** A question waits until a human resolves it (user-approved
  design). Instead of a deadline, the wait loop watches the run's
  own steering signals via ``runtime.active_run_handle``: a queued steer
  message supersedes the question (outcome ``declined`` — the user typed
  instead of tapping; the message rides the normal steer queue), an
  interrupt reads ``interrupted``, a cancel reads ``cancelled``. That keeps
  every supersede rule in THIS one wait loop — the ``/message`` and
  ``/interrupt`` routes are untouched.
- **No ``has_subscribers`` short-circuit.** The console POLLS ``/events``
  rather than holding SSE open, so subscriber-presence would false-negative;
  the ``ask_user`` capability advertisement is the delivery gate instead —
  a session that never advertised it never binds the tool at all.
- **No expiry-based reaping.** The dispatcher coroutine owns the entry's
  whole lifecycle (create → wait → take/withdraw in ``finally``), so the
  registry needs no deadline bookkeeping: an entry cannot outlive its
  waiter while the process is alive, and a process restart drops the run
  and its pending question together (accepted, same blast radius as any
  in-flight run).
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

from mewbo_core.ask_user import (
    USER_QUESTION_ANSWERED_EVENT,
    USER_QUESTION_EVENT,
    AskUserQuestionArgs,
    QuestionAnswerItem,
    QuestionDispatchResult,
    QuestionOutcome,
)
from mewbo_core.common import get_logger
from mewbo_core.session_runtime import SessionRuntime

logging = get_logger(name="api.ask_user")

_POLL_INTERVAL_S = 0.2

AnswerOutcome = Literal["ok", "not_found", "bad_token", "conflict", "invalid"]


@dataclass
class _PendingQuestion:
    """One in-flight question group awaiting a human answer."""

    call_token: str
    event: threading.Event
    args: AskUserQuestionArgs
    answers: tuple[QuestionAnswerItem, ...] = field(default=())
    answered_via: str | None = field(default=None)
    consumed: bool = field(default=False)


class QuestionPendingCalls:
    """Registry of in-flight ask-user questions awaiting a POSTed answer.

    One instance per process (see :func:`get_pending_questions`), guarded by
    a single lock. Answered-once: the first surface to :meth:`resolve` wins;
    a duplicate POST returns ``"conflict"`` (409) while the entry lingers,
    and ``"not_found"`` (404) once the dispatcher has taken it.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingQuestion] = {}

    def create(
        self, session_id: str, call_id: str, call_token: str, args: AskUserQuestionArgs
    ) -> threading.Event:
        """Register a new pending question; return the event the waiter polls."""
        event = threading.Event()
        with self._lock:
            self._pending[(session_id, call_id)] = _PendingQuestion(
                call_token=call_token, event=event, args=args
            )
        return event

    def resolve(
        self,
        session_id: str,
        call_id: str,
        token: str,
        items: list[QuestionAnswerItem],
        *,
        answered_via: str | None,
    ) -> tuple[AnswerOutcome, str]:
        """Deliver validated answer items for a pending question group.

        Token compared via :func:`hmac.compare_digest` (bearer secret, not a
        lookup key). Semantic validation (count match, index bounds, arity)
        runs HERE against the stored question args — the shape the route
        already Pydantic-validated cannot know the questions — and a failure
        returns ``("invalid", message)`` for a 422 whose text the user can
        act on. First delivery wins; the waiter's event is set atomically.
        """
        with self._lock:
            entry = self._pending.get((session_id, call_id))
            if entry is None:
                return "not_found", "No pending question with that call_id."
            if not hmac.compare_digest(entry.call_token, token):
                return "bad_token", "call_token does not match the pending question."
            if entry.consumed:
                return "conflict", "An answer was already delivered for this question."
            try:
                entry.args.render_answers(items)
            except ValueError as exc:
                return "invalid", str(exc)
            entry.answers = tuple(items)
            entry.answered_via = answered_via
            entry.consumed = True
            entry.event.set()
            return "ok", "answered"

    def withdraw(self, session_id: str, call_id: str) -> bool:
        """Withdraw an unanswered entry (steer/interrupt/cancel superseded it).

        Returns ``False`` — leaving the entry intact — when an answer raced
        in first (``consumed``); the caller must then read that answer as the
        real resolution instead of discarding the user's input.
        """
        with self._lock:
            entry = self._pending.get((session_id, call_id))
            if entry is None:
                return True
            if entry.consumed:
                return False
            del self._pending[(session_id, call_id)]
            return True

    def take(self, session_id: str, call_id: str) -> _PendingQuestion | None:
        """Pop and return a resolved entry (the dispatcher's final read)."""
        with self._lock:
            entry = self._pending.pop((session_id, call_id), None)
            if entry is None or not entry.consumed:
                return None
            return entry


class ApiQuestionDispatcher:
    """Concrete ``QuestionDispatcherImpl`` — event delivery + unbounded wait.

    Registered into the core seam (``mewbo_core.ask_user.QuestionDispatcher``)
    at api startup, right beside ``ApiDeviceToolDispatcher``. DI'd with the
    session runtime (event append through the same choke point every event
    uses, plus read-only steering-signal access) and the pending registry.
    """

    def __init__(
        self, *, runtime: SessionRuntime, pending: QuestionPendingCalls | None = None
    ) -> None:
        """Bind the session runtime and pending-question registry."""
        self._runtime = runtime
        self._pending = pending if pending is not None else get_pending_questions()

    async def dispatch(
        self, session_id: str, args: AskUserQuestionArgs
    ) -> QuestionDispatchResult:
        """Announce the questions, block until a human resolves them.

        Emits one ``user_question`` event carrying a fresh single-use
        ``call_token`` (same threat model as ``device_tool_call`` — see
        ``types.UserQuestionPayload``), then polls until the answer route
        sets the event OR a run steering signal supersedes the question.
        Always emits the matching ``user_question_answered`` event so every
        surface settles its card, whatever the outcome.
        """
        call_id = uuid.uuid4().hex
        call_token = secrets.token_urlsafe(24)
        event = self._pending.create(session_id, call_id, call_token, args)

        self._runtime.append_event(
            session_id,
            {
                "type": USER_QUESTION_EVENT,
                "payload": {
                    "call_id": call_id,
                    "call_token": call_token,
                    "questions": [q.model_dump(mode="json") for q in args.questions],
                },
            },
        )

        superseded: QuestionOutcome | None = None
        try:
            while not event.is_set():
                signal = self._steering_outcome(session_id)
                if signal is not None:
                    if self._pending.withdraw(session_id, call_id):
                        superseded = signal
                    # withdraw() False → an answer raced the signal in; the
                    # user's input wins and the loop falls through to read it.
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            result = self._resolve_result(session_id, call_id, superseded)
        finally:
            # Defensive sweep: whatever path exits (including an unexpected
            # raise), the entry must not outlive its waiter.
            self._pending.withdraw(session_id, call_id)
            self._pending.take(session_id, call_id)

        self._runtime.append_event(
            session_id,
            {
                "type": USER_QUESTION_ANSWERED_EVENT,
                "payload": {
                    "call_id": call_id,
                    "outcome": result.outcome,
                    "answered_via": result.answered_via,
                    "answers": (
                        [a.model_dump(mode="json") for a in result.answers]
                        if result.outcome == "answered"
                        else None
                    ),
                },
            },
        )
        return result

    def _resolve_result(
        self, session_id: str, call_id: str, superseded: QuestionOutcome | None
    ) -> QuestionDispatchResult:
        """Fold the wake reason + registry state into the dispatch result."""
        if superseded is not None:
            return QuestionDispatchResult(outcome=superseded)
        entry = self._pending.take(session_id, call_id)
        if entry is None or not entry.answers:  # pragma: no cover - defensive
            logging.warning(
                "Pending question {} for session {} vanished before read",
                call_id,
                session_id,
            )
            return QuestionDispatchResult(outcome="interrupted")
        return QuestionDispatchResult(
            outcome="answered",
            answers=entry.answers,
            answered_via=entry.answered_via,
        )

    def _steering_outcome(self, session_id: str) -> QuestionOutcome | None:
        """Map the run's live steering signals to a superseding outcome.

        Read-only peeks at the ``RunHandle``: a set ``cancel_event`` or
        ``interrupt_step`` is left for the loop's own turn-top handling to
        consume; a non-empty ``message_queue`` is likewise left queued (the
        loop drains it into the next turn — the question result only tells
        the model the user answered by message instead). A handle-less run
        (sync in-process drive) simply has no steering to watch.
        """
        handle = self._runtime.active_run_handle(session_id)
        if handle is None:
            return None
        if handle.cancel_event.is_set():
            return "cancelled"
        if handle.interrupt_step is not None and handle.interrupt_step.is_set():
            return "interrupted"
        if handle.message_queue is not None and not handle.message_queue.empty():
            return "declined"
        return None


# ---------------------------------------------------------------------------
# Process-wide singleton (DI seam shared by the dispatcher + the answer route)
# ---------------------------------------------------------------------------

_PENDING_QUESTIONS: QuestionPendingCalls | None = None


def get_pending_questions() -> QuestionPendingCalls:
    """Return the process-wide pending-questions registry (created on first use).

    Mirrors ``device_tools.get_pending_calls``: a singleton+factory+
    ``reset_for_tests`` seam shared by the dispatcher (writer) and the
    ``POST .../questions/<call_id>/answer`` route (resolver).
    """
    global _PENDING_QUESTIONS
    if _PENDING_QUESTIONS is None:
        _PENDING_QUESTIONS = QuestionPendingCalls()
    return _PENDING_QUESTIONS


def reset_pending_questions_for_tests() -> QuestionPendingCalls:
    """Swap in a fresh registry for test isolation and return it."""
    global _PENDING_QUESTIONS
    _PENDING_QUESTIONS = QuestionPendingCalls()
    return _PENDING_QUESTIONS


__all__ = [
    "AnswerOutcome",
    "ApiQuestionDispatcher",
    "QuestionPendingCalls",
    "get_pending_questions",
    "reset_pending_questions_for_tests",
]
