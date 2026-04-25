"""Tests for PluginsConfig in config.py and plugin discovery in plugins.py."""

from __future__ import annotations

import json
from pathlib import Path

from truss_core.config import AppConfig, PluginsConfig, resolve_truss_home


def test_plugins_config_defaults():
    config = AppConfig()
    assert config.plugins.enabled is True
    assert config.plugins.enabled_plugins == []
    assert "anthropics/claude-plugins-official" in config.plugins.marketplaces
    assert config.plugins.install_path == ""


def test_plugins_config_from_dict():
    config = AppConfig(
        plugins={
            "enabled": False,
            "enabled_plugins": ["superpowers@claude-plugins-official"],
            "marketplaces": [
                "anthropics/claude-plugins-official",
                "my-org/my-plugins",
            ],
        }
    )
    assert config.plugins.enabled is False
    assert len(config.plugins.enabled_plugins) == 1
    assert len(config.plugins.marketplaces) == 2


def test_plugins_config_coerce_types():
    config = AppConfig(
        plugins={
            "enabled": "true",
            "enabled_plugins": "superpowers, feature-dev",
            "marketplaces": "anthropics/claude-plugins-official",
            "install_path": "~/custom-plugins",
        }
    )
    assert config.plugins.enabled is True
    assert config.plugins.enabled_plugins == ["superpowers", "feature-dev"]
    assert config.plugins.marketplaces == ["anthropics/claude-plugins-official"]
    assert "~" not in config.plugins.install_path


def test_plugins_resolve_install_dir():
    cfg = PluginsConfig()
    assert cfg.resolve_install_dir() == resolve_truss_home() / "plugins"


def test_plugins_resolve_install_dir_custom():
    cfg = PluginsConfig(install_path="/opt/plugins")
    assert cfg.resolve_install_dir() == Path("/opt/plugins")


# ---------------------------------------------------------------------------
# Tests for plugins.py functions
# ---------------------------------------------------------------------------

from truss_core.plugins import (  # noqa: E402
    discover_installed_plugins,
    discover_marketplace_plugins,
    discover_plugin_components,
    parse_plugin_manifest,
    substitute_plugin_vars,
)


def test_substitute_plugin_vars():
    text = "bash ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh --flag"
    result = substitute_plugin_vars(text, "/opt/plugins/test")
    assert result == "bash /opt/plugins/test/hooks/run.sh --flag"
    assert "${CLAUDE_PLUGIN_ROOT}" not in result


def test_substitute_plugin_vars_no_var():
    text = "plain text with no variable"
    assert substitute_plugin_vars(text, "/any/path") == text


def test_parse_plugin_manifest(tmp_path):
    plugin_dir = tmp_path / "test-plugin"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "test-plugin",
                "description": "A test plugin",
                "version": "1.0.0",
                "author": {"name": "Test Author"},
            }
        )
    )
    manifest = parse_plugin_manifest(plugin_dir)
    assert manifest is not None
    assert manifest.name == "test-plugin"
    assert manifest.description == "A test plugin"
    assert manifest.version == "1.0.0"
    assert manifest.author == "Test Author"


def test_parse_plugin_manifest_author_string(tmp_path):
    plugin_dir = tmp_path / "test-plugin"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "test-plugin",
                "author": "Plain Author",
            }
        )
    )
    manifest = parse_plugin_manifest(plugin_dir)
    assert manifest is not None
    assert manifest.author == "Plain Author"


def test_parse_plugin_manifest_missing_file(tmp_path):
    assert parse_plugin_manifest(tmp_path) is None


def test_parse_plugin_manifest_missing_name(tmp_path):
    plugin_dir = tmp_path / "no-name"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"description": "oops"}))
    assert parse_plugin_manifest(plugin_dir) is None


def test_parse_plugin_manifest_bad_json(tmp_path):
    plugin_dir = tmp_path / "bad-json"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text("not-json{{{")
    assert parse_plugin_manifest(plugin_dir) is None


def test_parse_plugin_manifest_minimal(tmp_path):
    plugin_dir = tmp_path / "minimal"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "minimal"}))
    manifest = parse_plugin_manifest(plugin_dir)
    assert manifest is not None
    assert manifest.name == "minimal"


