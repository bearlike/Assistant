"""Ask-user questions — api-side dispatch glue.

Sibling of ``device_tools.py``: ``mewbo_core.tooling.ask_user`` owns the
``SessionTool`` (``AskUserQuestionTool``) and the down-only DI seam
(``QuestionDispatcher``); this module owns the CONCRETE dispatcher — the
transport half that appends session events and blocks on a client's
HTTP-delivered answer. The cross-thread wait is a ``threading.Event`` —
NEVER an ``asyncio.Future`` — for the same reason ``device_tools.py``
documents: the session loop and the Flask answer route live on different
threads with different event loops.

Three deliberate differences from the device-tool bridge:

- **Unbounded by default.** A question waits until a human resolves it unless
  the call named its own ``timeout_seconds``. Instead of a deadline, the wait
  loop watches the run's own steering signals via
  ``runtime.active_run_handle``: a queued steer message supersedes the
  question (outcome ``declined`` — the user typed instead of tapping; the
  message rides the normal steer queue), an interrupt reads ``interrupted``,
  a cancel reads ``cancelled``. That keeps every supersede rule in THIS one
  wait loop — the ``/message`` and ``/interrupt`` routes are untouched.
- **No presence short-circuit at all.** The console holds the transcript
  SSE open now, but subscriber-presence would STILL false-negative: the
  stream is closed as soon as a session stops running and the client
  re-subscribes a moment later, so a viewer sitting in front of the session
  has no subscription at all during each reconnect gap. Gating delivery on a
  live subscriber would drop exactly the questions asked in those windows.
  The ``ask_user`` capability advertisement is the delivery gate instead —
  a session that never advertised it never binds the tool at all. The device
  bridge does ask (``SessionEventBus.has_executor``) because it has a wait
  budget to protect, and it pays for the same reconnect gap with a grace
  window; a question that waits for a human indefinitely has nothing to
  protect and needs no window.
- **No expiry-based reaping.** The dispatcher coroutine owns the entry's
  whole lifecycle (create → wait → take/withdraw in ``finally``), so the
  registry needs no deadline bookkeeping: an entry cannot outlive its
  waiter while the process is alive.

**The waiter is a rendezvous; the transcript is the record.** Losing the
waiter — to a timeout, a supersede, or a process restart — does NOT close the
question, because the ``user_question`` event durably carries both the
questions and the ``call_token``. :class:`QuestionAnswerRouter` is the ONE
place that decides where an incoming answer goes: into a live waiter (the
blocked tool call resolves with it) or, when there is none, into the session
as a new user turn through the host's delivery seam. Only a real prior answer
(409) or a terminated session (410, guarded at the route) refuses one.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from mewbo_core.common import get_logger
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.tooling.ask_user import (
    USER_QUESTION_ANSWERED_EVENT,
    USER_QUESTION_EVENT,
    AskUserQuestionArgs,
    QuestionAnswerItem,
    QuestionDispatchResult,
    QuestionOutcome,
)
from pydantic import ValidationError

logging = get_logger(name="api.ask_user")

_POLL_INTERVAL_S = 0.2

AnswerOutcome = Literal["ok", "not_found", "bad_token", "conflict", "invalid"]

# Where an accepted answer landed. ``run`` = a live waiter took it and the
# blocked tool call resolves with it; ``message`` = no waiter was left, so it
# arrived as a new user turn.
AnswerDelivery = Literal["run", "message"]


@dataclass(frozen=True)
class TurnDelivery:
    """How a user turn reached a session — the host delivery seam's result.

    The contract :class:`QuestionAnswerRouter` needs from its injected
    ``deliver`` collaborator, declared here (with the router) rather than
    beside the seam's implementation: the api's implementation lives in
    ``backend.py``, which already imports this module, so the reverse
    direction would cycle.

    ``steered`` — an active run took the text as a steering message.
    ``started`` — the session was idle and a fresh run carries the text
    (``run_id`` is set). ``refused`` — neither path was available, which on
    this seam means a run started between the two attempts.
    """

    outcome: Literal["steered", "started", "refused"]
    run_id: str | None = field(default=None)


# The host seam that puts a user turn into a session (``/message``'s body).
DeliverTurn = Callable[[str, str], TurnDelivery]


@dataclass(frozen=True)
class AnswerRouting:
    """Where an answer went, in terms the HTTP layer maps straight to a status."""

    outcome: AnswerOutcome
    detail: str
    delivery: AnswerDelivery | None = field(default=None)


@dataclass
class _PendingQuestion:
    """One in-flight question group awaiting a human answer."""

    call_token: str
    event: threading.Event
    args: AskUserQuestionArgs
    answers: tuple[QuestionAnswerItem, ...] = field(default=())
    answered_via: str | None = field(default=None)
    notes: str | None = field(default=None)
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
        notes: str | None = None,
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
                entry.args.render_answers(items, notes)
            except ValueError as exc:
                return "invalid", str(exc)
            entry.answers = tuple(items)
            entry.answered_via = answered_via
            entry.notes = notes
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
    """Concrete ``QuestionDispatcherImpl`` — event delivery + bounded-or-not wait.

    Registered into the core seam (``mewbo_core.tooling.ask_user.QuestionDispatcher``)
    at api startup, right beside ``ApiDeviceToolDispatcher``. DI'd with the
    session runtime (event append through the same choke point every event
    uses, plus read-only steering-signal access), the pending registry, and
    the clock its deadline is measured on — an injected clock is what lets a
    test drive expiry with no sleeping.
    """

    def __init__(
        self,
        *,
        runtime: SessionRuntime,
        pending: QuestionPendingCalls | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the session runtime, pending-question registry and clock."""
        self._runtime = runtime
        self._pending = pending if pending is not None else get_pending_questions()
        self._monotonic = monotonic

    async def dispatch(
        self, session_id: str, args: AskUserQuestionArgs
    ) -> QuestionDispatchResult:
        """Announce the questions, block until the wait ends.

        Emits one ``user_question`` event carrying a fresh single-use
        ``call_token`` (same threat model as ``device_tool_call`` — see
        ``types.UserQuestionPayload``), then polls until the answer route
        sets the event, a run steering signal supersedes the question, or the
        call's own ``timeout_seconds`` elapses. Always emits the matching
        ``user_question_answered`` event so every surface settles its card,
        whatever the outcome — and an outcome other than ``answered`` records
        that the RUN stopped waiting, never that the question closed: the
        withdrawn entry leaves the durable event as the answerable record.
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
                    "timeout_seconds": args.timeout_seconds,
                    "notes_placeholder": args.notes_placeholder,
                },
            },
        )

        deadline = (
            None
            if args.timeout_seconds is None
            else self._monotonic() + float(args.timeout_seconds)
        )
        superseded: QuestionOutcome | None = None
        try:
            while not event.is_set():
                signal = self._steering_outcome(session_id)
                if signal is None and deadline is not None and self._monotonic() >= deadline:
                    signal = "timed_out"
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
                    "notes": result.notes if result.outcome == "answered" else None,
                    "delivery": "run" if result.outcome == "answered" else None,
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
            notes=entry.notes,
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


class QuestionAnswerRouter:
    """The ONE decision of where an incoming question answer goes.

    A question is answerable until it is ANSWERED, so an answer has two
    possible landing places and exactly one of them is chosen here:

    - **A live waiter** — the registry still holds the entry, so the blocked
      ``ask_user_question`` call resolves with the answer and the model reads
      it as that call's result (``delivery: "run"``).
    - **No waiter** — the wait ended (timeout, supersede, process restart) or
      the run finished. The questions and the ``call_token`` are recovered from
      the DURABLE ``user_question`` event and the answer is delivered as a new
      user turn through the host's ``deliver`` seam, so it reaches the model as
      an ordinary message (``delivery: "message"``). Whether that seam steers a
      live run or starts a fresh one is the seam's business, not this class's.

    Collaborators are injected as fields: the pending registry, the session
    runtime (durable event reads + the answered-event append), and the host
    delivery seam. Only a real prior answer refuses a late one — the outcomes
    that merely record the RUN stopping (``timed_out``/``declined``/
    ``interrupted``/``cancelled``) leave the question open by design.
    """

    # Frames the rendered answer so the model can act on it. Short on purpose:
    # the question itself is already in its context, so restating it would only
    # spend tokens re-teaching the model something it can read.
    _LATE_ANSWER_PREAMBLE = (
        "This answers the question you asked earlier, which had no answer at the time."
    )

    def __init__(
        self,
        *,
        runtime: SessionRuntime,
        pending: QuestionPendingCalls,
        deliver: DeliverTurn,
    ) -> None:
        """Bind the runtime, the pending registry and the host delivery seam."""
        self._runtime = runtime
        self._pending = pending
        self._deliver = deliver

    def route(
        self,
        session_id: str,
        call_id: str,
        token: str,
        items: list[QuestionAnswerItem],
        *,
        answered_via: str | None,
        notes: str | None = None,
    ) -> AnswerRouting:
        """Deliver *items* to the live waiter, or as a new turn when none is left."""
        outcome, detail = self._pending.resolve(
            session_id, call_id, token, items, answered_via=answered_via, notes=notes
        )
        if outcome != "not_found":
            return AnswerRouting(
                outcome, detail, delivery="run" if outcome == "ok" else None
            )
        return self._route_late(
            session_id, call_id, token, items, answered_via=answered_via, notes=notes
        )

    def _route_late(
        self,
        session_id: str,
        call_id: str,
        token: str,
        items: list[QuestionAnswerItem],
        *,
        answered_via: str | None,
        notes: str | None,
    ) -> AnswerRouting:
        """Recover the question from the transcript and deliver as a user turn."""
        asked, already_answered = self._recover_question(session_id, call_id)
        if asked is None:
            return AnswerRouting("not_found", "No question with that call_id.")
        # Token BEFORE answered-state, matching the live registry's order — the
        # same POST must not read a different status depending on whether a
        # waiter happens to still be alive, and an unauthorized caller learns
        # nothing about whether the question was answered.
        if not hmac.compare_digest(str(asked.get("call_token") or ""), token):
            return AnswerRouting("bad_token", "call_token does not match the question.")
        if already_answered:
            return AnswerRouting(
                "conflict", "An answer was already delivered for this question."
            )
        args = self._args_from_event(asked)
        if args is None:
            return AnswerRouting(
                "invalid", "The stored question cannot be read; it is not answerable."
            )
        try:
            rendered = args.render_answers(items, notes)
        except ValueError as exc:
            return AnswerRouting("invalid", str(exc))
        delivered = self._deliver(session_id, f"{self._LATE_ANSWER_PREAMBLE}\n\n{rendered}")
        if delivered.outcome == "refused":
            return AnswerRouting(
                "conflict", "The session could not accept the answer; try again."
            )
        self._record_answered(
            session_id, call_id, items, answered_via=answered_via, notes=notes
        )
        return AnswerRouting("ok", "answered", delivery="message")

    def _recover_question(
        self, session_id: str, call_id: str
    ) -> tuple[dict[str, object] | None, bool]:
        """One transcript pass: the question's payload, and is it answered yet.

        Both facts come off the same walk because they are read together on
        every late answer, and a second pass would double the cost of the read
        on a long transcript.
        """
        asked: dict[str, object] | None = None
        already_answered = False
        for event in self._runtime.load_events(session_id):
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("call_id") != call_id:
                continue
            kind = event.get("type")
            if kind == USER_QUESTION_EVENT:
                asked = payload
            elif kind == USER_QUESTION_ANSWERED_EVENT and payload.get("outcome") == "answered":
                already_answered = True
        return asked, already_answered

    def _args_from_event(self, asked: dict[str, object]) -> AskUserQuestionArgs | None:
        """Rebuild the tool's own argument model from the durable payload.

        Reusing the model is what keeps late-answer validation identical to
        the live path's — the alternative is a second implementation of
        ``render_answers``' rules that drifts the moment a question kind gains
        a field. ``None`` when the stored payload no longer validates (a
        server-side defect, not a client one — hence the warning).
        """
        try:
            return AskUserQuestionArgs.model_validate(
                {
                    "questions": asked.get("questions"),
                    "timeout_seconds": asked.get("timeout_seconds"),
                    "notes_placeholder": asked.get("notes_placeholder"),
                }
            )
        except ValidationError as exc:
            logging.warning("Stored user_question payload does not validate: {}", exc)
            return None

    def _record_answered(
        self,
        session_id: str,
        call_id: str,
        items: list[QuestionAnswerItem],
        *,
        answered_via: str | None,
        notes: str | None,
    ) -> None:
        """Append the resolution record for a late answer.

        Same payload shape ``ApiQuestionDispatcher.dispatch`` emits (keep the
        two in lockstep) — every surface folds this onto its pending card, so a
        late answer must settle the card everywhere, not only where it was
        typed.
        """
        self._runtime.append_event(
            session_id,
            {
                "type": USER_QUESTION_ANSWERED_EVENT,
                "payload": {
                    "call_id": call_id,
                    "outcome": "answered",
                    "answered_via": answered_via,
                    "answers": [a.model_dump(mode="json") for a in items],
                    "notes": notes,
                    "delivery": "message",
                },
            },
        )


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
    "AnswerDelivery",
    "AnswerOutcome",
    "AnswerRouting",
    "ApiQuestionDispatcher",
    "DeliverTurn",
    "QuestionAnswerRouter",
    "QuestionPendingCalls",
    "TurnDelivery",
    "get_pending_questions",
    "reset_pending_questions_for_tests",
]
