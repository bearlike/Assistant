"""Extra tests for mewbo_core/plugins.py — covers the missing lines
identified in the coverage gap analysis (lines 281-292, 347-363, 489-495,
551-577, 602-686, 700-717, 732-768, 811-835, 863-877).

Stubs: subprocess (git) only. No real network calls.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.plugins import (
    discover_builtin_plugins,
    discover_installed_plugins,
    discover_marketplace_plugins,
    discover_plugin_components,
    install_plugin,
    load_all_plugin_components,
    marketplace_dir_name,
    register_builtin_root,
    sync_marketplaces,
    uninstall_plugin,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plugin_dir(
    base: Path,
    name: str,
    *,
    extra: dict | None = None,
    with_mcp: dict | None = None,
    with_hooks: dict | None = None,
    with_skills: list[str] | None = None,
    session_tools: list[dict] | None = None,
) -> Path:
    """Scaffold a minimal plugin directory."""
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin").mkdir(exist_ok=True)
    manifest = {"name": name, **(extra or {})}
    if session_tools is not None:
        manifest["session_tools"] = session_tools
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    if with_mcp is not None:
        (plugin_dir / ".mcp.json").write_text(json.dumps(with_mcp), encoding="utf-8")
    if with_hooks is not None:
        (plugin_dir / "hooks").mkdir(exist_ok=True)
        (plugin_dir / "hooks" / "hooks.json").write_text(json.dumps(with_hooks), encoding="utf-8")
    if with_skills:
        for skill_name in with_skills:
            sd = plugin_dir / "skills" / skill_name
            sd.mkdir(parents=True)
            (sd / "SKILL.md").write_text(
                f"---\nname: {skill_name}\ndescription: skill {skill_name}\n---\nbody"
            )
    return plugin_dir


def _make_registry(
    base: Path, plugins: dict[str, str], *, registry_name: str = "installed_plugins.json"
) -> Path:
    """Write a minimal installed_plugins.json registry and return its path."""
    reg = {
        "version": 2,
        "plugins": {
            f"{name}@mp": [
                {"scope": "user", "installPath": str(Path(install_path)), "version": "1.0"}
            ]
            for name, install_path in plugins.items()
        },
    }
    reg_path = base / registry_name
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    return reg_path


# ---------------------------------------------------------------------------
# discover_plugin_components — MCP bad JSON path (line 281-282)
# ---------------------------------------------------------------------------


def test_discover_components_mcp_bad_json_logged(tmp_path: Path) -> None:
    """A corrupt .mcp.json is skipped with a warning; other components still load."""
    plugin_dir = _make_plugin_dir(tmp_path, "bad-mcp")
    (plugin_dir / ".mcp.json").write_text("{not valid json", encoding="utf-8")
    components = discover_plugin_components(plugin_dir)
    assert components.mcp_config is None
    # Other component discovery still succeeds.
    assert components.manifest is not None
    assert components.manifest.name == "bad-mcp"


# discover_plugin_components — hooks bad JSON path (line 290-291)


def test_discover_components_hooks_bad_json_logged(tmp_path: Path) -> None:
    """A corrupt hooks/hooks.json is skipped; rest of components loads."""
    plugin_dir = _make_plugin_dir(tmp_path, "bad-hooks")
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text("{not json", encoding="utf-8")
    components = discover_plugin_components(plugin_dir)
    assert components.hooks_config is None
    assert components.manifest is not None


# discover_plugin_components — session_tools (lines 297-301)


def test_discover_components_session_tools_extracted(tmp_path: Path) -> None:
    """session_tools entries from plugin.json are surfaced in PluginComponents."""
    entries = [{"tool_id": "my-tool", "module": "my.module", "class": "MyClass"}]
    plugin_dir = _make_plugin_dir(tmp_path, "tools-plugin", session_tools=entries)
    components = discover_plugin_components(plugin_dir)
    assert components.session_tool_entries == entries


def test_discover_components_session_tools_ignores_non_dict_entries(tmp_path: Path) -> None:
    """Non-dict entries in session_tools are filtered out."""
    plugin_dir = _make_plugin_dir(tmp_path, "tools-mixed")
    # Rewrite manifest to include a mixed list
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tools-mixed", "session_tools": [{"tool_id": "ok"}, "not-a-dict", 42]})
    )
    components = discover_plugin_components(plugin_dir)
    assert components.session_tool_entries == [{"tool_id": "ok"}]


def test_discover_components_session_tools_empty_when_no_manifest(tmp_path: Path) -> None:
    """No manifest → empty session_tool_entries."""
    plugin_dir = tmp_path / "no-manifest"
    plugin_dir.mkdir()
    components = discover_plugin_components(plugin_dir)
    assert components.session_tool_entries == []


# ---------------------------------------------------------------------------
# discover_installed_plugins — stale cache / no manifest (lines 347-399)
# ---------------------------------------------------------------------------


def test_discover_installed_plugins_stale_cache_skipped(tmp_path: Path) -> None:
    """An entry whose installPath exists but lacks plugin.json is silently skipped."""
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    # No .claude-plugin/plugin.json
    reg = _make_registry(tmp_path, {"stale-plugin": str(stale_dir)})
    result = discover_installed_plugins(registry_paths=[reg])
    assert result == []


def test_discover_installed_plugins_registry_bad_json(tmp_path: Path) -> None:
    """A corrupt registry file is skipped and discovery continues."""
    bad_reg = tmp_path / "installed_plugins.json"
    bad_reg.write_text("{bad json", encoding="utf-8")
    result = discover_installed_plugins(registry_paths=[bad_reg])
    assert result == []


def test_discover_installed_plugins_empty_entries_list_skipped(tmp_path: Path) -> None:
    """An entry with an empty installations list is skipped (no IndexError)."""
    reg_data = {"version": 2, "plugins": {"ghost@mp": []}}
    reg_path = tmp_path / "installed_plugins.json"
    reg_path.write_text(json.dumps(reg_data))
    result = discover_installed_plugins(registry_paths=[reg_path])
    assert result == []


def test_discover_installed_plugins_no_at_sign_in_key(tmp_path: Path) -> None:
    """Registry keys without '@' still work — marketplace defaults to empty string."""
    plugin_dir = _make_plugin_dir(tmp_path, "bare-plugin")
    reg_data = {
        "version": 2,
        "plugins": {
            "bare-plugin": [{"scope": "user", "installPath": str(plugin_dir), "version": "1.0"}]
        },
    }
    reg_path = tmp_path / "installed_plugins.json"
    reg_path.write_text(json.dumps(reg_data))
    result = discover_installed_plugins(registry_paths=[reg_path])
    assert len(result) == 1
    assert result[0].manifest.marketplace == ""


def test_discover_installed_plugins_enabled_filter_excludes(tmp_path: Path) -> None:
    """Plugins not in the ``enabled`` list are excluded even when installPath is valid."""
    plugin_dir = _make_plugin_dir(tmp_path, "unwanted")
    reg = _make_registry(tmp_path, {"unwanted": str(plugin_dir)})
    result = discover_installed_plugins(registry_paths=[reg], enabled=["something-else"])
    assert result == []


# ---------------------------------------------------------------------------
# discover_builtin_plugins (lines 489-495)
# ---------------------------------------------------------------------------


def test_discover_builtin_plugins_skips_nondir(tmp_path: Path) -> None:
    """Files (not dirs) inside the root are silently skipped."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").write_text("hi")  # a file, not a dir
    result = discover_builtin_plugins(root)
    assert result == []


