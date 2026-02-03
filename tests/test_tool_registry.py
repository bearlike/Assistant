"""Tests for tool registry loading behavior."""
import json

from core.tool_registry import load_registry


def test_default_registry(monkeypatch):
    """Load built-in tools when no manifest is configured."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_HOME_ASSISTANT_ENABLED", raising=False)
    registry = load_registry()
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    enabled_ids = {spec.tool_id for spec in registry.list_specs()}
    assert "home_assistant_tool" in tool_ids
    assert "talk_to_user_tool" in enabled_ids


def test_default_registry_homeassistant_enabled(monkeypatch):
    """Enable Home Assistant tool when required env vars are set."""
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.delenv("MESEEKS_HOME_ASSISTANT_ENABLED", raising=False)
    monkeypatch.setenv("HA_URL", "http://localhost")
    monkeypatch.setenv("HA_TOKEN", "token")
    registry = load_registry()
    enabled_ids = {spec.tool_id for spec in registry.list_specs()}
    assert "home_assistant_tool" in enabled_ids


def test_registry_disables_on_factory_error():
    """Disable tools that fail during initialization."""
    from core.tool_registry import ToolRegistry, ToolSpec

    def _boom():
        raise RuntimeError("nope")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="boom_tool",
            name="Boom",
            description="Boom",
            factory=_boom,
        )
    )
    assert registry.get("boom_tool") is None
    spec = next(
        spec for spec in registry.list_specs(include_disabled=True) if spec.tool_id == "boom_tool"
    )
    assert spec.enabled is False
    assert "Initialization failed" in spec.metadata.get("disabled_reason", "")


def test_registry_talk_to_user_skips_llm(monkeypatch, tmp_path):
    """Keep TalkToUser enabled even if LLM initialization would fail."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("LLM init should be skipped")

    monkeypatch.setattr("core.classes.build_chat_model", _boom)
    registry = load_registry()
    tool = registry.get("talk_to_user_tool")
    assert tool is not None


def test_manifest_local_tool(tmp_path, monkeypatch):
    """Load a local tool from a manifest entry."""
    module_path = tmp_path / "dummy_tool.py"
    module_path.write_text(
        "class DummyTool:\n"
        "    def run(self, action_step):\n"
        "        return None\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_id": "dummy_tool",
                        "name": "Dummy",
                        "description": "Test tool",
                        "module": "dummy_tool",
                        "class": "DummyTool",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    registry = load_registry(str(manifest_path))
    tool = registry.get("dummy_tool")

    assert tool is not None
    assert tool.__class__.__name__ == "DummyTool"


def test_manifest_empty_falls_back(tmp_path, monkeypatch):
    """Fall back to defaults when manifest contains no tools."""
    monkeypatch.delenv("MESEEKS_HOME_ASSISTANT_ENABLED", raising=False)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"tools": []}), encoding="utf-8")

    registry = load_registry(str(manifest_path))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    enabled_ids = {spec.tool_id for spec in registry.list_specs()}
    assert "home_assistant_tool" in tool_ids
    assert "talk_to_user_tool" in enabled_ids


def test_auto_manifest_from_mcp_config(tmp_path, monkeypatch):
    """Auto-generate a manifest when only MCP config is provided."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "url": "http://example"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_MCP_CONFIG", str(config_path))
    monkeypatch.delenv("MESEEKS_TOOL_MANIFEST", raising=False)
    monkeypatch.setenv("MESEEKS_CONFIG_DIR", str(tmp_path))

    monkeypatch.setattr(
        "core.tool_registry.discover_mcp_tools",
        lambda _config: {"srv": ["tool-a"]},
    )

    registry = load_registry()
    tool_ids = {spec.tool_id for spec in registry.list_specs()}
    assert any(tool_id.startswith("mcp_srv_tool_a") for tool_id in tool_ids)
    manifest_path = tmp_path / "tool-manifest.auto.json"
    assert manifest_path.exists()
