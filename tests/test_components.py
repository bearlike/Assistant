"""Tests for optional component helpers."""

import sys
import types

from meeseeks_core.components import (
    ComponentStatus,
    build_langfuse_handler,
    format_component_status,
    resolve_home_assistant_status,
    resolve_langfuse_status,
)


def test_langfuse_disabled_flag(monkeypatch):
    """Disable langfuse via explicit env flag."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "0")
    status = resolve_langfuse_status()
    assert status.enabled is False
    assert "disabled" in (status.reason or "")


def test_home_assistant_requires_env(monkeypatch):
    """Home Assistant tool stays disabled without env configuration."""
    monkeypatch.delenv("MESEEKS_HOME_ASSISTANT_ENABLED", raising=False)
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    status = resolve_home_assistant_status()
    assert status.enabled is False
    assert "HA_URL" in (status.reason or "") or "HA_TOKEN" in (status.reason or "")


def test_home_assistant_enabled(monkeypatch):
    """Enable Home Assistant when required env vars are present."""
    monkeypatch.delenv("MESEEKS_HOME_ASSISTANT_ENABLED", raising=False)
    monkeypatch.setenv("HA_URL", "http://localhost")
    monkeypatch.setenv("HA_TOKEN", "token")
    status = resolve_home_assistant_status()
    assert status.enabled is True


def test_format_component_status():
    """Format component status lines for prompts."""
    status_text = format_component_status(
        [
            ComponentStatus(name="langfuse", enabled=False, reason="disabled"),
            ComponentStatus(name="home_assistant_tool", enabled=True),
        ]
    )
    assert status_text == ("- langfuse: disabled (disabled)\n" "- home_assistant_tool: enabled")


def test_langfuse_status_requires_keys(monkeypatch):
    """Disable Langfuse when keys are missing but module is present."""
    module = types.ModuleType("langfuse.callback")

    class CallbackHandler:
        def __init__(self, **_kwargs):
            pass

    module.CallbackHandler = CallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse.callback", module)
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    status = resolve_langfuse_status()
    assert status.enabled is False
    assert "missing LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY" in (status.reason or "")


def test_build_langfuse_handler_configured(monkeypatch):
    """Construct a Langfuse callback handler when configured."""
    created = {}
    module = types.ModuleType("langfuse.callback")

    class CallbackHandler:
        def __init__(self, **kwargs):
            created.update(kwargs)

    module.CallbackHandler = CallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse.callback", module)
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pub")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    handler = build_langfuse_handler(
        user_id="user",
        session_id="sid",
        trace_name="trace",
        version="v1",
        release="dev",
    )
    assert handler is not None
    assert created["user_id"] == "user"


def test_build_langfuse_handler_disabled(monkeypatch):
    """Return None when Langfuse is disabled."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "0")
    assert (
        build_langfuse_handler(
            user_id="user",
            session_id="sid",
            trace_name="trace",
            version="v1",
            release="dev",
        )
        is None
    )
