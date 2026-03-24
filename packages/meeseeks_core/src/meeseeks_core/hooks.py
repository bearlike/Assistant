#!/usr/bin/env python3
"""Hook manager for orchestration lifecycle events."""

from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from meeseeks_core.classes import ActionStep
from meeseeks_core.common import MockSpeaker, get_logger
from meeseeks_core.permissions import PermissionDecision
from meeseeks_core.types import EventRecord

if TYPE_CHECKING:
    from meeseeks_core.config import HookEntry, HooksConfig
    from meeseeks_core.hypervisor import AgentHandle

logger = get_logger(name="core.hooks")


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
    on_session_start: list[Callable[[str], None]] = field(default_factory=list)
    on_session_end: list[Callable[[str, str | None], None]] = field(default_factory=list)
    on_compact: list[Callable[..., None]] = field(default_factory=list)

    def run_pre_tool_use(self, action_step: ActionStep) -> ActionStep:
        """Apply pre-tool hooks to an action step.

        Args:
            action_step: Action step to process.

        Returns:
            Updated action step after hooks run.
        """
        for hook in self.pre_tool_use:
            try:
                action_step = hook(action_step)
            except Exception:
                logger.warning("Pre-tool hook failed", exc_info=True)
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
            try:
                result = hook(action_step, result)
            except Exception:
                logger.warning("Post-tool hook failed", exc_info=True)
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
            try:
                decision = hook(action_step, decision)
            except Exception:
                logger.warning("Permission hook failed", exc_info=True)
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
            try:
                event_list = hook(event_list)
            except Exception:
                logger.warning("Pre-compact hook failed", exc_info=True)
        return event_list

    def run_on_agent_start(self, handle: AgentHandle) -> None:
        """Notify hooks that an agent has started."""
        for hook in self.on_agent_start:
            try:
                hook(handle)
            except Exception:
                logger.warning("on_agent_start hook failed", exc_info=True)

    def run_on_agent_stop(self, handle: AgentHandle) -> None:
        """Notify hooks that an agent has stopped."""
        for hook in self.on_agent_stop:
            try:
                hook(handle)
            except Exception:
                logger.warning("on_agent_stop hook failed", exc_info=True)

    def run_on_session_start(self, session_id: str) -> None:
        """Notify hooks that a session has started."""
        for hook in self.on_session_start:
            try:
                hook(session_id)
            except Exception:
                logger.warning("on_session_start hook failed", exc_info=True)

    def run_on_session_end(self, session_id: str, error: str | None = None) -> None:
        """Notify hooks that a session has ended."""
        for hook in self.on_session_end:
            try:
                hook(session_id, error)
            except Exception:
                logger.warning("on_session_end hook failed", exc_info=True)

    def run_on_compact(self, *args: Any, **kwargs: Any) -> None:
        """Notify hooks that compaction occurred."""
        for hook in self.on_compact:
            try:
                hook(*args, **kwargs)
            except Exception:
                logger.warning("on_compact hook failed", exc_info=True)

    @classmethod
    def load_from_config(cls, hooks_config: HooksConfig) -> HookManager:
        """Create a HookManager with hooks loaded from config."""
        manager = cls()
        for entry in hooks_config.pre_tool_use:
            manager.pre_tool_use.append(_make_command_hook(entry))
        for entry in hooks_config.post_tool_use:
            manager.post_tool_use.append(_make_post_tool_hook(entry))
        for entry in hooks_config.on_session_start:
            manager.on_session_start.append(_make_session_hook(entry))
        for entry in hooks_config.on_session_end:
            manager.on_session_end.append(_make_session_end_hook(entry))
        return manager


def _matches(matcher: str | None, tool_id: str) -> bool:
    """Check if a tool_id matches a hook's matcher pattern."""
    if matcher is None:
        return True
    return fnmatch.fnmatch(tool_id, matcher)


def _make_command_hook(entry: HookEntry) -> Callable[[ActionStep], ActionStep]:
    """Create a pre-tool-use hook from a config entry."""
    def hook(action_step: ActionStep) -> ActionStep:
        if not _matches(entry.matcher, action_step.tool_id):
            return action_step
        try:
            subprocess.run(
                entry.command, shell=True, timeout=entry.timeout,
                capture_output=True, text=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Command hook timed out or failed: %s", entry.command)
        return action_step
    return hook


def _make_post_tool_hook(entry: HookEntry) -> Callable[[ActionStep, MockSpeaker], MockSpeaker]:
    """Create a post-tool-use hook from a config entry."""
    def hook(action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        if not _matches(entry.matcher, action_step.tool_id):
            return result
        try:
            subprocess.run(
                entry.command, shell=True, timeout=entry.timeout,
                capture_output=True, text=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Command hook timed out or failed: %s", entry.command)
        return result
    return hook


def _make_session_hook(entry: HookEntry) -> Callable[[str], None]:
    """Create a session lifecycle hook from a config entry."""
    def hook(session_id: str) -> None:
        try:
            subprocess.run(
                entry.command, shell=True, timeout=entry.timeout,
                capture_output=True, text=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Session hook timed out or failed: %s", entry.command)
    return hook


def _make_session_end_hook(entry: HookEntry) -> Callable[[str, str | None], None]:
    """Create a session end hook from a config entry."""
    def hook(session_id: str, error: str | None = None) -> None:
        try:
            subprocess.run(
                entry.command, shell=True, timeout=entry.timeout,
                capture_output=True, text=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Session end hook timed out or failed: %s", entry.command)
    return hook


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
