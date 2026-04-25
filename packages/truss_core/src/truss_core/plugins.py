#!/usr/bin/env python3
"""Plugin discovery, manifest parsing, marketplace reading, and install/uninstall.

All path resolution is done by the CALLER via ``PluginsConfig.resolve_*()`` methods.
This module accepts resolved paths as parameters — no hardcoded ``~/.truss/`` or
``~/.claude/`` paths.

The only place ``${CLAUDE_PLUGIN_ROOT}`` is resolved is ``substitute_plugin_vars``.
It is applied once per plugin at discovery time via ``_deep_substitute``.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from truss_core.capabilities import parse_capabilities
from truss_core.common import get_logger

logging = get_logger(name="core.plugins")


# Dedupe "stale cache entry" TRACE logs per process. Plugin discovery runs
# every turn, so without this we'd log the same skipped manifest hundreds of
# times during a single session.
_LOGGED_STALE_CACHE: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _sanitize_path_component(value: str) -> str:
    """Remove path separators and '..' from a value used in path construction."""
    return value.replace("/", "-").replace("\\", "-").replace("..", "").strip("-")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginManifest:
    """Parsed .claude-plugin/plugin.json manifest."""

    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    install_path: str = ""  # absolute path to plugin root dir
    marketplace: str = ""  # which marketplace it came from
    scope: str = "user"  # "user" | "project" | "local"
    # Plugin-level capability gate. When non-empty, the values fan out onto
    # every agent / skill / command this plugin contributes — authors can
    # also repeat (or tighten) per file in the contribution's frontmatter.
    requires_capabilities: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PluginComponents:
    """Fan-out of a single plugin's contributions to existing registries."""

    manifest: PluginManifest | None
    skill_dirs: list[str] = field(default_factory=list)  # paths to skills/<name>/ dirs
    command_files: list[str] = field(default_factory=list)  # paths to commands/*.md files
    agent_files: list[str] = field(default_factory=list)  # paths to agents/*.md files
    mcp_config: dict | None = None  # parsed .mcp.json content (vars substituted)
    hooks_config: dict | None = None  # parsed hooks/hooks.json content
    # Plugin-contributed session tools (Truss extension to the Claude Code
    # plugin format). Each entry is a ``{tool_id, module, class}`` record used
    # by :class:`SessionToolRegistry` to instantiate per-session handlers.
    session_tool_entries: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


def substitute_plugin_vars(text: str, plugin_root: str) -> str:
    """Replace ``${CLAUDE_PLUGIN_ROOT}`` with *plugin_root* in *text*."""
    return text.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)


