"""Repository-wide pytest configuration."""
# ruff: noqa: E402, I001

import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.dirname(__file__))
SOURCE_PATHS = [
    ROOT,
    os.path.join(ROOT, "packages", "mewbo_core", "src"),
    os.path.join(ROOT, "packages", "mewbo_tools", "src"),
    os.path.join(ROOT, "apps", "mewbo_cli", "src"),
    os.path.join(ROOT, "apps", "mewbo_api", "src"),
    os.path.join(ROOT, "apps", "mewbo_mcp", "src"),
    os.path.join(ROOT, "apps"),
]
for path in SOURCE_PATHS:
    if path not in sys.path:
        sys.path.insert(0, path)

# Wiki test fixtures embed a fake repo (``tests/wiki/fixtures/tiny_repo``) that
# ships its own ``tests/test_main.py`` as *data* for the scanner — it is not a
# Mewbo test. Excluding it from collection avoids a module-name collision
# (``tests.test_main``) that pytest's prepend import mode hits once another
# top-level ``tests`` package is on sys.path.
collect_ignore_glob = ["tests/wiki/fixtures/*"]

import pytest

from mewbo_core.config import (
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