def test_discover_plugin_components(tmp_path):
    plugin_dir = tmp_path / "my-plugin"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "my-plugin", "description": "test"})
    )
    # Skill
    skill_dir = plugin_dir / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: test\n---\nBody")
    # Agent
    (plugin_dir / "agents").mkdir()
    (plugin_dir / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: test\n---\nBody"
    )
    # Command
    (plugin_dir / "commands").mkdir()
    (plugin_dir / "commands" / "deploy.md").write_text("---\ndescription: Deploy\n---\nBody")
    # MCP — Claude Code style (no top-level "servers" key)
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {"test-server": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"]}}
        )
    )
    # Hooks
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        )
    )

    components = discover_plugin_components(plugin_dir)
    assert components.manifest.name == "my-plugin"
    assert len(components.skill_dirs) == 1
    assert len(components.agent_files) == 1
    assert len(components.command_files) == 1
    assert components.mcp_config is not None
    # Variable substitution should have been applied
    assert str(plugin_dir) in components.mcp_config["test-server"]["args"][0]
    assert "${CLAUDE_PLUGIN_ROOT}" not in str(components.mcp_config)
    assert components.hooks_config is not None


def test_discover_plugin_components_mcp_native_format(tmp_path):
    """Test that Truss native MCP format (with top-level 'servers') is handled."""
    plugin_dir = tmp_path / "native-mcp"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "native-mcp"}))
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "my-server": {"command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}
                }
            }
        )
    )
    components = discover_plugin_components(plugin_dir)
    assert components.mcp_config is not None
    # Native format should preserve the "servers" key
    assert "servers" in components.mcp_config
    assert str(plugin_dir) in components.mcp_config["servers"]["my-server"]["args"][0]


def test_discover_plugin_components_no_manifest(tmp_path):
    """discover_plugin_components still works even with no manifest."""
    plugin_dir = tmp_path / "no-manifest"
    plugin_dir.mkdir()
    components = discover_plugin_components(plugin_dir)
    # manifest will be None since there's no plugin.json
    assert components.manifest is None
    assert components.skill_dirs == []
    assert components.mcp_config is None


def test_discover_plugin_components_skill_without_skill_md(tmp_path):
    """Skill dirs without SKILL.md should not appear in skill_dirs."""
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "plugin"}))
    (plugin_dir / "skills" / "no-skill-md").mkdir(parents=True)
    components = discover_plugin_components(plugin_dir)
    assert components.skill_dirs == []


def test_discover_installed_plugins(tmp_path):
    plugin_dir = tmp_path / "cache" / "test-mp" / "my-plugin" / "1.0.0"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "my-plugin", "description": "Test"})
    )
    registry_file = tmp_path / "installed_plugins.json"
    registry_file.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "my-plugin@test-mp": [
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
    plugins = discover_installed_plugins(registry_paths=[registry_file])
    assert len(plugins) == 1
    assert plugins[0].manifest.name == "my-plugin"
    assert plugins[0].manifest.marketplace == "test-mp"


def test_discover_installed_plugins_scope_set(tmp_path):
    plugin_dir = tmp_path / "p" / "1.0"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "p"}))
    registry_file = tmp_path / "installed_plugins.json"
    registry_file.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "p@mp": [{"scope": "project", "installPath": str(plugin_dir), "version": "1.0"}]
                },
            }
        )
    )
    plugins = discover_installed_plugins(registry_paths=[registry_file])
    assert plugins[0].manifest.scope == "project"


def test_discover_installed_plugins_enabled_filter(tmp_path):
    # Create two plugins
    for name in ["plugin-a", "plugin-b"]:
        d = tmp_path / "cache" / "mp" / name / "1.0"
        d.mkdir(parents=True)
        (d / ".claude-plugin").mkdir()
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))

    path_a = str(tmp_path / "cache/mp/plugin-a/1.0")
    path_b = str(tmp_path / "cache/mp/plugin-b/1.0")
    registry_file = tmp_path / "installed_plugins.json"
    registry_file.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "plugin-a@mp": [{"scope": "user", "installPath": path_a, "version": "1.0"}],
                    "plugin-b@mp": [{"scope": "user", "installPath": path_b, "version": "1.0"}],
                },
            }
        )
    )
    plugins = discover_installed_plugins(registry_paths=[registry_file], enabled=["plugin-a"])
    assert len(plugins) == 1
    assert plugins[0].manifest.name == "plugin-a"


