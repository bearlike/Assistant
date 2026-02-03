"""Tests for optional component helpers."""
from core.components import (
    ComponentStatus,
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
    assert status_text == (
        "- langfuse: disabled (disabled)\n"
        "- home_assistant_tool: enabled"
    )
