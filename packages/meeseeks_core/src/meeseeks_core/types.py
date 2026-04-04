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


class AgentMessagePayload(TypedDict):
    """Payload describing an intermediate agent text message."""

    text: str
    agent_id: str
    depth: int


EventPayload = (
    ActionPlanPayload
    | PermissionPayload
    | ToolResultPayload
    | UserPayload
    | AssistantPayload
    | CompletionPayload
    | SubAgentPayload
    | AgentMessagePayload
    | dict[str, JsonValue]
)


class Event(TypedDict):
    """Base event payload stored in transcripts."""

    type: str
    payload: EventPayload


class EventRecord(Event):
    """Event payload with a persisted timestamp."""

    ts: str