def test_discover_installed_plugins_dedup_first_wins(tmp_path):
    """First registry path has priority when the same plugin name appears in two registries."""
    for i, name in enumerate(["1.0", "2.0"]):
        d = tmp_path / f"reg{i}" / "mp" / "p" / name
        d.mkdir(parents=True)
        (d / ".claude-plugin").mkdir()
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "p", "version": name})
        )

    p1 = str(tmp_path / "reg0/mp/p/1.0")
    p2 = str(tmp_path / "reg1/mp/p/2.0")
    reg1 = tmp_path / "reg1_installed_plugins.json"
    reg1.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {"p@mp": [{"scope": "user", "installPath": p1, "version": "1.0"}]},
            }
        )
    )
    reg2 = tmp_path / "reg2_installed_plugins.json"
    reg2.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {"p@mp": [{"scope": "user", "installPath": p2, "version": "2.0"}]},
            }
        )
    )
    plugins = discover_installed_plugins(registry_paths=[reg1, reg2])
    assert len(plugins) == 1
    assert plugins[0].manifest.version == "1.0"


def test_discover_installed_plugins_missing_path(tmp_path):
    """Entries with non-existent installPath are silently skipped."""
    missing = str(tmp_path / "does/not/exist")
    registry_file = tmp_path / "installed_plugins.json"
    registry_file.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "ghost@mp": [{"scope": "user", "installPath": missing, "version": "1.0"}]
                },
            }
        )
    )
    plugins = discover_installed_plugins(registry_paths=[registry_file])
    assert plugins == []


def test_discover_marketplace_plugins(tmp_path):
    mp_dir = tmp_path / "marketplace"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "test-marketplace",
                "plugins": [
                    {
                        "name": "plugin-a",
                        "description": "First",
                        "source": "./plugins/a",
                        "category": "dev",
                    },
                    {
                        "name": "plugin-b",
                        "description": "Second",
                        "source": {"source": "github", "repo": "org/repo"},
                    },
                ],
            }
        )
    )
    available = discover_marketplace_plugins(marketplace_dirs=[mp_dir])
    assert len(available) == 2
    assert available[0]["name"] == "plugin-a"
    assert available[0]["marketplace"] == "test-marketplace"
    assert available[0]["category"] == "dev"
    assert available[1]["category"] == ""


def test_discover_marketplace_plugins_missing_dir(tmp_path):
    """Missing marketplace.json is silently skipped."""
    result = discover_marketplace_plugins(marketplace_dirs=[tmp_path / "nonexistent"])
    assert result == []


def test_discover_marketplace_plugins_installed_false(tmp_path):
    mp_dir = tmp_path / "mp"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "mp", "plugins": [{"name": "x", "description": "y"}]})
    )
    available = discover_marketplace_plugins(marketplace_dirs=[mp_dir])
    assert available[0]["installed"] is False


# ---------------------------------------------------------------------------
# Tests for SkillRegistry plugin extensions
# ---------------------------------------------------------------------------

from truss_core.skills import SkillRegistry, SkillSpec  # noqa: E402


def test_skill_registry_load_extra_dir(tmp_path):
    skill_dir = tmp_path / "skills" / "helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: helper\ndescription: Helps\n---\nHelp body.")
    registry = SkillRegistry()
    registry.load_extra_dir(str(tmp_path / "skills"), source="plugin:test")
    skill = registry.get("helper")
    assert skill is not None
    assert skill.source == "plugin:test"


def test_skill_registry_load_extra_dir_no_override(tmp_path):
    """Plugin skills should NOT override existing project/personal skills."""
    skill_dir = tmp_path / "skills" / "existing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: existing\ndescription: Plugin version\n---\nPlugin body."
    )
    registry = SkillRegistry()
    # Pre-populate with a "personal" skill
    registry._skills["existing"] = SkillSpec(
        name="existing",
        description="Personal version",
        source_path="/personal",
        source="personal",
        body="Personal body.",
    )
    registry.load_extra_dir(str(tmp_path / "skills"), source="plugin:test")
    assert registry.get("existing").description == "Personal version"


def test_skill_registry_load_command_file(tmp_path):
    cmd = tmp_path / "deploy.md"
    cmd.write_text("---\ndescription: Deploy the app\n---\nDeploy instructions.")
    registry = SkillRegistry()
    registry.load_command_file(str(cmd), source="plugin:test")
    skill = registry.get("deploy")
    assert skill is not None
    assert skill.description == "Deploy the app"
    assert "Deploy instructions" in skill.body


def test_skill_registry_load_command_file_with_name(tmp_path):
    """Commands with explicit 'name' in frontmatter use that name."""
    cmd = tmp_path / "my-cmd.md"
    cmd.write_text("---\nname: custom-name\ndescription: Custom\n---\nBody.")
    registry = SkillRegistry()
    registry.load_command_file(str(cmd), source="plugin:test")
    assert registry.get("custom-name") is not None
    assert registry.get("my-cmd") is None
