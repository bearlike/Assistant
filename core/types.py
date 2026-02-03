#!/usr/bin/env python3
"""Shared type definitions for core components."""
from __future__ import annotations

from typing import Any, TypedDict

from typing_extensions import NotRequired


class ActionStepPayload(TypedDict):
    """Serialized action step data sent to/from orchestration."""
    action_consumer: str
    action_type: str
    action_argument: str
    title: NotRequired[str]
    objective: NotRequired[str]
    execution_checklist: NotRequired[list[str]]
    expected_output: NotRequired[str]


class ActionPlanPayload(TypedDict):
    """Payload describing an action plan."""
    steps: list[ActionStepPayload]


class PermissionPayload(TypedDict):
    """Payload emitted for permission decisions."""
    action_consumer: str
    action_type: str
    action_argument: str
    decision: str


class ToolResultPayload(TypedDict):
    """Payload describing the outcome of a tool invocation."""
    action_consumer: str
    action_type: str
    action_argument: str
    result: str | None
    error: NotRequired[str]


class UserPayload(TypedDict):
    """Payload describing a user message."""
    text: str


class CompletionPayload(TypedDict):
    """Payload describing overall completion state."""
    done: bool
    done_reason: str | None
    task_result: str | None


EventPayload = (
    ActionPlanPayload
    | PermissionPayload
    | ToolResultPayload
    | UserPayload
    | CompletionPayload
    | dict[str, Any]
)


class Event(TypedDict):
    """Base event payload stored in transcripts."""
    type: str
    payload: EventPayload


class EventRecord(Event):
    """Event payload with a persisted timestamp."""
    ts: str


__all__ = [
    "ActionPlanPayload",
    "ActionStepPayload",
    "CompletionPayload",
    "Event",
    "EventPayload",
    "EventRecord",
    "PermissionPayload",
    "ToolResultPayload",
    "UserPayload",
]
