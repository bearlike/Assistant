"""Repository-wide pytest configuration."""
# ruff: noqa: E402, I001

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Pin MEWBO_HOME to a throwaway dir before any test module is imported.
# apps/mewbo_api/src/mewbo_api/backend.py constructs its session/key/project
# stores as MODULE-LEVEL singletons (``key_store = create_key_store()`` and
# friends), so the first import of that module -- which happens during
# collection, before any fixture (autouse or not) gets a chance to run --
# resolves MEWBO_HOME once, for the life of the process. A per-test fixture is
# structurally too late for this: `pytest --collect-only` alone (zero
# fixtures, zero test bodies executed) was enough to write into the
# developer's real ~/.mewbo. This module-scope statement is the earliest seam
# that can reach it. ``setdefault`` respects an operator/CI-supplied value.
_pytest_home = tempfile.mkdtemp(prefix="mewbo-pytest-home-")
atexit.register(shutil.rmtree, _pytest_home, ignore_errors=True)
os.environ.setdefault("MEWBO_HOME", _pytest_home)

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
