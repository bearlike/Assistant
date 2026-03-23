#!/usr/bin/env python3
"""Hook manager for orchestration lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from meeseeks_core.classes import ActionStep
from meeseeks_core.common import MockSpeaker
from meeseeks_core.permissions import PermissionDecision
from meeseeks_core.types import EventRecord

# TYPE_CHECKING avoids circular import with agent_context.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meeseeks_core.agent_context import AgentHandle


@dataclass
class HookManager:
    """Container for hook callbacks used during orchestration."""

    pre_tool_use: list[Callable[[ActionStep], ActionStep]] = field(default_factory=list)
    post_tool_use: list[Callable[[ActionStep, MockSpeaker], MockSpeaker]] = field(
        default_factory=list
    )
    permission_request: list[Callable[[ActionStep, PermissionDecision], PermissionDecision]] = (
        field(default_factory=list)
    )
    pre_compact: list[Callable[[list[EventRecord]], list[EventRecord]]] = field(
        default_factory=list
    )
    on_agent_start: list[Callable[[AgentHandle], None]] = field(default_factory=list)
    on_agent_stop: list[Callable[[AgentHandle], None]] = field(default_factory=list)

    def run_pre_tool_use(self, action_step: ActionStep) -> ActionStep:
        """Apply pre-tool hooks to an action step.

        Args:
            action_step: Action step to process.

        Returns:
            Updated action step after hooks run.
        """
        for hook in self.pre_tool_use:
            action_step = hook(action_step)
        return action_step

    def run_post_tool_use(self, action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        """Apply post-tool hooks to a tool result.

        Args:
            action_step: Action step that was executed.
            result: Result returned by the tool.

        Returns:
            Updated result after hooks run.
        """
        for hook in self.post_tool_use:
            result = hook(action_step, result)
        return result

    def run_permission_request(
        self, action_step: ActionStep, decision: PermissionDecision
    ) -> PermissionDecision:
        """Apply permission hooks to a decision outcome.

        Args:
            action_step: Action step under review.
            decision: Current decision to modify.

        Returns:
            Updated permission decision after hooks run.
        """
        for hook in self.permission_request:
            decision = hook(action_step, decision)
        return decision

    def run_pre_compact(self, events: Iterable[EventRecord]) -> list[EventRecord]:
        """Apply compaction hooks to events prior to summarization.

        Args:
            events: Iterable of event records.

        Returns:
            List of event records after hooks run.
        """
        event_list: list[EventRecord] = list(events)
        for hook in self.pre_compact:
            event_list = hook(event_list)
        return event_list


    def run_on_agent_start(self, handle: AgentHandle) -> None:
        """Notify hooks that an agent has started."""
        for hook in self.on_agent_start:
            hook(handle)

    def run_on_agent_stop(self, handle: AgentHandle) -> None:
        """Notify hooks that an agent has stopped."""
        for hook in self.on_agent_stop:
            hook(handle)


def default_hook_manager() -> HookManager:
    """Create a hook manager with no custom hooks registered.

    Returns:
        Empty HookManager instance.
    """
    return HookManager()


__all__ = [
    "HookManager",
    "default_hook_manager",
]
