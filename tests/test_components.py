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
from meeseeks_core.config import set_config_override


def test_langfuse_disabled_flag(monkeypatch):
    """Disable langfuse via explicit env flag."""
    set_config_override({"langfuse": {"enabled": False}})
    status = resolve_langfuse_status()
    assert status.enabled is False
    assert "disabled" in (status.reason or "")


def test_home_assistant_requires_env(monkeypatch):
    """Home Assistant tool stays disabled without env configuration."""
    set_config_override({"home_assistant": {"enabled": True, "url": "", "token": ""}})
    status = resolve_home_assistant_status()
    assert status.enabled is False
    assert "home_assistant.url" in (status.reason or "") or "home_assistant.token" in (
        status.reason or ""
    )


def test_home_assistant_enabled(monkeypatch):
    """Enable Home Assistant when required env vars are present."""
    set_config_override(
        {"home_assistant": {"enabled": True, "url": "http://localhost", "token": "token"}}
    )
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
    module = types.ModuleType("langfuse.langchain")

    class CallbackHandler:
        def __init__(self, **_kwargs):
            pass

    module.CallbackHandler = CallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse.langchain", module)
    set_config_override({"langfuse": {"enabled": True, "public_key": "", "secret_key": ""}})
    status = resolve_langfuse_status()
    assert status.enabled is False
    assert "missing langfuse.public_key/langfuse.secret_key" in (status.reason or "")


def test_build_langfuse_handler_configured(monkeypatch):
    """Construct a Langfuse callback handler when configured."""
    created = {}
    langfuse_module = types.ModuleType("langfuse")
    langchain_module = types.ModuleType("langfuse.langchain")

    class Langfuse:
        def __init__(self, **kwargs):
            created["client"] = kwargs

    class CallbackHandler:
        def __init__(self, **kwargs):
            created.update(kwargs)

    langfuse_module.Langfuse = Langfuse
    langchain_module.CallbackHandler = CallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse", langfuse_module)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", langchain_module)
    set_config_override(
        {"langfuse": {"enabled": True, "public_key": "pub", "secret_key": "secret"}}
    )
    handler = build_langfuse_handler(
        user_id="user",
        session_id="sid",
        trace_name="trace",
        version="v1",
        release="dev",
    )
    assert handler is not None
    assert created["public_key"] == "pub"
    assert created["client"]["public_key"] == "pub"
    assert created["client"]["secret_key"] == "secret"
    metadata = getattr(handler, "langfuse_metadata", {})
    assert metadata.get("langfuse_user_id") == "user"


def test_build_langfuse_handler_disabled(monkeypatch):
    """Return None when Langfuse is disabled."""
    set_config_override({"langfuse": {"enabled": False}})
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
