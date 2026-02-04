"""Tests for prompt/tool injection logic."""

from meeseeks_core import planning as planning_module
from meeseeks_core.common import get_system_prompt, ha_render_system_prompt
from meeseeks_core.context import ContextSnapshot
from meeseeks_core.planning import PromptBuilder
from meeseeks_core.token_budget import get_token_budget
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec, load_registry


def _build_prompt(registry, *, recent_events=None, selected_events=None, summary=None):
    context = ContextSnapshot(
        summary=summary,
        recent_events=recent_events or [],
        selected_events=selected_events,
        events=[],
        budget=get_token_budget([], None, None),
    )
    return PromptBuilder(registry).build(get_system_prompt(), context, component_status=None)


def test_prompt_excludes_home_assistant_when_disabled(monkeypatch):
    """Ensure HA guidance is omitted when the tool is disabled."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    monkeypatch.setenv("MESEEKS_HOME_ASSISTANT_ENABLED", "0")
    registry = load_registry()
    prompt = _build_prompt(registry)
    assert "Additional Devices Information" not in prompt
    assert 'action_consumer="home_assistant_tool"' not in prompt


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
    assert 'action_consumer="home_assistant_tool"' in prompt


def test_prompt_includes_recent_and_selected_events(monkeypatch):
    """Ensure recent/selected events are rendered in the system prompt."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    registry = load_registry()
    prompt = _build_prompt(
        registry,
        recent_events=[{"type": "user", "payload": {"text": "Hi"}}],
        selected_events=[{"type": "tool_result", "payload": {"result": "ok"}}],
    )
    assert "Recent conversation" in prompt
    assert "Relevant earlier context" in prompt


def test_prompt_includes_summary(monkeypatch):
    """Include summary lines when present in context."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_MCP_CONFIG", raising=False)
    registry = load_registry()
    prompt = _build_prompt(registry, summary="Remember this")
    assert "Session summary" in prompt
    assert "Remember this" in prompt


def test_prompt_skips_missing_tool_prompt(monkeypatch):
    """Skip tool prompt guidance when the prompt file fails to load."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy",
            name="Dummy Tool",
            description="Test",
            factory=lambda: object(),
            prompt_path="missing.txt",
        )
    )

    def _raise_prompt(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(planning_module, "get_system_prompt", _raise_prompt)
    prompt = _build_prompt(registry)
    assert "Tool guidance" not in prompt


def test_prompt_includes_mcp_schema(monkeypatch, tmp_path):
    """Include MCP tool schema hints in the prompt when available."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        """{
  "tools": [
    {
      "tool_id": "mcp_srv_tool",
      "name": "Test Tool",
      "description": "Test",
      "kind": "mcp",
      "server": "srv",
      "tool": "ask",
      "schema": {
        "required": ["question"],
        "properties": {"question": {"type": "string", "description": "Query"}}
      }
    }
  ]
}""",
        encoding="utf-8",
    )
    registry = load_registry(str(manifest_path))
    prompt = _build_prompt(registry)
    assert "MCP tool input schemas" in prompt
    assert "mcp_srv_tool" in prompt
    assert "question" in prompt


def test_ha_render_system_prompt_includes_entities():
    """Render the HA prompt with provided entity list."""
    prompt = ha_render_system_prompt(all_entities=["scene.lamp_power_on"])
    assert "scene.lamp_power_on" in prompt


def test_action_planner_prompt_identity():
    """Ensure the planner prompt includes agent identity and boundaries."""
    prompt = get_system_prompt()
    assert "Meeseeks, a task-completing agent" in prompt
    assert "Examples in the prompt are illustrative only" in prompt


def test_response_synthesizer_prompt_identity():
    """Ensure the response prompt includes agent identity."""
    prompt = get_system_prompt("response-synthesizer")
    assert "Meeseeks, a task-completing agent" in prompt