def _deep_substitute(obj: Any, plugin_root: str) -> Any:
    """Recursively apply ``substitute_plugin_vars`` to all strings in *obj*."""
    if isinstance(obj, dict):
        return {k: _deep_substitute(v, plugin_root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_substitute(item, plugin_root) for item in obj]
    if isinstance(obj, str):
        return substitute_plugin_vars(obj, plugin_root)
    return obj


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _read_manifest_data(plugin_dir: Path) -> dict | None:
    """Read + JSON-parse ``.claude-plugin/plugin.json`` once.

    Returns the raw dict, or ``None`` on any error. Keeps the file read
    in one place so ``parse_plugin_manifest`` and
    ``discover_plugin_components`` don't open the same file twice.
    """
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logging.trace("No manifest at {} — incomplete plugin cache entry", manifest_path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logging.debug("Failed to parse manifest at {}: {}", manifest_path, exc)
        return None


def _manifest_from_data(data: dict, plugin_dir: Path) -> PluginManifest | None:
    """Build a :class:`PluginManifest` from an already-parsed dict."""
    name = data.get("name", "")
    if not name:
        logging.debug(
            "Plugin manifest at {} missing required 'name' field", plugin_dir
        )
        return None

    # author may be a dict {"name": "..."} or a plain string
    raw_author = data.get("author", "")
    if isinstance(raw_author, dict):
        author = raw_author.get("name", "")
    else:
        author = str(raw_author) if raw_author else ""

    return PluginManifest(
        name=str(name),
        description=str(data.get("description", "")),
        version=str(data.get("version", "")),
        author=author,
        install_path=str(plugin_dir),
        requires_capabilities=parse_capabilities(data.get("requires-capabilities")),
    )


def parse_plugin_manifest(plugin_dir: Path | str) -> PluginManifest | None:
    """Parse ``.claude-plugin/plugin.json`` from *plugin_dir*.

    Returns ``None`` on any error (missing file, bad JSON, missing name).
    """
    plugin_dir = Path(plugin_dir)
    data = _read_manifest_data(plugin_dir)
    if data is None:
        return None
    return _manifest_from_data(data, plugin_dir)


# ---------------------------------------------------------------------------
# Component discovery
# ---------------------------------------------------------------------------


def discover_plugin_components(plugin_dir: Path | str) -> PluginComponents:
    """Scan *plugin_dir* for all plugin contributions.

    Returns a :class:`PluginComponents` instance.  If ``plugin.json`` is absent
    the manifest will be ``None`` but other components are still discovered.
    """
    plugin_dir = Path(plugin_dir)
    plugin_root = str(plugin_dir)

    # Parse plugin.json ONCE and reuse the raw dict for both the manifest
    # and the session_tools entries — no double file read.
    manifest_data = _read_manifest_data(plugin_dir)
    manifest = (
        _manifest_from_data(manifest_data, plugin_dir)
        if manifest_data is not None
        else None
    )

    # --- skills/
    # Return the parent skills/ directory so consumers can call load_extra_dir() directly.
    skill_dirs: list[str] = []
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        has_valid_skill = any(
            candidate.is_dir() and (candidate / "SKILL.md").is_file()
            for candidate in skills_dir.iterdir()
        )
        if has_valid_skill:
            skill_dirs.append(str(skills_dir))

    # --- commands/
    command_files: list[str] = []
    commands_dir = plugin_dir / "commands"
    if commands_dir.is_dir():
        command_files = sorted(str(p) for p in commands_dir.glob("*.md") if p.is_file())

    # --- agents/
    agent_files: list[str] = []
    agents_dir = plugin_dir / "agents"
    if agents_dir.is_dir():
        agent_files = sorted(str(p) for p in agents_dir.glob("*.md") if p.is_file())

    # --- .mcp.json
    mcp_config: dict | None = None
    mcp_path = plugin_dir / ".mcp.json"
    if mcp_path.is_file():
        try:
            raw: dict = json.loads(mcp_path.read_text(encoding="utf-8"))
            # Handle both Truss native {"servers": {...}} and Claude Code style
            # {"server-name": {...}} — if no top-level "servers" key, whole dict is servers
            substituted: dict = _deep_substitute(raw, plugin_root)
            mcp_config = substituted
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Failed to parse .mcp.json in {}: {}", plugin_dir, exc)

    # --- hooks/hooks.json (no variable substitution — merger does it later)
    hooks_config: dict | None = None
    hooks_path = plugin_dir / "hooks" / "hooks.json"
    if hooks_path.is_file():
        try:
            hooks_config = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Failed to parse hooks/hooks.json in {}: {}", plugin_dir, exc)

    # --- session_tools (Truss extension to the Claude Code plugin format).
    # Each entry is a ``{tool_id, module, class}`` record the core imports and
    # turns into a factory via :class:`SessionToolRegistry`. Read from the
    # same manifest_data parsed above — do not reopen plugin.json.
    session_tool_entries: list[dict] = []
    if manifest_data is not None:
        raw_entries = manifest_data.get("session_tools", [])
        if isinstance(raw_entries, list):
            session_tool_entries = [e for e in raw_entries if isinstance(e, dict)]

    return PluginComponents(
        manifest=manifest,
        skill_dirs=skill_dirs,
        command_files=command_files,
        agent_files=agent_files,
        mcp_config=mcp_config,
        hooks_config=hooks_config,
        session_tool_entries=session_tool_entries,
    )


# ---------------------------------------------------------------------------
# Installed-plugin registry
# ---------------------------------------------------------------------------


def discover_installed_plugins(
    registry_paths: list[Path | str],
    *,
    enabled: list[str] | None = None,
) -> list[PluginComponents]:
    """Read installed plugin registries and return discovered components.

    Registry format::

        {
            "version": 2,
            "plugins": {
                "name@marketplace": [
                    {"scope": "user", "installPath": "/abs/path", "version": "1.0.0"}
                ]
            }
        }

    - Takes the FIRST entry in the list for each key (highest-priority scope).
    - Filters by the name part (before ``@``) when *enabled* is non-empty.
    - Deduplicates by name — first registry_path wins.
    """
    seen: set[str] = set()
    result: list[PluginComponents] = []

    for raw_path in registry_paths:
        registry_path = Path(raw_path)
        if not registry_path.is_file():
            continue
        try:
            registry: dict = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Failed to read registry {}: {}", registry_path, exc)
            continue

        plugins_map: dict[str, list[dict]] = registry.get("plugins", {})
        for key, entries in plugins_map.items():
            if not entries:
                continue

            # Split "name@marketplace"
            if "@" in key:
                plugin_name, marketplace = key.split("@", 1)
            else:
                plugin_name, marketplace = key, ""

            # Dedup — first seen wins
            if plugin_name in seen:
                continue

            # Apply enabled filter
            if enabled:
                if plugin_name not in enabled:
                    continue

            # First entry = highest priority scope
            entry = entries[0]
            install_path = entry.get("installPath", "")
            scope = entry.get("scope", "user")

            if not install_path or not Path(install_path).is_dir():
                logging.debug(
                    "Plugin {} installPath '{}' does not exist — skipping",
                    plugin_name,
                    install_path,
                )
                continue

            # Skip entries with no manifest file — these are incomplete/stale
            # cache entries (e.g. repos that lack .claude-plugin/plugin.json).
            manifest_file = Path(install_path) / ".claude-plugin" / "plugin.json"
            if not manifest_file.is_file():
                stale_key = (plugin_name, str(manifest_file))
                if stale_key not in _LOGGED_STALE_CACHE:
                    _LOGGED_STALE_CACHE.add(stale_key)
                    logging.trace(
                        "Plugin {} has no manifest at {} — skipping stale cache entry",
                        plugin_name,
                        manifest_file,
                    )
                continue

            seen.add(plugin_name)
            components = discover_plugin_components(Path(install_path))

            # Overlay marketplace and scope onto the manifest
            if components.manifest is not None:
                patched_manifest = replace(
                    components.manifest, marketplace=marketplace, scope=scope
                )
                components = replace(components, manifest=patched_manifest)

            result.append(components)

    return result


# ---------------------------------------------------------------------------
# Built-in plugin discovery (first-party bundles shipped inside the core wheel)
# ---------------------------------------------------------------------------


# Test seam: tests may monkeypatch this to point ``load_all_plugin_components``
# at a synthetic built-in root. Kept deliberately minimal — production code
# resolves the real root via :func:`importlib.resources.files`.
_BUILTIN_ROOT_OVERRIDE: Path | None = None


def discover_builtin_plugins(root: Path | str) -> list[PluginComponents]:
    """Discover first-party plugins shipped inside the core package.

    Walks immediate subdirectories of *root* (no registry indirection)
    and returns :class:`PluginComponents` for each directory that
    contains a ``.claude-plugin/plugin.json``. Built-in plugins bypass
    ``installed_plugins.json`` because they ship with the core wheel —
    their presence is a property of the installation, not user action.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    result: list[PluginComponents] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            continue
        components = discover_plugin_components(child)
        # Mark scope = "built-in" so the origin is visible in /plugins listings.
        if components.manifest is not None:
            patched = replace(
                components.manifest, scope="built-in", marketplace="built-in"
            )
            components = replace(components, manifest=patched)
        result.append(components)
    return result


def _resolve_builtin_root() -> Path:
    """Locate the ``builtin_plugins/`` directory inside the core package.

    Uses :mod:`importlib.resources` so the lookup works from wheels,
    editable installs, and source checkouts alike. Returns an empty
    :class:`Path` if the resource cannot be resolved — callers must
    still check ``is_dir()``.
    """
    if _BUILTIN_ROOT_OVERRIDE is not None:
        return _BUILTIN_ROOT_OVERRIDE
    try:
        traversable = importlib.resources.files("truss_core") / "builtin_plugins"
        return Path(str(traversable))
    except (ModuleNotFoundError, OSError) as exc:
        logging.debug("Could not resolve built-in plugin root: {}", exc)
        return Path("")


# ---------------------------------------------------------------------------
# Marketplace sync (clone/update repos from config)
# ---------------------------------------------------------------------------


def sync_marketplaces(
    marketplace_repos: list[str],
    install_base: Path,
) -> list[Path]:
    """Ensure marketplace repos are cloned locally, return their directory paths.

    For each repo in *marketplace_repos* (GitHub ``owner/repo`` format), clone
    it into ``install_base/marketplaces/<repo-name>/`` if not already present.
    Returns the list of marketplace directories (same contract as
    ``PluginsConfig.resolve_marketplace_dirs()``).

    This is the bridge between the ``plugins.marketplaces`` config list and the
    filesystem-based marketplace discovery.  Without it, standalone deployments
    (e.g. Docker without ``~/.claude``) would have zero marketplaces.
    """
    marketplaces_base = install_base / "marketplaces"
    marketplaces_base.mkdir(parents=True, exist_ok=True)

    dirs: list[Path] = []
    for repo in marketplace_repos:
        # "anthropics/claude-plugins-official" → dir name "claude-plugins-official"
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        mp_dir = marketplaces_base / _sanitize_path_component(repo_name)

        if mp_dir.is_dir():
            # Already cloned — check for marketplace.json
            marker = mp_dir / ".claude-plugin" / "marketplace.json"
            if marker.is_file():
                dirs.append(mp_dir)
                continue
            # Dir exists but looks broken — try updating
            if (mp_dir / ".git").is_dir():
                try:
                    subprocess.run(
                        ["git", "-C", str(mp_dir), "pull", "--ff-only"],
                        check=True,
                        capture_output=True,
                        timeout=60,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                    logging.warning("Failed to update marketplace {}: {}", repo, exc)
                if marker.is_file():
                    dirs.append(mp_dir)
                continue

        # Not yet cloned — shallow clone (only need marketplace.json + plugin dirs)
        git_url = f"https://github.com/{repo}.git"
        logging.info("Cloning marketplace {} into {}", repo, mp_dir)
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", git_url, str(mp_dir)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            dirs.append(mp_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            logging.warning("Failed to clone marketplace {}: {}", repo, exc)

    return dirs


# ---------------------------------------------------------------------------
# Marketplace discovery
# ---------------------------------------------------------------------------


def discover_marketplace_plugins(marketplace_dirs: list[Path | str]) -> list[dict]:
    """Read marketplace.json from each directory and return a flat list of plugin dicts.

    Each dict contains: ``name``, ``description``, ``category``, ``marketplace``,
    ``installed`` (always ``False`` — caller can enrich).
    """
    result: list[dict] = []
    for raw_dir in marketplace_dirs:
        mp_dir = Path(raw_dir)
        marketplace_json = mp_dir / ".claude-plugin" / "marketplace.json"
        if not marketplace_json.is_file():
            logging.debug("No marketplace.json found in {}", mp_dir)
            continue
        try:
            data: dict = json.loads(marketplace_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Failed to parse marketplace.json in {}: {}", mp_dir, exc)
            continue

        marketplace_name = data.get("name", str(mp_dir.name))
        for plugin in data.get("plugins", []):
            result.append(
                {
                    "name": plugin.get("name", ""),
                    "description": plugin.get("description", ""),
                    "category": plugin.get("category", ""),
                    "marketplace": marketplace_name,
                    "installed": False,
                }
            )

    return result


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------


def install_plugin(
    name: str,
    marketplace: str,
    *,
    marketplace_dirs: list[Path],
    install_base: Path,
) -> PluginManifest:
    """Install a plugin from a marketplace into *install_base*.

    - Finds the plugin entry in marketplace.json by searching *marketplace_dirs*.
    - For local sources (string starting with ``./``): copies the directory.
    - For git sources: clones the repo.
    - Updates ``install_base/installed_plugins.json``.
    - Returns the parsed :class:`PluginManifest`.
    """
    plugin_entry: dict | None = None
    source_mp_dir: Path | None = None

    for mp_dir in marketplace_dirs:
        marketplace_json = mp_dir / ".claude-plugin" / "marketplace.json"
        if not marketplace_json.is_file():
            continue
        try:
            data: dict = json.loads(marketplace_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        mp_name = data.get("name", "")
        if mp_name != marketplace:
            continue
        for entry in data.get("plugins", []):
            if entry.get("name") == name:
                plugin_entry = entry
                source_mp_dir = mp_dir
                break
        if plugin_entry is not None:
            break

    if plugin_entry is None:
        raise ValueError(f"Plugin '{name}' not found in marketplace '{marketplace}'")

    source = plugin_entry.get("source", "")
    version = plugin_entry.get("version", "latest")
    cache_dir = (
        install_base
        / "cache"
        / _sanitize_path_component(marketplace)
        / _sanitize_path_component(name)
        / _sanitize_path_component(version)
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(source, str) and source.startswith("./"):
        # Local path relative to the marketplace directory
        if source_mp_dir is None:
            raise ValueError(f"Plugin '{name}' not found in any marketplace")
        src_path = (source_mp_dir / source).resolve()
        if not src_path.is_relative_to(source_mp_dir.resolve()):
            raise ValueError(f"Plugin source path escapes marketplace directory: {source}")
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        shutil.copytree(src_path, cache_dir)
    elif isinstance(source, dict):
        if source.get("source") == "url" and source.get("url"):
            git_url = source["url"]
        else:
            repo = source.get("repo", "")
            if not repo:
                raise ValueError(
                    f"Plugin '{name}' has a dict source with no 'repo' or 'url': {source!r}"
                )
            git_url = f"https://github.com/{repo}.git"
        if (cache_dir / ".git").exists():
            logging.info("Plugin {} already cloned, skipping", name)
        else:
            subprocess.run(
                ["git", "clone", git_url, str(cache_dir)],
                check=True,
                capture_output=True,
            )
    else:
        raise ValueError(f"Unsupported plugin source for '{name}': {source!r}")

    # Update registry
    registry_path = install_base / "installed_plugins.json"
    registry: dict = {"version": 2, "plugins": {}}
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    registry.setdefault("plugins", {})
    key = f"{name}@{marketplace}"
    registry["plugins"][key] = [
        {
            "scope": "user",
            "installPath": str(cache_dir),
            "version": version,
        }
    ]
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    manifest = parse_plugin_manifest(cache_dir)
    if manifest is None:
        raise RuntimeError(f"Installed plugin '{name}' is missing a valid plugin.json")
    return manifest


def uninstall_plugin(name: str, *, install_base: Path) -> bool:
    """Remove *name* from the installed plugins registry and delete its cache directory.

    Returns ``True`` if the plugin was found and removed, ``False`` otherwise.
    """
    registry_path = install_base / "installed_plugins.json"
    if not registry_path.is_file():
        return False

    try:
        registry: dict = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Failed to read registry for uninstall: {}", exc)
        return False

    plugins: dict = registry.get("plugins", {})
    keys_to_remove = [k for k in plugins if k.split("@")[0] == name]
    if not keys_to_remove:
        return False

    for key in keys_to_remove:
        entries = plugins.pop(key, [])
        for entry in entries:
            install_path = Path(entry.get("installPath", ""))
            if install_path.is_dir():
                try:
                    shutil.rmtree(install_path)
                    logging.info("Removed plugin cache: {}", install_path)
                except OSError as exc:
                    logging.warning("Failed to remove plugin cache {}: {}", install_path, exc)

    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Convenience: load all plugin components from config (DRY helper)
# ---------------------------------------------------------------------------


@dataclass
class PluginFanOut:
    """Aggregated components from all enabled plugins, ready for registry injection."""

    components: list[PluginComponents]
    skill_dirs: list[str]
    command_files: list[str]
    agent_files: list[str]
    mcp_servers: dict[str, dict]
    hooks_configs: list[tuple[dict, str]]  # (hooks_json, plugin_root)
    session_tool_entries: list[dict] = field(default_factory=list)


_fanout_cache: PluginFanOut | None = None
_fanout_cache_mtime: float = 0.0


def load_all_plugin_components() -> PluginFanOut:
    """Discover all enabled plugins and aggregate their components.

    Uses ``PluginsConfig`` from the live config for all path resolution.
    Returns a :class:`PluginFanOut` that callers can inject into their
    registries.  This is the single point of truth for "what do plugins
    contribute?" — used by both ``Orchestrator.__init__`` and the API
    endpoints so they stay in sync.

    Results are cached and only recomputed when the installed-plugins
    registry file changes (mtime comparison).
    """
    global _fanout_cache, _fanout_cache_mtime

    from truss_core.config import get_config

    cfg = get_config().plugins
    if not cfg.enabled:
        return PluginFanOut([], [], [], [], {}, [], [])

    # Hydrate CLAUDE_PLUGIN_ROOT from config so shell commands in agent
    # templates can rely on it. setdefault lets docker.env / the host
    # environment override it without the config silently clobbering the value.
    os.environ.setdefault("CLAUDE_PLUGIN_ROOT", str(cfg.resolve_install_dir()))

    # Check cache freshness against registry file mtime
    registry_paths = cfg.resolve_registry_paths()
    max_mtime = 0.0
    for rp in registry_paths:
        try:
            max_mtime = max(max_mtime, Path(rp).stat().st_mtime)
        except OSError:
            pass

    # Include the built-in plugin root's mtime so the cache refreshes when
    # a developer edits a built-in plugin during local dev. Lookup is cheap
    # (single stat call) and keeps editable installs responsive.
    builtin_root = _resolve_builtin_root()
    try:
        max_mtime = max(max_mtime, builtin_root.stat().st_mtime)
    except OSError:
        pass

    if _fanout_cache is not None and max_mtime <= _fanout_cache_mtime:
        return _fanout_cache

    # Built-in components come first so they win the dedup race inside the
    # fan-out loop below (same-name plugin from a marketplace loses).
    builtin_components = discover_builtin_plugins(builtin_root)
    all_components = [
        *builtin_components,
        *discover_installed_plugins(
            registry_paths=cfg.resolve_registry_paths(),
            enabled=cfg.enabled_plugins or None,
        ),
    ]

    skill_dirs: list[str] = []
    command_files: list[str] = []
    agent_files: list[str] = []
    mcp_servers: dict[str, dict] = {}
    hooks_configs: list[tuple[dict, str]] = []
    session_tool_entries: list[dict] = []

    for pc in all_components:
        if pc.manifest is None:
            continue
        skill_dirs.extend(pc.skill_dirs)
        command_files.extend(pc.command_files)
        agent_files.extend(pc.agent_files)
        session_tool_entries.extend(pc.session_tool_entries)
        if pc.mcp_config:
            # Normalize Claude Code "mcpServers" → "servers"
            raw = pc.mcp_config
            if "mcpServers" in raw and "servers" not in raw:
                raw = {**raw, "servers": raw["mcpServers"]}
            servers = raw.get("servers", raw)
            if isinstance(servers, dict):
                for srv_name, srv_cfg in servers.items():
                    if srv_name in ("servers", "mcpServers"):
                        continue
                    if isinstance(srv_cfg, dict):
                        mcp_servers.setdefault(srv_name, srv_cfg)
        if pc.hooks_config:
            hooks_configs.append((pc.hooks_config, pc.manifest.install_path))

    result = PluginFanOut(
        components=all_components,
        skill_dirs=skill_dirs,
        command_files=command_files,
        agent_files=agent_files,
        mcp_servers=mcp_servers,
        hooks_configs=hooks_configs,
        session_tool_entries=session_tool_entries,
    )
    _fanout_cache = result
    _fanout_cache_mtime = max_mtime
    return result
