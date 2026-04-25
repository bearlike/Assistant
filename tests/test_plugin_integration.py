#!/usr/bin/env python3
"""Integration tests for plugin loading fan-out and MCP config merging."""

from __future__ import annotations

import json
from pathlib import Path


def test_plugin_components_fan_out(tmp_path):
    """Full integration: discover plugins and fan out to all registries."""
    # Create a plugin with all component types
    plugin_dir = tmp_path / "cache" / "test-mp" / "test-plugin" / "1.0.0"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "test-plugin", "description": "Integration test plugin"})
    )
    # Skill
    skill_dir = plugin_dir / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\nSkill body."
    )
    # Agent
    (plugin_dir / "agents").mkdir()
    (plugin_dir / "agents" / "test-agent.md").write_text(
        "---\nname: test-agent\ndescription: A test agent\n"
        "model: sonnet\ntools: Read Bash\n---\nYou are a test agent."
    )
    # Command
    (plugin_dir / "commands").mkdir()
    (plugin_dir / "commands" / "test-cmd.md").write_text(
        "---\ndescription: A test command\n---\nCommand body."
    )
    # Hooks
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "echo started"}]}]
                }
            }
        )
    )
    # MCP — flat format (server-name → config), no "servers" wrapper
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"test-server": {"command": "echo", "args": ["${CLAUDE_PLUGIN_ROOT}/serve"]}})
    )

    # Create registry
    registry_file = tmp_path / "installed_plugins.json"
    registry_file.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "test-plugin@test-mp": [
                        {
                            "scope": "user",
                            "installPath": str(plugin_dir),
                            "version": "1.0.0",
                        }
                    ]
                },
            }
        )
    )

    from truss_core.agent_registry import AgentRegistry, parse_agent_file
    from truss_core.hooks import HookManager, merge_plugin_hooks
    from truss_core.plugins import discover_installed_plugins
    from truss_core.skills import SkillRegistry

    plugins = discover_installed_plugins(registry_paths=[registry_file])
    assert len(plugins) == 1
    pc = plugins[0]

    # Fan out skills — skill_dirs are the skills/ parent dirs, load directly
    sr = SkillRegistry()
    seen: set[str] = set()
    for sd in pc.skill_dirs:
        if sd not in seen:
            seen.add(sd)
            sr.load_extra_dir(sd, source=f"plugin:{pc.manifest.name}")
    assert sr.get("test-skill") is not None

    # Fan out commands
    for cf in pc.command_files:
        sr.load_command_file(cf, source=f"plugin:{pc.manifest.name}")
    assert sr.get("test-cmd") is not None

    # Fan out agents
    ar = AgentRegistry()
    for af in pc.agent_files:
        agent = parse_agent_file(Path(af), source=f"plugin:{pc.manifest.name}")
        if agent:
            ar.register(agent)
    assert ar.get("test-agent") is not None
    assert ar.get("test-agent").model == "sonnet"

    # Fan out hooks
    hm = HookManager()
    if pc.hooks_config:
        merge_plugin_hooks(hm, pc.hooks_config, pc.manifest.install_path)
    assert len(hm.on_session_start) == 1

    # MCP config collected — variable substitution should have replaced ${CLAUDE_PLUGIN_ROOT}
    assert pc.mcp_config is not None
    assert "test-server" in pc.mcp_config
    assert str(plugin_dir) in pc.mcp_config["test-server"]["args"][0]


def test_get_merged_mcp_config_extra_servers(tmp_path):
    """get_merged_mcp_config should accept extra_servers at lowest priority."""
    import truss_core.config as _cfg_module
    from truss_core.config import get_merged_mcp_config, set_mcp_config_path

    # Point to an empty temp MCP config so there's no real global config to shadow extras
    empty_mcp = tmp_path / "mcp.json"
    empty_mcp.write_text("{}")
    set_mcp_config_path(empty_mcp)
    try:
        extra = {"plugin-server": {"command": "echo", "args": ["hello"]}}
        result = get_merged_mcp_config(extra_servers=extra)
        servers = result.get("servers", {})
        assert "plugin-server" in servers
    finally:
        _cfg_module._MCP_CONFIG_PATH_OVERRIDE = None
        _cfg_module._MCP_CONFIG_DISABLED = False
