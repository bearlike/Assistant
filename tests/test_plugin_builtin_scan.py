"""Tests for the built-in plugin scan path.

Task 5 of the widget-builder-as-plugin refactor: first-party plugins shipped
inside the ``mewbo_core.builtin_plugins`` package directory are discovered
automatically, without an ``installed_plugins.json`` entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mewbo_core import plugins as plugins_module
from mewbo_core.plugins import (
    PluginFanOut,
    discover_builtin_plugins,
    load_all_plugin_components,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_builtin_plugin(root: Path, name: str) -> Path:
    """Create a minimal built-in plugin under *root* and return its directory."""
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": f"Built-in plugin {name}",
                "version": "1.0.0",
            }
        )
    )
    return plugin_dir


def _reset_fanout_cache() -> None:
    """Clear the module-level fan-out cache between tests."""
    plugins_module._fanout_cache = None
    plugins_module._fanout_cache_mtime = 0.0


# ---------------------------------------------------------------------------
# discover_builtin_plugins — direct unit tests
# ---------------------------------------------------------------------------


def test_discover_builtin_plugins_nonexistent_root(tmp_path):
    """A path that does not exist returns an empty list (no error)."""
    assert discover_builtin_plugins(tmp_path / "does-not-exist") == []


def test_discover_builtin_plugins_empty_directory(tmp_path):
    """A present but empty directory yields no components."""
    assert discover_builtin_plugins(tmp_path) == []


def test_discover_builtin_plugins_single_plugin(tmp_path):
    """A directory with one built-in plugin yields exactly one PluginComponents."""
    _write_builtin_plugin(tmp_path, "fake-plugin")
    components = discover_builtin_plugins(tmp_path)
    assert len(components) == 1

    manifest = components[0].manifest
    assert manifest is not None
    assert manifest.name == "fake-plugin"
    # The built-in scope/marketplace labels are the contract operators
    # rely on to tell these apart from user-installed plugins in listings.
    assert manifest.scope == "built-in"
    assert manifest.marketplace == "built-in"


def test_discover_builtin_plugins_skips_non_directories_and_stray_files(tmp_path):
    """Stray files and dirs without a manifest must be skipped silently."""
    # Stray file at the top level — not a directory.
    (tmp_path / "README.md").write_text("not a plugin")
    # Directory without a .claude-plugin/plugin.json manifest.
    (tmp_path / "not-a-plugin").mkdir()
    (tmp_path / "not-a-plugin" / "random.txt").write_text("no manifest here")

    assert discover_builtin_plugins(tmp_path) == []


def test_discover_builtin_plugins_sorted_order(tmp_path):
    """Results are returned in sorted directory order (deterministic)."""
    _write_builtin_plugin(tmp_path, "zzz-last")
    _write_builtin_plugin(tmp_path, "aaa-first")
    _write_builtin_plugin(tmp_path, "mmm-middle")

    components = discover_builtin_plugins(tmp_path)
    names = [pc.manifest.name for pc in components if pc.manifest is not None]
    assert names == ["aaa-first", "mmm-middle", "zzz-last"]


# ---------------------------------------------------------------------------
# load_all_plugin_components — integration via the _BUILTIN_ROOT_OVERRIDE seam
# ---------------------------------------------------------------------------


@pytest.fixture
def builtin_root_seam(tmp_path, monkeypatch):
    """Provide a fresh tmp dir as the built-in plugin root for one test.

    Resets the fan-out cache before and after so each test sees a clean
    discovery pass. Uses the ``_BUILTIN_ROOT_OVERRIDE`` module-level seam
    rather than monkeypatching :func:`importlib.resources.files`, which is
    brittle across Python versions / wheel layouts.
    """
    _reset_fanout_cache()
    root = tmp_path / "builtin_root"
    root.mkdir()
    monkeypatch.setattr(plugins_module, "_BUILTIN_ROOT_OVERRIDE", root)
    yield root
    _reset_fanout_cache()


def test_load_all_plugin_components_picks_up_builtin(builtin_root_seam, monkeypatch):
    """A plugin under the built-in root shows up in the fan-out."""
    _write_builtin_plugin(builtin_root_seam, "fake-builtin")

    # Stub out installed-plugin discovery so registry lookups don't leak the
    # real user environment into the test.
    monkeypatch.setattr(
        plugins_module, "discover_installed_plugins", lambda **_kwargs: []
    )

    fanout: PluginFanOut = load_all_plugin_components()
    names = [pc.manifest.name for pc in fanout.components if pc.manifest is not None]
    assert "fake-builtin" in names

    # The manifest we got back should be the built-in-stamped one.
    builtin = next(
        pc
        for pc in fanout.components
        if pc.manifest and pc.manifest.name == "fake-builtin"
    )
    assert builtin.manifest.scope == "built-in"
    assert builtin.manifest.marketplace == "built-in"


def test_load_all_plugin_components_builtin_before_installed(
    builtin_root_seam, monkeypatch
):
    """Built-in components are prepended so they win dedup races downstream."""
    _write_builtin_plugin(builtin_root_seam, "shared-name")

    # Fake installed plugin with the same name — should appear AFTER the
    # built-in in the ordered components list.
    from mewbo_core.plugins import PluginComponents, PluginManifest

    installed = PluginComponents(
        manifest=PluginManifest(
            name="shared-name",
            description="installed version",
            install_path="/fake/installed",
            scope="user",
            marketplace="user-mp",
        ),
    )
    monkeypatch.setattr(
        plugins_module, "discover_installed_plugins", lambda **_kwargs: [installed]
    )

    fanout: PluginFanOut = load_all_plugin_components()
    ordered = [pc.manifest for pc in fanout.components if pc.manifest is not None]
    # First "shared-name" entry must be the built-in.
    first_shared = next(m for m in ordered if m.name == "shared-name")
    assert first_shared.scope == "built-in"


def test_load_all_plugin_components_cache_invalidates_on_builtin_mtime(
    builtin_root_seam, monkeypatch
):
    """Editing a built-in plugin refreshes the cache via the root's mtime."""
    monkeypatch.setattr(
        plugins_module, "discover_installed_plugins", lambda **_kwargs: []
    )

    # Warm the cache with zero plugins.
    first = load_all_plugin_components()
    assert [pc.manifest.name for pc in first.components if pc.manifest] == []

    # Add a plugin and bump the root dir's mtime forward to simulate an edit.
    _write_builtin_plugin(builtin_root_seam, "new-plugin")
    future = plugins_module._fanout_cache_mtime + 100.0
    os.utime(builtin_root_seam, (future, future))

    second = load_all_plugin_components()
    names = [pc.manifest.name for pc in second.components if pc.manifest]
    assert "new-plugin" in names, (
        "built-in root mtime must participate in cache invalidation"
    )


# ---------------------------------------------------------------------------
# Packaging sanity: the builtin_plugins/ dir exists on disk
# ---------------------------------------------------------------------------


def test_builtin_plugins_directory_shipped_with_package():
    """``importlib.resources`` can resolve ``builtin_plugins`` inside the core package.

    Also verifies the ``.keep`` sentinel is present so the dir survives in VCS.
    """
    import importlib.resources

    traversable = importlib.resources.files("mewbo_core") / "builtin_plugins"
    root = Path(str(traversable))
    assert root.is_dir(), f"expected built-in plugin root to exist: {root}"
    assert (root / ".keep").is_file(), "`.keep` sentinel must be committed"
