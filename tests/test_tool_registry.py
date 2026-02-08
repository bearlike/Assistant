"""Tests for tool registry loading behavior."""

import json
from types import SimpleNamespace

from meeseeks_core.config import set_config_override, set_mcp_config_path
from meeseeks_core.tool_registry import (
    ToolRegistry,
    ToolSpec,
    _default_manifest_cache_path,
    _ensure_auto_manifest,
    load_registry,
)


def test_default_registry(monkeypatch):
    """Load built-in tools when no manifest is configured."""
    set_mcp_config_path("")
    set_config_override({"home_assistant": {"enabled": False}})
    registry = load_registry()
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "home_assistant_tool" in tool_ids


def test_default_registry_homeassistant_enabled(monkeypatch):
    """Enable Home Assistant tool when required env vars are set."""
    set_mcp_config_path("")
    set_config_override(
        {"home_assistant": {"enabled": True, "url": "http://localhost", "token": "token"}}
    )
    registry = load_registry()
    enabled_ids = {spec.tool_id for spec in registry.list_specs()}
    assert "home_assistant_tool" in enabled_ids


def test_registry_disables_on_factory_error():
    """Disable tools that fail during initialization."""
    from meeseeks_core.tool_registry import ToolRegistry, ToolSpec

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


def test_manifest_local_tool(tmp_path, monkeypatch):
    """Load a local tool from a manifest entry."""
    module_path = tmp_path / "dummy_tool.py"
    module_path.write_text(
        "class DummyTool:\n" "    def run(self, action_step):\n" "        return None\n",
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
    set_config_override({"home_assistant": {"enabled": False}})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"tools": []}), encoding="utf-8")

    registry = load_registry(str(manifest_path))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "home_assistant_tool" in tool_ids


def test_disable_unknown_tool_is_noop():
    """Ignore disable calls for unknown tool ids."""
    registry = ToolRegistry()
    registry.disable("missing_tool", "reason")
    assert registry.list_specs() == []


def test_manifest_skips_missing_local_class(tmp_path, monkeypatch):
    """Skip local tools that omit module/class metadata."""
    set_config_override({"home_assistant": {"enabled": False}})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"tools": [{"tool_id": "bad_tool", "name": "Bad Tool", "module": "bad_module"}]}
        ),
        encoding="utf-8",
    )

    registry = load_registry(str(manifest_path))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "bad_tool" not in tool_ids
    assert "home_assistant_tool" in tool_ids


def test_manifest_skips_mcp_tool_when_support_missing(tmp_path, monkeypatch):
    """Skip MCP tools when MCP adapters are unavailable."""
    monkeypatch.setattr("meeseeks_core.tool_registry._load_mcp_support", lambda: None)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_id": "mcp_tool",
                        "name": "MCP Tool",
                        "description": "Test",
                        "kind": "mcp",
                        "server": "srv",
                        "tool": "ask",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = load_registry(str(manifest_path))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "mcp_tool" not in tool_ids
    assert "home_assistant_tool" in tool_ids


def test_manifest_skips_mcp_tool_missing_server(tmp_path, monkeypatch):
    """Skip MCP tools missing server/tool metadata."""
    dummy_mcp = SimpleNamespace(MCPToolRunner=object)
    monkeypatch.setattr("meeseeks_core.tool_registry._load_mcp_support", lambda: dummy_mcp)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_id": "mcp_tool",
                        "name": "MCP Tool",
                        "description": "Test",
                        "kind": "mcp",
                        "tool": "ask",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = load_registry(str(manifest_path))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "mcp_tool" not in tool_ids
    assert "home_assistant_tool" in tool_ids


def test_auto_manifest_returns_existing_when_mcp_missing(tmp_path, monkeypatch):
    """Reuse existing manifest when MCP support is unavailable."""
    manifest_path = tmp_path / "tool-manifest.auto.json"
    manifest_path.write_text("{}", encoding="utf-8")
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})
    monkeypatch.setattr("meeseeks_core.tool_registry._load_mcp_support", lambda: None)

    result = _ensure_auto_manifest(str(tmp_path / "mcp.json"))
    assert result == str(manifest_path)


