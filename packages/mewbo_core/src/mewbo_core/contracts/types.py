#!/usr/bin/env python3
"""Shared type definitions for core components."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from typing_extensions import NotRequired

JsonValue = str | int | float | bool | None | list[object] | dict[str, object]
ToolInput = str | dict[str, object]


class PlanStepPayload(TypedDict):
    """Payload describing a single plan step."""

    title: str
    description: str


class ActionPlanPayload(TypedDict):
    """Payload describing an action plan."""

    steps: list[PlanStepPayload]


class ActionStepPayload(TypedDict):
    """Serialized tool call data sent to/from execution."""

    tool_id: str
    operation: str
    tool_input: ToolInput
    title: NotRequired[str]
    objective: NotRequired[str]
    execution_checklist: NotRequired[list[str]]
    expected_output: NotRequired[str]


class PermissionPayload(TypedDict):
    """Payload emitted for permission decisions."""

    tool_id: str
    operation: str
    tool_input: str
    decision: str


class ToolCallPayload(TypedDict):
    """Payload describing a tool invocation that is about to be dispatched.

    The initiation half of a tool call, emitted BEFORE the tool is awaited so a
    client can show the step while it runs rather than only once it finished.
    ``tool_call_id`` is the provider's call id and the only correlation key to the
    matching :class:`ToolResultPayload`; it is ``""`` when the provider supplied
    none, which means "not correlatable" and never "shares a key with the others".
    """

    tool_call_id: str
    tool_id: str
    operation: str
    tool_input: ToolInput
    agent_id: NotRequired[str]
    depth: NotRequired[int]
    model: NotRequired[str]


class ToolResultPayload(TypedDict):
    """Payload describing the outcome of a tool invocation."""

    tool_id: str
    operation: str
    tool_input: ToolInput
    result: str | None
    # Pairs this outcome with its :class:`ToolCallPayload`. Absent on every
    # transcript written before the initiation event existed, and ``""`` when the
    # provider supplied no id — both mean "not correlatable".
    tool_call_id: NotRequired[str]
    success: NotRequired[bool]
    summary: NotRequired[str]
    error: NotRequired[str]


class UserPayload(TypedDict):
    """Payload describing a user message."""

    text: str
    # Present only when the turn carried attachments — the same
    # AttachmentDescriptor dicts riding the sibling ``context`` event's
    # ``payload.attachments`` (see ``mewbo_core.session.context._iter_attachments``),
    # additively duplicated here so clients can render attachment cards
    # above the user turn without cross-referencing an adjacent event.
    attachments: NotRequired[list[dict[str, object]]]


class AssistantPayload(TypedDict):
    """Payload describing an assistant response."""

    text: str


class CompletionPayload(TypedDict):
    """Payload describing overall completion state."""

    done: bool
    done_reason: str | None
    task_result: str | None
    # Flat, bounded blurbs — Aura's error card and the CLI read these.
    # Written from ``RunError.brief()``, so never longer than 500
    # characters no matter what the provider stack emitted.
    error: NotRequired[str]
    last_error: NotRequired[str]
    # Additive structured failure record — a serialized ``RunError``
    # (``mewbo_core.contracts.run_error``), stored via ``model_dump(mode="json")`` so the
    # value is plain JSON types for Mongo and the wire. Present only alongside
    # ``error``; a client that only knows the flat keys is unaffected.
    error_detail: NotRequired[dict[str, Any]]
    # The last UNRECOVERED tool-envelope error code, and only when it is one a
    # user can act on: ``repo_access`` / ``network`` / ``forbidden`` /
    # ``quota_exceeded``. Projected from ``OrchestrationState.blocked_code``,
    # which the loop clears per tool_id as soon as that tool succeeds again.
    #
    # This is the sole input to the derived ``blocked`` status
    # (``session_runtime``): ``done_reason`` stays ``"completed"`` for a blocked
    # run at the loop layer, so without this key the fact that a run died
    # against a credential or a network path is unreachable from the record.
    # NotRequired, so every payload written before it existed still validates.
    blocked_code: NotRequired[str]


# The user-actionable tool-envelope codes that a run carries as ``blocked_code``
# and that derive the ``blocked`` status. Kept here beside the wire field so the
# loop that STAMPS the code and the runtime that HONORS it read one set — a new
# code added in only one place would stamp a completion the other never renders
# as blocked, silently dropping the very signal.
BLOCKED_CODES: frozenset[str] = frozenset(
    {"repo_access", "network", "forbidden", "quota_exceeded"}
)


class SubAgentPayload(TypedDict):
    """Payload describing a sub-agent lifecycle event."""

    action: Literal["start", "stop"]
    agent_id: str
    parent_id: str | None
    depth: int
    model: str
    detail: str
    status: NotRequired[str]
    steps_completed: NotRequired[int]
    input_tokens: NotRequired[int]
    output_tokens: NotRequired[int]
    # The spawned AgentDef name (e.g. ``scg-path-probe``) — additive, present
    # only for an agent_type spawn so the trace projection can label the lane by
    # its definition rather than the model name.
    agent_type: NotRequired[str]
    # The child's compressed result (set only on the terminal ``stop``).
    summary: NotRequired[str]


class AgentMessagePayload(TypedDict):
    """Payload describing an intermediate agent text message."""

    text: str
    agent_id: str
    depth: int


class PlanProposedPayload(TypedDict):
    """Payload emitted when the LLM calls ``exit_plan_mode``."""

    plan_path: str
    revision: int
    content: str
    summary: NotRequired[str]


class PlanApprovedPayload(TypedDict):
    """Payload emitted when the user approves a proposed plan."""

    plan_path: str
    revision: int


class PlanRejectedPayload(TypedDict):
    """Payload emitted when the user rejects a proposed plan."""

    plan_path: str
    revision: int


class RecoveryPayload(TypedDict):
    """Payload emitted when the user triggers retry/continue after a failure."""

    action: Literal["retry", "continue"]


class LlmRetryPayload(TypedDict):
    """Payload emitted before a same-model LLM retry (``llm_retry`` event)."""

    agent_id: str
    depth: int
    step: int
    model: str
    attempt: int
    max_attempts: int
    error: str
    error_type: str
    delay: float
    retryable: bool


class LlmFallbackPayload(TypedDict):
    """Payload emitted when the run advances to another model (``llm_fallback``).

    ``reason`` is either a classifier reason (``quota_exhausted``,
    ``no_deployments``, ``context_window``, ``auth``) for a ``switch_model``
    decision, or ``retries_exhausted`` when the per-model retry cap tripped on a
    transient error. ``sticky`` is true when the destination model is pinned for
    the rest of the run (always true under the escalation policy).
    """

    agent_id: str
    depth: int
    step: int
    from_model: str
    to_model: str
    reason: str
    previous_error_type: str
    sticky: NotRequired[bool]


class LlmCallEndPayload(TypedDict):
    """Payload emitted for a SUCCESSFUL ``llm_call_end`` event.

    Brackets the whole ``RetryStrategy`` logical call — retries and fallback
    attempts included, not just the final attempt — so ``duration_ms`` is the
    wall time a caller actually waited, not the cheapest leg of it. The failed
    variant of this event (``success: False``) carries ``error_type``/``reason``
    instead and is a separate, untyped payload — it never reaches this arm.
    """

    agent_id: str
    depth: int
    step: int
    success: Literal[True]
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    reasoning_output_tokens: int
    cumulative_input_tokens: int
    cumulative_output_tokens: int
    duration_ms: int


class RecoveryHaltPayload(TypedDict):
    """Payload emitted when the doom-loop guard halts a no-progress run.

    The ``recovery`` event with ``action == "halt_no_progress"`` — distinct from
    :class:`RecoveryPayload` (user-triggered retry/continue).
    """

    action: Literal["halt_no_progress"]
    agent_id: str
    depth: int
    step: int
    tool: str


class TodoItemPayload(TypedDict):
    """One authoritative todo item: a label plus its lifecycle status.

    ``status`` is one of ``pending`` / ``in_progress`` / ``completed`` (see
    ``update_todos.TODO_STATUSES``).
    """

    label: str
    status: str


class TodosPayload(TypedDict):
    """Payload for the authoritative live todo list (``todos`` event).

    Re-emitted in FULL on every ``update_todos`` call (compaction-resilient).
    ``source`` discriminates the agent's live working set (``agent``) from an
    approved plan's roadmap (``plan``); ``agent_id`` attributes it to the
    emitting agent (the root, in practice).
    """

    items: list[TodoItemPayload]
    source: str
    agent_id: str | None


class DeviceToolCallPayload(TypedDict):
    """Payload for a client-declared device-tool invocation (``device_tool_call``).

    Emitted when a session-bound ``ClientDeclaredTool`` (see ``client_tools.py``)
    is invoked; delivered to the client over the session's existing SSE stream.
    The client fulfils the call by POSTing its result back, presenting
    ``call_token`` (single-use, ``hmac.compare_digest``-compared). Honest
    threat model: this proves the responder had SESSION-STREAM READ ACCESS
    (received the SSE event) and prevents replay (single-use, consumed-once)
    — it does NOT prove the response came from the physical device the call
    was dispatched to. Any concurrent viewer of the same session's stream
    (e.g. a console tab) receives the same token and could answer on the
    device's behalf; this is the accepted threat model, distinct from the
    session's own API key.
    ``expires_at`` is an epoch-seconds deadline after which the server gives
    up waiting and resolves the tool call with a ``device_timeout`` error.
    """

    call_id: str
    call_token: str
    tool_id: str
    args: dict[str, object]
    expires_at: float


class UserQuestionOptionPayload(TypedDict):
    """One selectable option on a ``user_question`` event."""

    label: str
    description: str | None


class UserQuestionItemPayload(TypedDict):
    """One question in a ``user_question`` event's group."""

    header: str
    question: str
    options: list[UserQuestionOptionPayload]
    multi_select: bool


