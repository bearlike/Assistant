#!/usr/bin/env python3
"""Shared type definitions for core components."""

from __future__ import annotations

from typing import Literal, TypedDict

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


class ToolResultPayload(TypedDict):
    """Payload describing the outcome of a tool invocation."""

    tool_id: str
    operation: str
    tool_input: ToolInput
    result: str | None
    success: NotRequired[bool]
    summary: NotRequired[str]
    error: NotRequired[str]


class UserPayload(TypedDict):
    """Payload describing a user message."""

    text: str
    # Present only when the turn carried attachments — the same
    # AttachmentDescriptor dicts riding the sibling ``context`` event's
    # ``payload.attachments`` (see ``mewbo_core.context._iter_attachments``),
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
    error: NotRequired[str]
    last_error: NotRequired[str]


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
    device's behalf; this is the accepted Phase 1 threat model, distinct
    from the session's own API key. Verified per-device identity is Phase 5.
    ``expires_at`` is an epoch-seconds deadline after which the server gives
    up waiting and resolves the tool call with a ``device_timeout`` error.
    """

    call_id: str
    call_token: str
    tool_id: str
    args: dict[str, object]
    expires_at: float


EventPayload = (
    ActionPlanPayload
    | PermissionPayload
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
    | RecoveryHaltPayload
    | TodosPayload
    | DeviceToolCallPayload
    | dict[str, JsonValue]
)


class Event(TypedDict):
    """Base event payload stored in transcripts."""

    type: str
    payload: EventPayload


class EventRecord(Event):
    """Event payload with a persisted timestamp."""

    ts: str
