"""Tests for prompt/tool injection logic."""
from core import task_master
from core.common import get_system_prompt
from core.tool_registry import load_registry


def _build_prompt(registry):
    return task_master._augment_system_prompt(  # pylint: disable=protected-access
        get_system_prompt(),
        registry,
        session_summary=None,
        recent_events=None,
        selected_events=None,
        component_status=None,
    )


def test_prompt_excludes_home_assistant_when_disabled(monkeypatch):
    """Ensure HA guidance is omitted when the tool is disabled."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    monkeypatch.setenv("MESEEKS_HOME_ASSISTANT_ENABLED", "0")
    registry = load_registry()
    prompt = _build_prompt(registry)
    assert "Additional Devices Information" not in prompt
    assert "action_consumer=\"home_assistant_tool\"" not in prompt


def test_prompt_includes_home_assistant_when_enabled(monkeypatch):
    """Ensure HA guidance is included when the tool is enabled."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    monkeypatch.delenv("MESEEKS_HOME_ASSISTANT_ENABLED", raising=False)
    monkeypatch.setenv("HA_URL", "http://example")
    monkeypatch.setenv("HA_TOKEN", "token")
    registry = load_registry()
    prompt = _build_prompt(registry)
    assert "Additional Devices Information" in prompt
    assert "action_consumer=\"home_assistant_tool\"" in prompt


def test_prompt_includes_recent_and_selected_events(monkeypatch):
    """Ensure recent/selected events are rendered in the system prompt."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    registry = load_registry()
    prompt = task_master._augment_system_prompt(  # pylint: disable=protected-access
        get_system_prompt(),
        registry,
        session_summary=None,
        recent_events=[{"type": "user", "payload": {"text": "Hi"}}],
        selected_events=[{"type": "tool_result", "payload": {"result": "ok"}}],
        component_status=None,
    )
    assert "Recent conversation" in prompt
    assert "Relevant earlier context" in prompt
