"""Pytest configuration for repository tests."""
# ruff: noqa: E402, I001

import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE_PATHS = [
    ROOT,
    os.path.join(ROOT, "packages", "meeseeks_core", "src"),
    os.path.join(ROOT, "packages", "meeseeks_tools", "src"),
    os.path.join(ROOT, "apps", "meeseeks_cli", "src"),
    os.path.join(ROOT, "apps", "meeseeks_api", "src"),
    os.path.join(ROOT, "meeseeks_ha_conversation"),
]
for path in SOURCE_PATHS:
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest

from meeseeks_core.config import (
    AppConfig,
    reset_config,
    set_app_config_path,
    set_mcp_config_path,
)


@pytest.fixture(autouse=True)
def app_config_file(tmp_path: Path):
    """Write a fresh app config file and point the loader at it."""
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
        from meeseeks_tools.integration.mcp_pool import reset_mcp_pool
    except ImportError:
        yield
        return
    reset_mcp_pool()
    yield
    reset_mcp_pool()
