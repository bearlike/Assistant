"""Pytest configuration for repository tests."""
# ruff: noqa: E402, I001

import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE_PATHS = [
    ROOT,
    os.path.join(ROOT, "packages", "mewbo_core", "src"),
    os.path.join(ROOT, "packages", "mewbo_tools", "src"),
    os.path.join(ROOT, "apps", "mewbo_cli", "src"),
    os.path.join(ROOT, "apps", "mewbo_api", "src"),
    os.path.join(ROOT, "apps"),
]
for path in SOURCE_PATHS:
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest

from mewbo_core.config import (
    AppConfig,
    reset_config,
    set_app_config_path,
    set_mcp_config_path,
)


@pytest.fixture(autouse=True)
def app_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a fresh app config file and point the loader at it.

    Also pins ``MEWBO_HOME`` to a throwaway dir *before* constructing
    ``AppConfig()``. ``RuntimeConfig``'s ``config_dir``/``cache_dir``/
    ``session_dir`` fields default to ``""`` and a ``field_validator`` resolves
    that to ``resolve_mewbo_home()`` at construction time — the resolved
    absolute path is what gets baked into the JSON this fixture writes, not a
    placeholder that re-resolves later. A test that sets ``MEWBO_HOME`` itself
    inside its own body does so too late for anything reading `config_dir`
    (every ``_JsonCollectionStore`` subclass in ``mewbo_iam.stores``) — without
    this, those stores fall back to the developer's real ``~/.mewbo``.
    """
    monkeypatch.setenv("MEWBO_HOME", str(tmp_path / "home"))
    reset_config()
    config_path = tmp_path / "app.json"
    AppConfig().write(config_path)
    set_app_config_path(config_path)
    set_mcp_config_path(tmp_path / "mcp.json")
    yield config_path
    reset_config()


@pytest.fixture(autouse=True)
def _reset_mcp_pool():
    """Reset the MCP connection pool singleton between tests."""
    try:
        from mewbo_tools.integration.mcp_pool import reset_mcp_pool
    except ImportError:
        yield
        return
    reset_mcp_pool()
    yield
    reset_mcp_pool()


@pytest.fixture(autouse=True)
def _reset_schedule_trigger_provider():
    """Reset the process-wide schedule_trigger provider between tests.

    ``configs/app.json`` enables triggers, so importing ``backend.py`` (any apps
    test, in any random order) runs ``init_triggers`` and sets
    ``mewbo_core.triggers.session_tool._TRIGGER_TOOL_PROVIDER`` for the whole
    PROCESS. After that every ``Orchestrator`` registers ``schedule_trigger`` as
    an ``unconditional`` session tool, so it surfaces on any un-scoped OR
    permissive session — polluting core tests that assert an EXACT session-tool
    set. A test that genuinely WANTS the provider registers it in its own body;
    everyone else gets a clean slate (mirrors the config / MCP-pool resets above).
    """
    from mewbo_core.triggers import session_tool as _st

    saved = _st._TRIGGER_TOOL_PROVIDER
    _st._TRIGGER_TOOL_PROVIDER = None
    yield
    _st._TRIGGER_TOOL_PROVIDER = saved
