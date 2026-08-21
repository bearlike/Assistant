#!/usr/bin/env python3
"""Ask-the-user question tool — native human-in-the-loop clarification.

The Mewbo analog of the ask-user-question tools interactive coding harnesses
ship natively: the agent calls ``ask_user_question`` with 1-4 structured
questions, the run BLOCKS until a human answers, and the chosen answers
return directly as the tool result.

Blocking forever is the DEFAULT, not the only mode. A call may name its own
``timeout_seconds``; when that elapses the tool resolves ``timed_out`` — an
ordinary result the model reads and acts on, never an error — and the run
carries on. There is deliberately no default-answer concept and no
``on_timeout`` policy field: the two moves after an unanswered question
(proceed on the task's own terms, or stop and wait) are both already
available to the model, and what it lacked was an outcome it could read.

**A question is answerable until it is ANSWERED.** A timeout, a supersede,
even a process restart do not close it — only an answer does, and only a
terminated session refuses one. Because the ``user_question`` event is
durable, a host can recover a question's arguments from the transcript long
after the waiter departed and deliver the late answer as a new turn; that is
what makes "the user was away" a recoverable situation rather than a lost
decision. See ``apps/mewbo_api/src/mewbo_api/ask_user.py`` for the concrete
late-answer routing.

The split mirrors ``client_tools.py`` exactly: core owns the ``SessionTool``
(:class:`AskUserQuestionTool`), the wire/validation contract (the Pydantic
models below), and the down-only DI seam (:class:`QuestionDispatcher`); each
host registers its concrete transport half at startup (the api's
SSE-event + HTTP-answer dispatcher, the CLI's blocking-modal dispatcher).
Core never imports up.

The tool is CONDITIONALLY BOUND, never registered as a plugin factory: a run
gets it only when the querying client advertised the :data:`ASK_USER_CAPABILITY`
capability (``X-Mewbo-Capabilities`` → ``context.client_capabilities``),
via the api's ``_derive_tool_grants`` → ``extra_session_tools`` seam — the
same advertise-and-answer-at-one-seam pattern device tools use. Headless
drives (triggers, wiki, search, channels) never advertise it, so the tool
simply does not exist for them and nothing ever blocks waiting for a user
who is not there. ``extra_session_tools`` are not forwarded to spawned
children, so the tool is structurally ROOT-ONLY — a sub-agent reports its
open questions through its result instead of interrogating the user
mid-fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mewbo_core.capabilities import ASK_USER_CAPABILITY
from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.tooling.container_args import JsonContainerArguments

logging = get_logger(name="core.ask_user")

ASK_USER_QUESTION_TOOL_ID = "ask_user_question"

# ``ASK_USER_CAPABILITY`` is re-exported from its real home in
# ``mewbo_core.capabilities``, which owns the first-party capability registry and
# the ``X-Mewbo-Capabilities`` wire seam. It stays importable from here because
# existing call sites reach for it at this path; new code imports it from
# ``mewbo_core.capabilities``.

# Transcript event kinds. ``user_question`` announces a pending question
# (rides the session SSE stream + backlog replay); ``user_question_answered``
# records its resolution so every surface — not just the one that answered —
# can settle its card. Both are intentionally ephemeral transcript events
# (like ``device_tool_call``): compaction may drop them freely because the
# tool result carries the durable outcome into the message history.
USER_QUESTION_EVENT = "user_question"
USER_QUESTION_ANSWERED_EVENT = "user_question_answered"

QuestionOutcome = Literal["answered", "declined", "interrupted", "cancelled", "timed_out"]

# Ceiling on a model-supplied ``timeout_seconds``. A day is far past any
# plausible "wait for a human" and still finite, so a fat-fingered value can
# never park a run for a week; omitting the field entirely remains the way to
# wait indefinitely, which is the documented default.
MAX_QUESTION_TIMEOUT_S = 86_400

# Headroom between the dispatcher's own deadline and the loop's outer
# execution ceiling. The dispatcher must be the thing that expires, so that
# expiry renders as the ``timed_out`` RESULT the model can act on; if the
# loop's ``asyncio.wait_for`` fired first the step would die as a failed tool
# call instead, which is the exact defect this contract exists to remove.
QUESTION_TIMEOUT_MARGIN_S = 30.0

# Cap on the free-text notes a client may return alongside the selections.
MAX_QUESTION_NOTES_CHARS = 4_000


class QuestionOption(BaseModel):
    """One selectable option: a short label plus an optional description."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Concise option text the user picks (1-5 words).")
    description: str | None = Field(
        default=None,
        description="Optional one-line explanation of what choosing this means.",
    )

    @field_validator("label")
    @classmethod
    def _label_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("option label must be non-empty")
        return value.strip()