def test_auto_manifest_handles_discovery_error(tmp_path, monkeypatch):
    """Keep existing manifest when MCP discovery fails."""
    manifest_path = tmp_path / "tool-manifest.auto.json"
    manifest_path.write_text("{}", encoding="utf-8")
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})

    class DummyMcpModule:
        def _load_mcp_config(self, _path):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "meeseeks_core.tool_registry._load_mcp_support",
        lambda: DummyMcpModule(),
    )

    result = _ensure_auto_manifest(str(tmp_path / "mcp.json"))
    assert result == str(manifest_path)


def test_auto_manifest_handles_write_failure(tmp_path, monkeypatch):
    """Return None when auto manifest write fails and no prior file exists."""
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})
    manifest_path = tmp_path / "tool-manifest.auto.json"

    class DummyMcpModule:
        def _load_mcp_config(self, _path):
            return {}

        def discover_mcp_tool_details_with_failures(self, _config):
            return ({}, {})

    monkeypatch.setattr(
        "meeseeks_core.tool_registry._load_mcp_support",
        lambda: DummyMcpModule(),
    )

    import builtins

    real_open = builtins.open

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == str(manifest_path) and "w" in mode:
            raise OSError("nope")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    assert _ensure_auto_manifest(str(tmp_path / "mcp.json")) is None


def test_tool_catalog_reports_registered_tools():
    """Return a minimal tool catalog for UI rendering."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Test tool",
            factory=lambda: object(),
        )
    )
    catalog = registry.tool_catalog()
    assert catalog == [
        {
            "tool_id": "dummy_tool",
            "name": "Dummy",
            "description": "Test tool",
        }
    ]


def test_default_manifest_cache_path_uses_home_fallback(tmp_path, monkeypatch):
    """Fallback to ~/.meeseeks when config dir is empty."""
    set_config_override({"runtime": {"config_dir": ""}})
    monkeypatch.setattr("meeseeks_core.tool_registry.os.path.expanduser", lambda _p: str(tmp_path))
    path = _default_manifest_cache_path()
    assert path.endswith("tool-manifest.auto.json")


def test_auto_manifest_marks_cached_tools_disabled_on_failure(tmp_path, monkeypatch):
    """Disable cached MCP tools when discovery fails globally."""
    manifest_path = tmp_path / "tool-manifest.auto.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_id": "mcp_srv_tool",
                        "name": "Tool",
                        "description": "Test",
                        "kind": "mcp",
                        "server": "srv",
                        "tool": "ask",
                    },
                    {
                        "tool_id": "mcp_bad_tool",
                        "name": "Tool",
                        "description": "Test",
                        "kind": "mcp",
                        "server": "",
                        "tool": "ask",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})

    class DummyMcpModule:
        def _load_mcp_config(self, _path):
            raise RuntimeError("boom")

    monkeypatch.setattr("meeseeks_core.tool_registry._load_mcp_support", lambda: DummyMcpModule())

    result = _ensure_auto_manifest(str(tmp_path / "mcp.json"))
    assert result == str(manifest_path)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    tool = next(item for item in payload["tools"] if item["tool_id"] == "mcp_srv_tool")
    assert tool["enabled"] is False
    assert "Discovery failed" in tool.get("disabled_reason", "")


def test_manifest_missing_path_falls_back(tmp_path):
    """Fall back to defaults when manifest path is missing."""
    registry = load_registry(str(tmp_path / "missing.json"))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "home_assistant_tool" in tool_ids


def test_manifest_builds_mcp_factory(tmp_path, monkeypatch):
    """Instantiate MCP tool runner from manifest entry."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_id": "mcp_tool",
                        "name": "MCP Tool",
                        "description": "Test",
                        "kind": "mcp",
                        "server": "srv",
                        "tool": "ask",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class DummyMCPToolRunner:
        def __init__(self, server_name: str, tool_name: str):
            self.server_name = server_name
            self.tool_name = tool_name

        def run(self, _action_step):
            return None

    dummy_module = SimpleNamespace(MCPToolRunner=DummyMCPToolRunner)
    monkeypatch.setattr("meeseeks_core.tool_registry._load_mcp_support", lambda: dummy_module)

    registry = load_registry(str(manifest_path))
    tool = registry.get("mcp_tool")
    assert isinstance(tool, DummyMCPToolRunner)
    assert tool.server_name == "srv"
    assert tool.tool_name == "ask"


