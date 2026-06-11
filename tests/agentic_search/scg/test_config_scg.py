"""``ScgConfig`` reads the REAL config chain — no accessor monkeypatching.

Every other SCG test forces ``ScgConfig.enabled`` at the accessor seam (the
right isolation for route/runner behavior). This module is the one place that
proves an operator's ``app.json`` edit actually flips the feature end-to-end:
the accessors read ``scg.*`` through ``get_config_value`` and the per-run
runner resolution follows the same flip — never a stale process default.
Relies on the autouse ``app_config_file`` isolation in ``tests/conftest.py``
(fresh config file + cache reset per test). NEVER spawns a real LLM/session.
"""

from __future__ import annotations

import json

import pytest
from mewbo_api.agentic_search.runner import set_search_runner
from mewbo_api.agentic_search.scg.config import ScgConfig
from mewbo_core.config import set_app_config_path
from mewbo_graph.scg import store as scg_store_mod


@pytest.fixture(autouse=True)
def _isolate_scg_state():
    """Fresh SCG structure store + no pinned runner override around each test."""
    scg_store_mod.reset_for_tests()
    set_search_runner(None)
    yield
    scg_store_mod.reset_for_tests()
    set_search_runner(None)


def _write_config(tmp_path, payload: dict) -> None:
    target = tmp_path / "scg_app.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    set_app_config_path(target)


def test_accessors_default_off_with_spec_budgets():
    """A pristine config ships the feature OFF with the model-default tier."""
    assert ScgConfig.enabled() is False
    assert ScgConfig.default_tier() == "auto"


def test_accessors_flip_through_real_config(tmp_path):
    """An ``scg`` block in app.json flips every accessor — no monkeypatch."""
    _write_config(
        tmp_path,
        {
            "scg": {
                "enabled": True,
                "traversal": {"default_tier": "deep"},
            }
        },
    )

    assert ScgConfig.enabled() is True
    assert ScgConfig.default_tier() == "deep"


def test_runner_resolution_follows_real_config_flip(tmp_path):
    """Flipping ``scg.enabled`` in app.json flips ``get_search_runner`` live.

    Same process, same mapped source: echo while the file says off (the
    default), orchestrated on the next resolution after the config flip.
    """
    from mewbo_api.agentic_search.runner import EchoSearchRunner, get_search_runner
    from mewbo_api.agentic_search.scg.orchestrated_runner import (
        OrchestratedSearchRunner,
    )
    from mewbo_graph.scg.types import SourceDescriptor

    scg_store_mod.get_scg_store().upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )
    assert isinstance(get_search_runner(), EchoSearchRunner)

    _write_config(tmp_path, {"scg": {"enabled": True}})

    assert isinstance(get_search_runner(), OrchestratedSearchRunner)