class UserQuestion(BaseModel):
    """One question: a header chip, the question text, and its answer shape.

    ``options`` must be empty (a free-text question) or hold 2-4 distinct
    choices — a single option is a statement, not a question. Regardless of
    options, a free-text answer is ALWAYS accepted (the ever-present
    "Other"), so ``multi_select`` only widens index selection and demands
    options to select from.
    """

    model_config = ConfigDict(extra="forbid")

    header: str = Field(description="Very short topic chip (max 48 chars), e.g. 'Auth method'.")
    question: str = Field(description="The complete question to ask the user.")
    options: list[QuestionOption] = Field(
        default_factory=list,
        description="Either empty (free-text question) or 2-4 distinct choices.",
    )
    multi_select: bool = Field(
        default=False,
        description="Allow selecting multiple options (requires options).",
    )

    @field_validator("header")
    @classmethod
    def _header_fits(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("header must be non-empty")
        if len(stripped) > 48:
            raise ValueError("header must be at most 48 characters")
        return stripped

    @field_validator("question")
    @classmethod
    def _question_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must be non-empty")
        return value

    @model_validator(mode="after")
    def _coherent_shape(self) -> UserQuestion:
        if len(self.options) == 1 or len(self.options) > 4:
            raise ValueError("options must be empty or hold 2-4 choices")
        if self.multi_select and not self.options:
            raise ValueError("multi_select requires options")
        return self

    @property
    def kind(self) -> Literal["free_text", "single_select", "multi_select"]:
        """Answer shape, derived from structure — never stored, never drifts."""
        if not self.options:
            return "free_text"
        return "multi_select" if self.multi_select else "single_select"

    def resolve_answer(self, item: QuestionAnswerItem) -> str:
        """Validate *item* against this question and render the chosen answer.

        Free text is always acceptable; indexes must exist, be in bounds and
        respect the selection arity. Raises ``ValueError`` with a message the
        answering client can show verbatim.
        """
        if item.text is not None:
            return item.text
        indexes = item.selected_indexes or ()
        if not self.options:
            raise ValueError(f"question '{self.header}' takes a free-text answer, not indexes")
        if any(i >= len(self.options) for i in indexes):
            raise ValueError(f"question '{self.header}' has {len(self.options)} options")
        if not self.multi_select and len(indexes) != 1:
            raise ValueError(f"question '{self.header}' takes exactly one selection")
        return ", ".join(self.options[i].label for i in indexes)


class QuestionAnswerItem(BaseModel):
    """One answer: selected option indexes XOR free text — never both.

    The index form is unambiguous on the wire (labels can repeat across
    surfaces/locales); the free-text form is the ever-present "Other".
    """

    model_config = ConfigDict(extra="forbid")

    selected_indexes: list[int] | None = Field(
        default=None, description="0-based indexes into the question's options."
    )
    text: str | None = Field(default=None, description="Free-text answer ('Other').")

    @model_validator(mode="after")
    def _exactly_one(self) -> QuestionAnswerItem:
        has_indexes = bool(self.selected_indexes)
        has_text = self.text is not None and bool(self.text.strip())
        if has_indexes == has_text:
            raise ValueError("provide selected_indexes XOR non-empty text")
        if has_indexes:
            indexes = self.selected_indexes or []
            if any(i < 0 for i in indexes):
                raise ValueError("selected_indexes must be non-negative")
            if len(set(indexes)) != len(indexes):
                raise ValueError("selected_indexes must be unique")
        return self


class AskUserQuestionArgs(BaseModel):
    """The tool's argument contract: 1-4 questions asked as one card group.

    Two optional knobs sit beside the questions, both absent by default so an
    ordinary call behaves exactly as it always has: ``timeout_seconds`` bounds
    the wait, and ``notes_placeholder`` asks clients for a free-text box.
    """

    model_config = ConfigDict(extra="forbid")

    questions: list[UserQuestion] = Field(min_length=1, max_length=4)
    timeout_seconds: int | None = Field(
        default=None,
        description="Seconds to wait before resolving 'timed_out'; omit to wait forever.",
    )
    notes_placeholder: str | None = Field(
        default=None,
        description="Placeholder for an optional free-text notes box shown with the group.",
    )

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_sane(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("timeout_seconds must be positive (omit it to wait forever)")
        if value > MAX_QUESTION_TIMEOUT_S:
            raise ValueError(f"timeout_seconds must be at most {MAX_QUESTION_TIMEOUT_S}")
        return value

    @field_validator("notes_placeholder")
    @classmethod
    def _placeholder_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("notes_placeholder must be non-empty when provided")
        if len(stripped) > 120:
            raise ValueError("notes_placeholder must be at most 120 characters")
        return stripped

    def execution_ceiling(self) -> float | None:
        """The loop's outer ``wait_for`` ceiling for this call, or ``None``.

        ``None`` means no ceiling: the documented block-until-answered
        contract, whose escape hatches are the run's own steering signals
        rather than a clock. A bounded call sits ABOVE its own deadline by
        :data:`QUESTION_TIMEOUT_MARGIN_S` so the dispatcher expires first and
        the model receives a readable ``timed_out`` result.
        """
        if self.timeout_seconds is None:
            return None
        return float(self.timeout_seconds) + QUESTION_TIMEOUT_MARGIN_S

    def render_answers(
        self, items: list[QuestionAnswerItem], notes: str | None = None
    ) -> str:
        """Validate *items* pairwise against the questions; render for the LLM.

        Raises ``ValueError`` on any mismatch (count or per-question shape) —
        the api's answer route surfaces that message as a 422 so a client
        never half-answers silently. *notes* is the optional free-text the
        user typed alongside their selections and is never validated against a
        question: it is additional prose, not an answer to any one of them.
        """
        if len(items) != len(self.questions):
            raise ValueError(
                f"expected {len(self.questions)} answer(s), got {len(items)}"
            )
        lines = [
            f"{question.header}: {question.resolve_answer(item)}"
            for question, item in zip(self.questions, items)
        ]
        trimmed = (notes or "").strip()
        if trimmed:
            lines.append(f"Notes: {trimmed[:MAX_QUESTION_NOTES_CHARS]}")
        return "\n".join(lines)


@dataclass(frozen=True)
class QuestionDispatchResult:
    """How a dispatched question group resolved (in-process value, not wire)."""

    outcome: QuestionOutcome
    answers: tuple[QuestionAnswerItem, ...] = field(default=())
    answered_via: str | None = field(default=None)
    notes: str | None = field(default=None)

    def render(self, args: AskUserQuestionArgs) -> str:
        """The LLM-facing tool result for this resolution."""
        if self.outcome == "answered":
            return "The user answered:\n" + args.render_answers(
                list(self.answers), self.notes
            )
        if self.outcome == "declined":
            return (
                "The user did not answer the question(s) — they sent a new "
                "message instead; it arrives as the next user message. "
                "Address that message."
            )
        if self.outcome == "timed_out":
            # Both continuations are named because the right one depends on the
            # task, which the model holds and this seam does not. The last
            # sentence is load-bearing: a late answer really does arrive as a
            # new user message, so waiting is a genuine option rather than a
            # dead end, and proceeding is not a silent gamble.
            waited = args.timeout_seconds
            return (
                f"No answer arrived within the {waited}s this call allowed, so "
                "the run continues without one. The question stays open — if "
                "the user answers later it arrives as a new user message. "
                "Judge from the task whether to proceed on the most reasonable "
                "assumption, stating which assumption you made, or to stop and "
                "report that you are waiting on this answer. Do not ask the "
                "same question again."
            )
        if self.outcome == "interrupted":
            return "The user interrupted the run without answering the question(s)."
        return "The session was cancelled before the user answered."


class QuestionDispatcherImpl(Protocol):
    """The concrete dispatcher a host registers (transport-bound)."""

    async def dispatch(
        self, session_id: str, args: AskUserQuestionArgs
    ) -> QuestionDispatchResult:
        """Deliver the questions to the user and block until resolution."""
        ...


class QuestionDispatcher:
    """Process-wide injectable dispatcher for ask-user questions.

    Down-only DI push seam mirroring ``client_tools.DeviceToolDispatcher``: a
    host registers its concrete implementation at startup; core never imports
    up to find it. No dispatcher registered → ``dispatch`` returns ``None``
    and :class:`AskUserQuestionTool` degrades to a structured "unavailable"
    error instead of crashing.
    """

    _impl: ClassVar[QuestionDispatcherImpl | None] = None

    @classmethod
    def register(cls, impl: QuestionDispatcherImpl | None) -> None:
        """Install the concrete dispatcher (called by the host at startup)."""
        cls._impl = impl

    @classmethod
    def reset(cls) -> None:
        """Clear the registered dispatcher (test isolation)."""
        cls._impl = None

    @classmethod
    def available(cls) -> bool:
        """True when a concrete dispatcher is registered."""
        return cls._impl is not None

    @classmethod
    async def dispatch(
        cls, session_id: str, args: AskUserQuestionArgs
    ) -> QuestionDispatchResult | None:
        """Dispatch via the registered impl; ``None`` when none is wired."""
        if cls._impl is None:
            return None
        return await cls._impl.dispatch(session_id, args)


def _error_envelope(code: str, message: str) -> MockSpeaker:
    """Structured error envelope for the loop's failed-step reclassification.

    The exact shape ``tool_use_loop._session_tool_error_envelope`` recognises
    (the ``str({"error": {...}})`` repr) — the step records ``success=False``
    while the model still receives the envelope text.
    """
    return MockSpeaker(content=str({"error": {"code": code, "message": message}}))


ASK_USER_QUESTION_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": ASK_USER_QUESTION_TOOL_ID,
        "description": (
            "Ask the user 1-4 structured questions and wait for their answer. "
            "Use ONLY when blocked on a decision that is genuinely the user's "
            "to make and that you cannot resolve from the request, the "
            "codebase, or sensible defaults — never for trivial choices with "
            "a conventional answer. By default the run pauses indefinitely, so "
            "batch related questions into one call. Set `timeout_seconds` when "
            "the task can proceed without an answer: on expiry you get a "
            "'timed out' result to act on rather than an error, the question "
            "stays open, and a later answer reaches you as a new user message. "
            "Offer 2-4 distinct, mutually exclusive options per question when "
            "choices are enumerable; the user can always answer in free text "
            "instead, and may also ignore the question and send a new message "
            "— address that message when they do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "Questions presented together as one group.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "header": {
                                "type": "string",
                                "description": (
                                    "Very short topic chip (max 48 chars), "
                                    "e.g. 'Auth method' or 'Scope'."
                                ),
                            },
                            "question": {
                                "type": "string",
                                "description": "The complete question, ending with '?'.",
                            },
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 4,
                                "description": (
                                    "2-4 distinct choices; omit entirely for a "
                                    "free-text question. Put your recommended "
                                    "option first, suffixed '(Recommended)'."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Concise choice text (1-5 words).",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": (
                                                "One-line explanation of the "
                                                "trade-off or consequence."
                                            ),
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi_select": {
                                "type": "boolean",
                                "description": (
                                    "Allow selecting several options (default "
                                    "false; requires options)."
                                ),
                            },
                        },
                        "required": ["header", "question"],
                    },
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_QUESTION_TIMEOUT_S,
                    "description": (
                        "Optional. Seconds to wait before the call resolves as "
                        "timed out so you can continue. OMIT to wait "
                        "indefinitely — the right choice when proceeding "
                        "without the answer would be wrong or destructive. Set "
                        "it when the work has other useful moves available."
                    ),
                },
                "notes_placeholder": {
                    "type": "string",
                    "description": (
                        "Optional. Hint text for a free-text notes box shown "
                        "alongside the questions (e.g. 'Anything else I should "
                        "know about the deploy?'). Supply it when extra context "
                        "would help; the user may leave it blank."
                    ),
                },
            },
            "required": ["questions"],
        },
    },
}


