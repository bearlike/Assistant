#!/usr/bin/env python3
"""Hook manager for orchestration lifecycle events."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import threading
import urllib.request
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

    def run_on_compact(
        self,
        session_id: str,
        *,
        summary: str = "",
        tokens_before: int = 0,
        tokens_saved: int = 0,
        events_summarized: int = 0,
    ) -> None:
        """Notify hooks that compaction occurred."""
        for hook in self.on_compact:
            try:
                hook(
                    session_id,
                    summary=summary,
                    tokens_before=tokens_before,
                    tokens_saved=tokens_saved,
                    events_summarized=events_summarized,
                )
            except Exception:
                logger.warning("on_compact hook failed", exc_info=True)

    @classmethod
    def load_from_config(cls, hooks_config: HooksConfig) -> HookManager:
        """Create a HookManager with hooks loaded from config."""
        manager = cls()
        pre_map = {"command": _make_command_hook, "http": _make_http_hook}
        post_map = {"command": _make_post_tool_hook, "http": _make_http_post_tool_hook}
        start_map = {"command": _make_session_hook, "http": _make_http_session_hook}
        end_map = {"command": _make_session_end_hook, "http": _make_http_session_end_hook}
        for entry in hooks_config.pre_tool_use:
            manager.pre_tool_use.append(pre_map.get(entry.type, _make_command_hook)(entry))
        for entry in hooks_config.post_tool_use:
            manager.post_tool_use.append(post_map.get(entry.type, _make_post_tool_hook)(entry))
        for entry in hooks_config.on_session_start:
            manager.on_session_start.append(start_map.get(entry.type, _make_session_hook)(entry))
        for entry in hooks_config.on_session_end:
            manager.on_session_end.append(end_map.get(entry.type, _make_session_end_hook)(entry))
        return manager


def _matches(matcher: str | None, tool_id: str) -> bool:
    """Check if a tool_id matches a hook's matcher pattern."""
    if matcher is None:
        return True
    return fnmatch.fnmatch(tool_id, matcher)


def _hook_env(action_step: ActionStep, result_content: str | None = None) -> dict[str, str]:
    """Build env vars to pass to command hooks."""
    env = dict(os.environ)
    env["MEESEEKS_TOOL_ID"] = action_step.tool_id or ""
    env["MEESEEKS_OPERATION"] = action_step.operation or ""
    if result_content is not None:
        env["MEESEEKS_TOOL_RESULT"] = result_content[:2000]
    return env


def _session_env(session_id: str, error: str | None = None) -> dict[str, str]:
    """Build env vars for session lifecycle command hooks."""
    env = dict(os.environ)
    env["MEESEEKS_SESSION_ID"] = session_id
    if error is not None:
        env["MEESEEKS_ERROR"] = error
    return env


def _make_command_hook(entry: HookEntry) -> Callable[[ActionStep], ActionStep]:
    """Create a pre-tool-use hook from a config entry."""

    def hook(action_step: ActionStep) -> ActionStep:
        if not _matches(entry.matcher, action_step.tool_id):
            return action_step
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_hook_env(action_step),
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
        content = getattr(result, "content", None)
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_hook_env(action_step, str(content) if content else None),
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
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_session_env(session_id),
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Session hook timed out or failed: %s", entry.command)

    return hook


def _make_session_end_hook(entry: HookEntry) -> Callable[[str, str | None], None]:
    """Create a session end hook from a config entry."""

    def hook(session_id: str, error: str | None = None) -> None:
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_session_env(session_id, error),
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Session end hook timed out or failed: %s", entry.command)

    return hook