def test_manifest_skips_empty_tool_id(tmp_path):
    """Skip tools with empty tool_id values."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_id": "",
                        "name": "Missing ID",
                        "description": "Missing",
                        "module": "dummy_tool",
                        "class": "DummyTool",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = load_registry(str(manifest_path))
    tool_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    assert "" not in tool_ids
    assert "home_assistant_tool" in tool_ids


def test_auto_manifest_from_mcp_config(tmp_path, monkeypatch):
    """Auto-generate a manifest when only MCP config is provided."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "url": "http://example"}}}',
        encoding="utf-8",
    )
    set_mcp_config_path(str(config_path))
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})

    manifest_path = tmp_path / "tool-manifest.auto.json"
    manifest_path.write_text("{bad json", encoding="utf-8")

    monkeypatch.setattr(
        "meeseeks_tools.integration.mcp.discover_mcp_tool_details_with_failures",
        lambda _config: (
            {"srv": [{"name": " ", "schema": None}, {"name": "tool-a", "schema": None}]},
            {},
        ),
    )

    registry = load_registry()
    tool_ids = {spec.tool_id for spec in registry.list_specs()}
    assert any(tool_id.startswith("mcp_srv_tool_a") for tool_id in tool_ids)
    manifest_path = tmp_path / "tool-manifest.auto.json"
    assert manifest_path.exists()


def test_load_registry_sets_available_tools_for_mcp(tmp_path, monkeypatch):
    """Ensure MCP tools participate in action validation."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "url": "http://example"}}}',
        encoding="utf-8",
    )
    set_mcp_config_path(str(config_path))
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})

    monkeypatch.setattr(
        "meeseeks_tools.integration.mcp.discover_mcp_tool_details_with_failures",
        lambda _config: ({"srv": [{"name": "tool-a", "schema": None}]}, {}),
    )

    registry = load_registry()
    tool_ids = {spec.tool_id for spec in registry.list_specs()}
    assert "mcp_srv_tool_a" in tool_ids

    from meeseeks_core.classes import AVAILABLE_TOOLS

    assert "mcp_srv_tool_a" in AVAILABLE_TOOLS


def test_auto_manifest_marks_failed_server(tmp_path, monkeypatch):
    """Disable cached MCP tools when discovery fails."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "url": "http://example"}}}',
        encoding="utf-8",
    )
    set_mcp_config_path(str(config_path))
    set_config_override({"runtime": {"config_dir": str(tmp_path)}})

    manifest_path = tmp_path / "tool-manifest.auto.json"
    manifest_path.write_text(json.dumps({"tools": "bad"}), encoding="utf-8")

    monkeypatch.setattr(
        "meeseeks_tools.integration.mcp.discover_mcp_tool_details_with_failures",
        lambda _config: ({}, {"srv": RuntimeError("boom")}),
    )
    monkeypatch.setattr(
        "meeseeks_core.tool_registry._build_manifest_payload",
        lambda _tools: {"tools": "bad"},
    )

    load_registry()

    manifest_path.write_text(
        json.dumps(
            {
                "tools": [
                    "bad",
                    {"name": "No id"},
                    {
                        "tool_id": "local_tool",
                        "name": "Local Tool",
                        "description": "Local",
                        "kind": "local",
                        "enabled": True,
                    },
                    {
                        "tool_id": "mcp_other_tool",
                        "name": "Other Tool",
                        "description": "Other",
                        "kind": "mcp",
                        "server": "other",
                        "tool": "tool-other",
                        "enabled": True,
                    },
                    {
                        "tool_id": "",
                        "name": "Missing ID",
                        "description": "Missing",
                        "kind": "mcp",
                        "server": "srv",
                        "tool": "tool-missing",
                        "enabled": True,
                    },
                    {
                        "tool_id": "mcp_srv_tool_a",
                        "name": "Tool A",
                        "description": "Test tool",
                        "kind": "mcp",
                        "server": "srv",
                        "tool": "tool-a",
                        "enabled": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "meeseeks_core.tool_registry._build_manifest_payload",
        lambda _tools: {"tools": [{"tool_id": "mcp_srv_tool_a"}, {"name": "bad"}, "bad"]},
    )

    registry = load_registry()
    spec = next(
        spec
        for spec in registry.list_specs(include_disabled=True)
        if spec.tool_id == "mcp_srv_tool_a"
    )
    assert spec.enabled is False
    assert "Discovery failed" in spec.metadata.get("disabled_reason", "")