class UserQuestionPayload(TypedDict):
    """Payload announcing a pending ask-user question group (``user_question``).

    Emitted by the api's question dispatcher when the root agent calls
    ``ask_user_question`` (see ``ask_user.py``); rides the session's SSE
    stream + backlog replay so every attached surface renders the card.
    ``call_token`` follows the ``device_tool_call`` threat model verbatim:
    it proves session-stream read access and prevents replay — not which
    surface answered.

    **This event is the DURABLE record of the question, and that is what makes
    a late answer possible.** The in-process waiter registry is a rendezvous,
    not a store; it dies with the run. A client rendering this card long after
    the run moved on can still answer, because the answer route recovers the
    questions and the token from THIS payload. So the two additive fields are
    not decoration: ``timeout_seconds`` is what a surface needs to show how
    long the run will wait, and ``notes_placeholder`` is what makes it render
    the free-text box at all. Both are ``None`` when the run declared neither.
    """

    call_id: str
    call_token: str
    questions: list[UserQuestionItemPayload]
    timeout_seconds: int | None
    notes_placeholder: str | None


class UserQuestionAnswerItemPayload(TypedDict):
    """One delivered answer (indexes XOR text) on the answered event."""

    selected_indexes: list[int] | None
    text: str | None


class UserQuestionAnsweredPayload(TypedDict):
    """Resolution record for a question group (``user_question_answered``).

    ``outcome`` is ``answered`` / ``declined`` (user sent a message instead)
    / ``interrupted`` / ``cancelled`` / ``timed_out``; ``answers`` and
    ``notes`` are present only when answered. Every surface — not just the one
    that answered — folds this onto its pending card.

    **Only ``answered`` settles a card.** The other four record that the RUN
    stopped waiting, which is not the same as the question being resolved: the
    user may still answer, and that answer reaches the session as a new turn.
    A surface that greys out its card on any terminal outcome would be hiding
    the affordance precisely when the user finally came back to use it.
    ``delivery`` distinguishes the two landing paths for an ``answered``
    outcome — ``run`` (the blocked tool call took it) vs ``message`` (the run
    had moved on, so it arrived as a new turn).
    """

    call_id: str
    outcome: str
    answered_via: str | None
    answers: list[UserQuestionAnswerItemPayload] | None
    notes: str | None
    delivery: str | None