# ---------------------------------------------------------------------------
# HTTP hook factories (fire-and-forget POST to external URLs)
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> None:
    """POST JSON payload to a URL. Called from a daemon thread."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
    except Exception:
        logger.warning("HTTP hook POST failed: %s", url, exc_info=True)


def _fire_http(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> None:
    """Launch a daemon thread to POST JSON without blocking."""
    threading.Thread(target=_post_json, args=(url, payload, headers, timeout), daemon=True).start()


def _make_http_hook(entry: HookEntry) -> Callable[[ActionStep], ActionStep]:
    """Create a pre-tool-use HTTP hook from a config entry."""

    def hook(action_step: ActionStep) -> ActionStep:
        if not _matches(entry.matcher, action_step.tool_id):
            return action_step
        payload = {
            "event": "pre_tool_use",
            "tool_id": action_step.tool_id,
            "operation": action_step.operation,
        }
        _fire_http(entry.url, payload, entry.headers, entry.timeout)
        return action_step

    return hook


def _make_http_post_tool_hook(
    entry: HookEntry,
) -> Callable[[ActionStep, MockSpeaker], MockSpeaker]:
    """Create a post-tool-use HTTP hook from a config entry."""

    def hook(action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        if not _matches(entry.matcher, action_step.tool_id):
            return result
        content = getattr(result, "content", None)
        payload: dict[str, Any] = {
            "event": "post_tool_use",
            "tool_id": action_step.tool_id,
            "operation": action_step.operation,
        }
        if content is not None:
            payload["result_preview"] = str(content)[:2000]
        _fire_http(entry.url, payload, entry.headers, entry.timeout)
        return result

    return hook


def _make_http_session_hook(entry: HookEntry) -> Callable[[str], None]:
    """Create a session start HTTP hook from a config entry."""

    def hook(session_id: str) -> None:
        _fire_http(
            entry.url,
            {"event": "session_start", "session_id": session_id},
            entry.headers,
            entry.timeout,
        )

    return hook


def _make_http_session_end_hook(entry: HookEntry) -> Callable[[str, str | None], None]:
    """Create a session end HTTP hook from a config entry."""

    def hook(session_id: str, error: str | None = None) -> None:
        payload: dict[str, Any] = {"event": "session_end", "session_id": session_id}
        if error is not None:
            payload["error"] = error
        _fire_http(entry.url, payload, entry.headers, entry.timeout)

    return hook


def default_hook_manager() -> HookManager:
    """Create a hook manager with no custom hooks registered.

    Returns:
        Empty HookManager instance.
    """
    return HookManager()


_PLUGIN_HOOK_MAP: dict[str, tuple[str, Callable]] = {
    "PreToolUse": ("pre_tool_use", _make_command_hook),
    "PostToolUse": ("post_tool_use", _make_post_tool_hook),
    "SessionStart": ("on_session_start", _make_session_hook),
    "SessionEnd": ("on_session_end", _make_session_end_hook),
}


def merge_plugin_hooks(
    manager: HookManager,
    hooks_json: dict[str, Any],
    plugin_root: str,
) -> None:
    """Translate Claude Code plugin hooks.json into HookManager callbacks.

    Supports: PreToolUse, PostToolUse, SessionStart, SessionEnd.
    Substitutes ${CLAUDE_PLUGIN_ROOT} in command strings.
    """
    from meeseeks_core.config import HookEntry
    from meeseeks_core.plugins import substitute_plugin_vars

    raw_hooks = hooks_json.get("hooks", {})
    for cc_event, entry_groups in raw_hooks.items():
        mapping = _PLUGIN_HOOK_MAP.get(cc_event)
        if mapping is None:
            continue
        slot_name, factory = mapping
        for group in entry_groups:
            matcher = group.get("matcher")
            for hook_def in group.get("hooks", []):
                if hook_def.get("type") != "command":
                    continue
                command = hook_def.get("command")
                if not command:
                    continue
                command = substitute_plugin_vars(command, plugin_root)
                entry = HookEntry(
                    type="command",
                    command=command,
                    matcher=matcher,
                    timeout=hook_def.get("timeout", 30),
                )
                getattr(manager, slot_name).append(factory(entry))


__all__ = [
    "HookManager",
    "default_hook_manager",
    "merge_plugin_hooks",
]