def test_discover_builtin_plugins_skips_dir_without_manifest(tmp_path: Path) -> None:
    """A subdir with no .claude-plugin/plugin.json is skipped."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "incomplete-plugin").mkdir()
    result = discover_builtin_plugins(root)
    assert result == []


def test_discover_builtin_plugins_marks_scope_built_in(tmp_path: Path) -> None:
    """Valid built-in plugins get scope='built-in' and marketplace='built-in'."""
    root = tmp_path / "root"
    root.mkdir()
    _make_plugin_dir(root, "my-builtin")
    result = discover_builtin_plugins(root)
    assert len(result) == 1
    assert result[0].manifest.scope == "built-in"
    assert result[0].manifest.marketplace == "built-in"


def test_discover_builtin_plugins_nonexistent_root(tmp_path: Path) -> None:
    """A non-existent root returns an empty list without error."""
    result = discover_builtin_plugins(tmp_path / "does-not-exist")
    assert result == []


# ---------------------------------------------------------------------------
# _resolve_builtin_root / _all_builtin_roots / register_builtin_root
# ---------------------------------------------------------------------------


def test_resolve_builtin_root_falls_back_to_empty_on_error(monkeypatch) -> None:
    """When importlib.resources fails, _resolve_builtin_root returns Path('') (non-existent)."""
    import mewbo_core.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_BUILTIN_ROOT_OVERRIDE", None)
    with patch("importlib.resources.files", side_effect=OSError("boom")):
        result = plugins_mod._resolve_builtin_root()
    # Path("") resolves to "." or "" — in either case it won't be a real dir
    assert not result.is_dir() or str(result) in ("", ".")


def test_all_builtin_roots_uses_override(tmp_path: Path, monkeypatch) -> None:
    """_BUILTIN_ROOT_OVERRIDE short-circuits _all_builtin_roots to just that path."""
    import mewbo_core.plugins as plugins_mod

    override = tmp_path / "custom"
    override.mkdir()
    monkeypatch.setattr(plugins_mod, "_BUILTIN_ROOT_OVERRIDE", override)
    roots = plugins_mod._all_builtin_roots()
    assert roots == [override]


def test_register_builtin_root_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Registering the same root twice is a no-op."""
    import mewbo_core.plugins as plugins_mod

    # Patch the module-level list to avoid polluting other tests
    monkeypatch.setattr(plugins_mod, "_BUILTIN_ROOT_OVERRIDE", None)
    original_extras = list(plugins_mod._EXTRA_BUILTIN_ROOTS)
    try:
        new_root = tmp_path / "extra"
        new_root.mkdir()
        register_builtin_root(new_root)
        register_builtin_root(new_root)  # second call is no-op
        count = sum(1 for r in plugins_mod._EXTRA_BUILTIN_ROOTS if r == new_root)
        assert count == 1
    finally:
        # Restore original state
        plugins_mod._EXTRA_BUILTIN_ROOTS[:] = original_extras