class VerificationPayload(TypedDict):
    """One verifier-gate check verdict (``verification`` event).

    Bounded scalars ONLY: the grounded verifier stdout/stderr is injected into
    the model's own context (a SystemMessage), never onto the wire — so a
    chatty command can't bloat transcripts. ``attempt`` is 1-based; ``passed``
    is the interpreted verdict; ``exit_code``/``timed_out`` explain a failure.
    """

    agent_id: str
    depth: int
    step: int
    passed: bool
    attempt: int
    exit_code: int
    timed_out: bool


EventPayload = (
    ActionPlanPayload
    | PermissionPayload
    | ToolCallPayload
    | ToolResultPayload
    | UserPayload
    | AssistantPayload
    | CompletionPayload
    | SubAgentPayload
    | AgentMessagePayload
    | PlanProposedPayload
    | PlanApprovedPayload
    | PlanRejectedPayload
    | RecoveryPayload
    | LlmRetryPayload
    | LlmFallbackPayload
    | LlmCallEndPayload
    | RecoveryHaltPayload
    | TodosPayload
    | DeviceToolCallPayload
    | UserQuestionPayload
    | UserQuestionAnsweredPayload
    | VerificationPayload
    | dict[str, JsonValue]
)


class Event(TypedDict):
    """Base event payload stored in transcripts."""

    type: str
    payload: EventPayload


class EventRecord(Event):
    """Event payload with a persisted timestamp."""

    ts: str
