#!/usr/bin/env python3
"""Ask-the-user question tool — native human-in-the-loop clarification.

The Mewbo analog of the ask-user-question tools interactive coding harnesses
ship natively: the agent calls ``ask_user_question`` with 1-4 structured
questions, the run BLOCKS until a human answers — no timeout and no
default-answer concept; an unanswered question waits until it is answered or
the run is steered/interrupted/cancelled — and the chosen answers return
directly as the tool result.

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

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger

logging = get_logger(name="core.ask_user")

ASK_USER_QUESTION_TOOL_ID = "ask_user_question"

# The client-advertised capability that opts a run into this tool. A plain
# string two sides agree on (there is deliberately no capability enum — see
# packages/mewbo_core/CLAUDE.md → "Custom system instructions", trap 2).
ASK_USER_CAPABILITY = "ask_user"

# Transcript event kinds. ``user_question`` announces a pending question
# (rides the session SSE stream + backlog replay); ``user_question_answered``
# records its resolution so every surface — not just the one that answered —
# can settle its card. Both are intentionally ephemeral transcript events
# (like ``device_tool_call``): compaction may drop them freely because the
# tool result carries the durable outcome into the message history.
USER_QUESTION_EVENT = "user_question"
USER_QUESTION_ANSWERED_EVENT = "user_question_answered"

QuestionOutcome = Literal["answered", "declined", "interrupted", "cancelled"]


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
    """The tool's argument contract: 1-4 questions asked as one card group."""

    model_config = ConfigDict(extra="forbid")

    questions: list[UserQuestion] = Field(min_length=1, max_length=4)

    def render_answers(self, items: list[QuestionAnswerItem]) -> str:
        """Validate *items* pairwise against the questions; render for the LLM.

        Raises ``ValueError`` on any mismatch (count or per-question shape) —
        the api's answer route surfaces that message as a 422 so a client
        never half-answers silently.
        """
        if len(items) != len(self.questions):
            raise ValueError(
                f"expected {len(self.questions)} answer(s), got {len(items)}"
            )
        lines = [
            f"{question.header}: {question.resolve_answer(item)}"
            for question, item in zip(self.questions, items)
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class QuestionDispatchResult:
    """How a dispatched question group resolved (in-process value, not wire)."""

    outcome: QuestionOutcome
    answers: tuple[QuestionAnswerItem, ...] = field(default=())
    answered_via: str | None = field(default=None)

    def render(self, args: AskUserQuestionArgs) -> str:
        """The LLM-facing tool result for this resolution."""
        if self.outcome == "answered":
            return "The user answered:\n" + args.render_answers(list(self.answers))
        if self.outcome == "declined":
            return (
                "The user did not answer the question(s) — they sent a new "
                "message instead; it arrives as the next user message. "
                "Address that message."
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
            "a conventional answer. The run pauses until the user responds "
            "(there is no timeout), so batch related questions into one call. "
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
                }
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

    def terminal_reason(self) -> str:
        """Unused (never terminates); explicit for Protocol-default parity."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate the questions, dispatch to the host, block, render."""
        tool_input = (
            action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        )
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