# ---------------------------------------------------------------------------
# sync_marketplaces — error paths (lines 551-577)
# ---------------------------------------------------------------------------


def test_sync_marketplaces_clone_failure_logged(tmp_path: Path, monkeypatch) -> None:
    """A failed git clone is silently skipped — no exception propagated."""

    def _fail_clone(cmd, **kwargs):
        raise subprocess.CalledProcessError(128, cmd, stderr="auth error")

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _fail_clone)
    dirs = sync_marketplaces(["org/plugins"], tmp_path)
    assert dirs == []


def test_sync_marketplaces_existing_dir_no_marker_tries_pull(tmp_path: Path, monkeypatch) -> None:
    """Dir exists + has .git but no marketplace.json → tries git pull."""
    entry = "org/plugins"
    mp_dir = tmp_path / "marketplaces" / marketplace_dir_name(entry)
    mp_dir.mkdir(parents=True)
    (mp_dir / ".git").mkdir()  # simulate a real git repo

    calls: list[list] = []

    def _record(cmd, **kwargs):
        calls.append(cmd)
        # Don't create marketplace.json, so the marker won't be found
        return MagicMock(returncode=0)

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _record)
    dirs = sync_marketplaces([entry], tmp_path)
    # A pull was attempted
    assert any("pull" in " ".join(cmd) for cmd in calls)
    # Dir has no marker, so it is NOT appended to the result
    assert dirs == []


