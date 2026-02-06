"""Repository-wide pytest configuration."""
# ruff: noqa: E402, I001

import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.dirname(__file__))
SOURCE_PATHS = [
    ROOT,
    os.path.join(ROOT, "packages", "meeseeks_core", "src"),
    os.path.join(ROOT, "packages", "meeseeks_tools", "src"),
    os.path.join(ROOT, "apps", "meeseeks_cli", "src"),
    os.path.join(ROOT, "apps", "meeseeks_api", "src"),
    os.path.join(ROOT, "apps", "meeseeks_chat", "src"),
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
