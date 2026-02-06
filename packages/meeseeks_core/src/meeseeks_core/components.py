#!/usr/bin/env python3
"""Helpers for optional components and observability integration."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from meeseeks_core.common import get_logger
from meeseeks_core.config import get_config
from meeseeks_core.types import JsonValue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
    from langfuse.types import TraceContext
else:
    TraceContext = dict[str, str]

logging = get_logger(name="core.components")

_LANGFUSE_TRACE_CONTEXT: ContextVar[TraceContext | None] = ContextVar(
    "langfuse_trace_context",
    default=None,
)
_LANGFUSE_SESSION_ID: ContextVar[str | None] = ContextVar("langfuse_session_id", default=None)
_LANGFUSE_USER_ID: ContextVar[str | None] = ContextVar("langfuse_user_id", default=None)


@dataclass(frozen=True)
class ComponentStatus:
    """Describe whether a component is enabled and why."""

    name: str
    enabled: bool
    reason: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


def resolve_langfuse_status() -> ComponentStatus:
    """Determine whether Langfuse callbacks are available and configured."""
    enabled, reason, metadata = get_config().langfuse.evaluate()
    return ComponentStatus(name="langfuse", enabled=enabled, reason=reason, metadata=metadata)


def build_langfuse_handler(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    version: str,
    release: str,
    trace_context: TraceContext | None = None,
) -> LangfuseCallbackHandler | None:
    """Create a Langfuse callback handler when configured."""
    status = resolve_langfuse_status()
    if not status.enabled:
        logging.debug("Langfuse disabled: {}", status.reason)
        return None

    config = get_config().langfuse
    _ensure_langfuse_client(config)

    from langfuse.langchain import CallbackHandler

    trace_context = trace_context or _LANGFUSE_TRACE_CONTEXT.get()
    session_id_value = _LANGFUSE_SESSION_ID.get() or session_id
    user_id_value = _LANGFUSE_USER_ID.get() or user_id

    try:
        handler = CallbackHandler(public_key=config.public_key or None, trace_context=trace_context)
        _attach_langfuse_metadata(
            handler,
            user_id=user_id_value,
            session_id=session_id_value,
            trace_name=trace_name,
            version=version,
            release=release,
        )
        return handler
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Langfuse initialization failed: {}", exc)
        return None


def resolve_home_assistant_status() -> ComponentStatus:
    """Determine whether the Home Assistant tool is configured."""
    enabled, reason, metadata = get_config().home_assistant.evaluate()
    return ComponentStatus(
        name="home_assistant_tool",
        enabled=enabled,
        reason=reason,
        metadata=metadata,
    )


def format_component_status(statuses: Iterable[ComponentStatus]) -> str:
    """Format component statuses for inclusion in prompts."""
    lines: list[str] = []
    for status in statuses:
        state = "enabled" if status.enabled else "disabled"
        reason = f" ({status.reason})" if status.reason else ""
        lines.append(f"- {status.name}: {state}{reason}")
    return "\n".join(lines)


def _is_hex_trace_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", value))


def _build_langfuse_trace_context(session_id: str | None) -> TraceContext | None:
    if not session_id:
        return None
    if _is_hex_trace_id(session_id):
        return cast(TraceContext, {"trace_id": session_id})
    try:
        from langfuse import Langfuse
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        trace_id = Langfuse.create_trace_id(seed=session_id)
    except Exception:  # pragma: no cover - defensive
        return None
    if not trace_id or not _is_hex_trace_id(trace_id):
        return None
    return cast(TraceContext, {"trace_id": trace_id})


@contextmanager
def langfuse_session_context(session_id: str, *, user_id: str | None = None) -> Iterator[None]:
    """Bind a stable Langfuse trace context to the current session."""
    trace_context = _build_langfuse_trace_context(session_id)
    token_ctx = _LANGFUSE_TRACE_CONTEXT.set(trace_context)
    token_session = _LANGFUSE_SESSION_ID.set(session_id)
    token_user = _LANGFUSE_USER_ID.set(user_id or session_id)
    try:
        yield
    finally:
        _LANGFUSE_TRACE_CONTEXT.reset(token_ctx)
        _LANGFUSE_SESSION_ID.reset(token_session)
        _LANGFUSE_USER_ID.reset(token_user)


@contextmanager
def langfuse_trace_span(name: str) -> Iterator[object | None]:
    """Open a Langfuse span bound to the current session trace context."""
    status = resolve_langfuse_status()
    if not status.enabled:
        yield None
        return
    trace_context = _LANGFUSE_TRACE_CONTEXT.get()
    if not trace_context:
        yield None
        return
    try:
        from langfuse import get_client
    except Exception:  # pragma: no cover - defensive
        yield None
        return
    try:
        langfuse = get_client()
        with langfuse.start_as_current_observation(
            as_type="span",
            name=name,
            trace_context=trace_context,
        ) as span:
            yield span
    except Exception:  # pragma: no cover - defensive
        yield None


def _ensure_langfuse_client(config) -> None:
    if config is None:
        return
    if not config.public_key or not config.secret_key:
        return

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", config.secret_key)
    if config.host:
        os.environ.setdefault("LANGFUSE_BASE_URL", config.host)
        os.environ.setdefault("LANGFUSE_HOST", config.host)

    try:
        from langfuse import Langfuse
    except Exception as exc:  # pragma: no cover - defensive
        logging.debug("Langfuse client unavailable: {}", exc)
        return

    try:
        Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            base_url=config.host or None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.debug("Langfuse client init failed: {}", exc)


def _attach_langfuse_metadata(
    handler: object,
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    version: str,
    release: str,
) -> None:
    metadata: dict[str, object] = {}
    if user_id:
        metadata["langfuse_user_id"] = user_id
    if session_id:
        metadata["langfuse_session_id"] = session_id
    tags: list[str] = []
    if trace_name:
        tags.append(trace_name)
    if version:
        tags.append(f"version:{version}")
    if release:
        tags.append(f"release:{release}")
    if tags:
        metadata["langfuse_tags"] = tags
    if metadata:
        setattr(handler, "langfuse_metadata", metadata)


__all__ = [
    "ComponentStatus",
    "build_langfuse_handler",
    "format_component_status",
    "langfuse_session_context",
    "langfuse_trace_span",
    "resolve_home_assistant_status",
    "resolve_langfuse_status",
]