def test_sync_marketplaces_existing_dir_no_marker_pull_creates_marker(
    tmp_path: Path, monkeypatch
) -> None:
    """After a successful pull the marketplace.json marker is detected."""
    entry = "org/plugins"
    mp_dir = tmp_path / "marketplaces" / marketplace_dir_name(entry)
    mp_dir.mkdir(parents=True)
    (mp_dir / ".git").mkdir()

    def _create_marker(cmd, **kwargs):
        # Simulate pull creating the marketplace.json
        (mp_dir / ".claude-plugin").mkdir(exist_ok=True)
        (mp_dir / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "mp", "plugins": []})
        )
        return MagicMock(returncode=0)

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _create_marker)
    dirs = sync_marketplaces([entry], tmp_path)
    assert len(dirs) == 1


def test_sync_marketplaces_pull_failure_logged(tmp_path: Path, monkeypatch) -> None:
    """A pull failure is logged but not raised."""
    entry = "org/plugins"
    mp_dir = tmp_path / "marketplaces" / marketplace_dir_name(entry)
    mp_dir.mkdir(parents=True)
    (mp_dir / ".git").mkdir()

    def _fail_pull(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _fail_pull)
    # Must not raise
    dirs = sync_marketplaces([entry], tmp_path)
    assert dirs == []


# ---------------------------------------------------------------------------
# discover_marketplace_plugins — error paths (lines 602-604)
# ---------------------------------------------------------------------------


def test_discover_marketplace_plugins_bad_json(tmp_path: Path) -> None:
    """Corrupt marketplace.json is skipped, no exception."""
    mp_dir = tmp_path / "mp"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text("{bad", encoding="utf-8")
    result = discover_marketplace_plugins([mp_dir])
    assert result == []


# ---------------------------------------------------------------------------
# install_plugin — various source types (lines 647-765)
# ---------------------------------------------------------------------------


def _setup_marketplace(tmp_path: Path, plugins: list[dict]) -> tuple[Path, Path]:
    """Create a marketplace dir + install_base for install_plugin tests."""
    mp_dir = tmp_path / "mp"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "test-mp", "plugins": plugins})
    )
    install_base = tmp_path / "install"
    install_base.mkdir()
    return mp_dir, install_base


def test_install_plugin_not_found_raises(tmp_path: Path) -> None:
    """Requesting an unknown plugin raises ValueError."""
    mp_dir, install_base = _setup_marketplace(tmp_path, [])
    with pytest.raises(ValueError, match="not found in marketplace"):
        install_plugin("ghost", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)


def test_install_plugin_local_path_traversal_rejected(tmp_path: Path) -> None:
    """A local source that escapes the marketplace directory is rejected."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "p", "version": "1.0", "source": "./../../../evil"}],
    )
    with pytest.raises(ValueError, match="escapes marketplace"):
        install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)


def test_install_plugin_local_path_copies(tmp_path: Path) -> None:
    """A local (./relative) source is copied into the cache dir."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "local-p", "version": "1.0", "source": "./local-p"}],
    )
    # Create the local plugin inside the marketplace
    src = mp_dir / "local-p"
    src.mkdir()
    (src / ".claude-plugin").mkdir()
    (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "local-p"}))

    manifest = install_plugin(
        "local-p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base
    )
    assert manifest.name == "local-p"


def test_install_plugin_dict_source_url_field(tmp_path: Path, monkeypatch) -> None:
    """A dict source with 'url' field is cloned from that URL."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [
            {
                "name": "p",
                "version": "1.0",
                "source": {"source": "url", "url": "https://git.example.com/p.git"},
            }
        ],
    )
    calls: list[list] = []

    def _fake_clone(cmd, **kw):
        dest = Path(cmd[-1])
        (dest / ".claude-plugin").mkdir(parents=True)
        (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "p"}))
        calls.append(cmd)

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _fake_clone)
    manifest = install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)
    assert manifest.name == "p"
    assert "https://git.example.com/p.git" in calls[0]


def test_install_plugin_dict_source_missing_repo_and_url(tmp_path: Path) -> None:
    """A dict source with neither 'repo' nor 'url' raises ValueError."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "p", "version": "1.0", "source": {"source": "unknown"}}],
    )
    with pytest.raises(ValueError, match="no 'repo' or 'url'"):
        install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)