class AskUserQuestionTool:
    """A ``SessionTool`` that blocks the run on a human answer.

    Non-terminal by design: the answer is an ordinary tool result and the
    agent keeps working. ``terminal_reason`` is defined explicitly because
    ``SessionTool`` is a structural Protocol — its default bodies are NOT
    inherited by standalone implementers (the ``submit_widget`` trap; see
    packages/mewbo_core/CLAUDE.md → "Built-in plugins").
    """

    tool_id: str = ASK_USER_QUESTION_TOOL_ID
    # Clarifying questions are MOST valuable while planning, so the tool is
    # bound in both modes (cf. ExitPlanModeTool's plan-only override).
    modes: frozenset[str] = frozenset({"plan", "act"})

    def __init__(self, session_id: str) -> None:
        """Bind the session id; the schema is a class-level constant."""
        self._session_id = session_id
        self.schema: dict[str, object] = ASK_USER_QUESTION_SCHEMA

    def should_terminate_run(self) -> bool:
        """Never terminates — the answer is an ordinary tool result."""
        return False

    def execution_timeout(self, tool_input: object) -> float | None:
        """Declare this call's outer execution ceiling for the loop.

        The loop reads this defensively (``getattr`` + ``callable``), so the
        hook is a convention rather than a Protocol member — the same shape
        ``poll_class`` and ``result_headline`` use. Unparseable input yields
        the unbounded contract rather than a raise: this runs BEFORE
        :meth:`handle` validates, and a bad ceiling must never be the thing
        that fails a call whose own validator will report the real problem.
        """
        if not isinstance(tool_input, dict):
            return None
        try:
            raw = JsonContainerArguments.decode(AskUserQuestionArgs, tool_input).arguments
            return AskUserQuestionArgs.model_validate(raw).execution_ceiling()
        except ValidationError:
            return None

    def terminal_reason(self) -> str:
        """Unused (never terminates); explicit for Protocol-default parity."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate the questions, dispatch to the host, block, render."""
        tool_input = (
            action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        )
        # A `questions` list emitted as a JSON string is decoded through the one
        # shared law before validation (see tooling/container_args.py).
        tool_input = JsonContainerArguments.decode(
            AskUserQuestionArgs, tool_input
        ).arguments
        try:
            args = AskUserQuestionArgs.model_validate(tool_input)
        except ValidationError as exc:
            return _error_envelope("validation", str(exc))
        result = await QuestionDispatcher.dispatch(self._session_id, args)
        if result is None:
            return _error_envelope(
                "ask_user_unavailable",
                "No question dispatcher is registered in this host; the user "
                "cannot be asked. Proceed with your best judgment.",
            )
        return MockSpeaker(content=result.render(args))


__all__ = [
    "ASK_USER_CAPABILITY",
    "ASK_USER_QUESTION_SCHEMA",
    "ASK_USER_QUESTION_TOOL_ID",
    "MAX_QUESTION_NOTES_CHARS",
    "MAX_QUESTION_TIMEOUT_S",
    "QUESTION_TIMEOUT_MARGIN_S",
    "USER_QUESTION_ANSWERED_EVENT",
    "USER_QUESTION_EVENT",
    "AskUserQuestionArgs",
    "AskUserQuestionTool",
    "QuestionAnswerItem",
    "QuestionDispatchResult",
    "QuestionDispatcher",
    "QuestionDispatcherImpl",
    "QuestionOption",
    "QuestionOutcome",
    "UserQuestion",
]
