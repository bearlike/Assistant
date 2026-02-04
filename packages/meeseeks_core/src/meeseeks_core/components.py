#!/usr/bin/env python3
"""Helpers for optional components and observability integration."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from meeseeks_core.common import get_logger
from meeseeks_core.types import JsonValue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langfuse.callback import CallbackHandler as LangfuseCallbackHandler

logging = get_logger(name="core.components")


@dataclass(frozen=True)
class ComponentStatus:
    """Describe whether a component is enabled and why."""

    name: str
    enabled: bool
    reason: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


def _env_falsey(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"0", "false", "no", "off"}


def resolve_langfuse_status() -> ComponentStatus:
    """Determine whether Langfuse callbacks are available and configured."""
    enabled_flag = os.getenv("LANGFUSE_ENABLED")
    if _env_falsey(enabled_flag):
        return ComponentStatus(
            name="langfuse",
            enabled=False,
            reason="disabled via LANGFUSE_ENABLED",
        )
    try:
        from langfuse.callback import CallbackHandler  # noqa: F401
    except ImportError:
        return ComponentStatus(
            name="langfuse",
            enabled=False,
            reason="langfuse not installed",
        )

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        return ComponentStatus(
            name="langfuse",
            enabled=False,
            reason="missing LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY",
            metadata={"required_env": ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]},
        )
    return ComponentStatus(name="langfuse", enabled=True)


def build_langfuse_handler(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    version: str,
    release: str,
) -> LangfuseCallbackHandler | None:
    """Create a Langfuse callback handler when configured."""
    status = resolve_langfuse_status()
    if not status.enabled:
        logging.debug("Langfuse disabled: {}", status.reason)
        return None

    from langfuse.callback import CallbackHandler

    try:
        return CallbackHandler(
            user_id=user_id,
            session_id=session_id,
            trace_name=trace_name,
            version=version,
            release=release,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Langfuse initialization failed: {}", exc)
        return None


def resolve_home_assistant_status() -> ComponentStatus:
    """Determine whether the Home Assistant tool is configured."""
    enabled_flag = os.getenv("MESEEKS_HOME_ASSISTANT_ENABLED")
    if _env_falsey(enabled_flag):
        return ComponentStatus(
            name="home_assistant_tool",
            enabled=False,
            reason="disabled via MESEEKS_HOME_ASSISTANT_ENABLED",
        )
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    if not base_url or not token:
        return ComponentStatus(
            name="home_assistant_tool",
            enabled=False,
            reason="missing HA_URL/HA_TOKEN",
            metadata={"required_env": ["HA_URL", "HA_TOKEN"]},
        )
    return ComponentStatus(name="home_assistant_tool", enabled=True)


def format_component_status(statuses: Iterable[ComponentStatus]) -> str:
    """Format component statuses for inclusion in prompts."""
    lines: list[str] = []
    for status in statuses:
        state = "enabled" if status.enabled else "disabled"
        reason = f" ({status.reason})" if status.reason else ""
        lines.append(f"- {status.name}: {state}{reason}")
    return "\n".join(lines)


__all__ = [
    "ComponentStatus",
    "build_langfuse_handler",
    "format_component_status",
    "resolve_home_assistant_status",
    "resolve_langfuse_status",
]