def test_install_plugin_unsupported_source_type(tmp_path: Path) -> None:
    """A non-string, non-dict source raises ValueError."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "p", "version": "1.0", "source": 42}],
    )
    with pytest.raises(ValueError, match="Unsupported plugin source"):
        install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)


def test_install_plugin_missing_manifest_after_install(tmp_path: Path, monkeypatch) -> None:
    """If git clone succeeds but plugin.json is absent, RuntimeError is raised."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "p", "version": "1.0", "source": {"repo": "owner/p"}}],
    )

    def _fake_clone_no_manifest(cmd, **kw):
        # Create the destination dir but NO plugin.json
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _fake_clone_no_manifest)
    with pytest.raises(RuntimeError, match="missing a valid plugin.json"):
        install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)


def test_install_plugin_skips_already_cloned_git_dir(tmp_path: Path, monkeypatch) -> None:
    """When the cache dir already has .git, the clone is skipped."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "p", "version": "1.0", "source": {"repo": "owner/p"}}],
    )
    # Pre-create the destination with a .git dir and manifest
    from mewbo_core.plugins import _sanitize_path_component

    cache_dir = (
        install_base
        / "cache"
        / _sanitize_path_component("test-mp")
        / _sanitize_path_component("p")
        / _sanitize_path_component("1.0")
    )
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    (cache_dir / ".claude-plugin").mkdir()
    (cache_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "p"}))

    calls: list = []
    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", lambda *a, **kw: calls.append(a))

    manifest = install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)
    assert manifest.name == "p"
    assert calls == []  # No git clone invoked


def test_install_plugin_updates_registry(tmp_path: Path, monkeypatch) -> None:
    """install_plugin writes the plugin entry into installed_plugins.json."""
    mp_dir, install_base = _setup_marketplace(
        tmp_path,
        [{"name": "p", "version": "2.0", "source": {"repo": "owner/p"}}],
    )

    def _fake_clone(cmd, **kw):
        dest = Path(cmd[-1])
        (dest / ".claude-plugin").mkdir(parents=True)
        (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "p"}))

    monkeypatch.setattr("mewbo_core.plugins.subprocess.run", _fake_clone)
    install_plugin("p", "test-mp", marketplace_dirs=[mp_dir], install_base=install_base)

    reg_path = install_base / "installed_plugins.json"
    assert reg_path.is_file()
    reg = json.loads(reg_path.read_text())
    assert "p@test-mp" in reg["plugins"]


# ---------------------------------------------------------------------------
# uninstall_plugin (lines 741-768)
# ---------------------------------------------------------------------------


def test_uninstall_plugin_returns_false_no_registry(tmp_path: Path) -> None:
    """No registry file → uninstall returns False."""
    result = uninstall_plugin("x", install_base=tmp_path)
    assert result is False


def test_uninstall_plugin_returns_false_not_found(tmp_path: Path) -> None:
    """Plugin not in registry → returns False."""
    reg_path = tmp_path / "installed_plugins.json"
    reg_path.write_text(json.dumps({"version": 2, "plugins": {}}))
    result = uninstall_plugin("ghost", install_base=tmp_path)
    assert result is False


def test_uninstall_plugin_removes_cache_dir(tmp_path: Path) -> None:
    """Successful uninstall removes the cache directory and returns True."""
    cache = tmp_path / "cache" / "mp" / "my-plugin" / "1.0"
    cache.mkdir(parents=True)
    reg_path = tmp_path / "installed_plugins.json"
    reg_path.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "my-plugin@mp": [{"scope": "user", "installPath": str(cache), "version": "1.0"}]
                },
            }
        )
    )
    result = uninstall_plugin("my-plugin", install_base=tmp_path)
    assert result is True
    assert not cache.exists()
    reg = json.loads(reg_path.read_text())
    assert "my-plugin@mp" not in reg["plugins"]


def test_uninstall_plugin_missing_cache_dir_still_returns_true(tmp_path: Path) -> None:
    """uninstall returns True even if the cache dir was already gone."""
    reg_path = tmp_path / "installed_plugins.json"
    reg_path.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "gone@mp": [
                        {
                            "scope": "user",
                            "installPath": str(tmp_path / "not-here"),
                            "version": "1.0",
                        }
                    ]
                },
            }
        )
    )
    result = uninstall_plugin("gone", install_base=tmp_path)
    assert result is True


def test_uninstall_plugin_bad_registry_json(tmp_path: Path) -> None:
    """Corrupt registry → returns False without raising."""
    reg_path = tmp_path / "installed_plugins.json"
    reg_path.write_text("{bad json")
    result = uninstall_plugin("x", install_base=tmp_path)
    assert result is False


# ---------------------------------------------------------------------------
# load_all_plugin_components — cache + disabled path (lines 811-835)
# ---------------------------------------------------------------------------


def test_load_all_plugin_components_disabled(monkeypatch) -> None:
    """When plugins.enabled is False, returns an empty PluginFanOut."""
    import mewbo_core.plugins as plugins_mod
    from mewbo_core.config import reset_config, set_config_override

    set_config_override({"plugins": {"enabled": False}})
    # Reset the module-level cache so it's not served stale
    monkeypatch.setattr(plugins_mod, "_fanout_cache", None)
    try:
        fanout = load_all_plugin_components()
        assert fanout.components == []
        assert fanout.skill_dirs == []
        assert fanout.mcp_servers == {}
    finally:
        reset_config()


def test_load_all_plugin_components_cache_hit(tmp_path: Path, monkeypatch) -> None:
    """A second call returns the cached PluginFanOut when registry hasn't changed."""
    import mewbo_core.plugins as plugins_mod
    from mewbo_core.config import reset_config, set_config_override

    set_config_override(
        {
            "plugins": {
                "enabled": True,
                "install_path": str(tmp_path / "plugins"),
            }
        }
    )
    monkeypatch.setattr(plugins_mod, "_fanout_cache", None)
    monkeypatch.setattr(plugins_mod, "_fanout_cache_mtime", 0.0)
    monkeypatch.setattr(plugins_mod, "_BUILTIN_ROOT_OVERRIDE", tmp_path / "builtins")
    (tmp_path / "builtins").mkdir()

    try:
        first = load_all_plugin_components()
        second = load_all_plugin_components()
        assert first is second  # exact same object — cache hit
    finally:
        reset_config()
        plugins_mod._fanout_cache = None


def test_load_all_plugin_components_mcp_normalization(tmp_path: Path, monkeypatch) -> None:
    """mcpServers top-level key is renamed to 'servers' during fan-out."""
    import mewbo_core.plugins as plugins_mod
    from mewbo_core.config import reset_config, set_config_override

    # Build a plugin with mcpServers format
    _make_plugin_dir(
        tmp_path / "builtins",
        "mcp-plugin",
        with_mcp={"mcpServers": {"my-server": {"command": "node", "args": []}}},
    )
    set_config_override(
        {
            "plugins": {
                "enabled": True,
                "install_path": str(tmp_path / "plugins"),
            }
        }
    )
    monkeypatch.setattr(plugins_mod, "_fanout_cache", None)
    monkeypatch.setattr(plugins_mod, "_fanout_cache_mtime", 0.0)
    monkeypatch.setattr(plugins_mod, "_BUILTIN_ROOT_OVERRIDE", tmp_path / "builtins")

    try:
        fanout = load_all_plugin_components()
        assert "my-server" in fanout.mcp_servers
    finally:
        reset_config()
        plugins_mod._fanout_cache = None
