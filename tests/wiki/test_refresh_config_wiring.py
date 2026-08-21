"""``wiki.refresh.*`` thresholds must reach the stage that consumes them.

Every one of these seven knobs was declared in config, documented, and shipped
in ``app.example.json`` while nothing read it. The reason nobody noticed is the
reason this file asserts on the CONSTRUCTED OBJECT rather than on a refresh's
output: each stage takes its threshold as a keyword argument whose default
equals the config default, so an unwired stage produces byte-identical results
to a correctly wired one on a default deployment. Only a non-default config
value, read back off the object it was supposed to reach, can tell the two
apart.
"""
from __future__ import annotations

from typing import Any

import pytest
from mewbo_core.config import reset_config, set_config_override
from mewbo_graph.wiki.refresh import (
    DocStalenessPlanner,
    GraphDeltaIndexer,
    MemoryReconciler,
    RefreshOrchestrator,
)
from mewbo_graph.wiki.store import JsonWikiStore

from .conftest import FakeEmbedder, FakeParser

# field in ``wiki.refresh`` → (orchestrator stage attribute, stage's own attribute,
# a value that is NOT the config default).
WIRING: dict[str, tuple[str, str, Any]] = {
    "closure_max_depth": ("_graph_indexer", "_closure_max_depth", 9),
    "drift_keep": ("_reconciler", "_drift_keep", 0.42),
    "drift_invalidate": ("_reconciler", "_drift_invalidate", 0.11),
    "page_keep": ("_doc_planner", "_keep", 0.17),
    "page_edit": ("_doc_planner", "_edit", 0.44),
    "page_regen": ("_doc_planner", "_regen", 0.88),
    "new_page_min": ("_doc_planner", "_new_page_min", 23),
}


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def refresh_config():
    """Apply a ``wiki.refresh`` override for one test and clear it after."""

    def _apply(**fields: Any) -> None:
        set_config_override({"wiki": {"refresh": dict(fields)}})

    yield _apply
    reset_config()


def _build(store) -> RefreshOrchestrator:
    """The production composition root, with only its I/O collaborators stubbed."""
    return RefreshOrchestrator.from_store(
        store, slug="acme/repo", parser=FakeParser({}), embedder=FakeEmbedder()
    )


@pytest.mark.parametrize("field", sorted(WIRING))
def test_each_threshold_reaches_the_stage_that_consumes_it(
    field, store, refresh_config
) -> None:
    stage_attr, value_attr, value = WIRING[field]
    refresh_config(**{field: value})

    orch = _build(store)

    assert getattr(getattr(orch, stage_attr), value_attr) == value


def test_all_seven_travel_together(store, refresh_config) -> None:
    """One config read per field — not one field wired and six left behind.

    Set individually a mistake is easy to miss; set together, a stage that
    reads only the first argument it was given shows up immediately.
    """
    refresh_config(**{f: v for f, (_, _, v) in WIRING.items()})

    orch = _build(store)

    assert {
        field: getattr(getattr(orch, stage_attr), value_attr)
        for field, (stage_attr, value_attr, _) in WIRING.items()
    } == {field: value for field, (_, _, value) in WIRING.items()}


def test_defaults_survive_an_empty_config(store) -> None:
    """No override ⇒ exactly the constructor defaults, not zeros or ``None``.

    ``get_config_value`` returns its default for a missing key, so a wiring bug
    that passed ``None`` down would blow up the first comparison inside the
    drift gate rather than here — pin the resolved values instead.
    """
    orch = _build(store)

    assert orch._graph_indexer._closure_max_depth == 4
    assert (orch._reconciler._drift_keep, orch._reconciler._drift_invalidate) == (
        0.90,
        0.75,
    )
    planner = orch._doc_planner
    assert (planner._keep, planner._edit, planner._regen, planner._new_page_min) == (
        0.05,
        0.35,
        0.70,
        5,
    )


def test_a_configured_threshold_changes_the_verdict_it_gates(
    store, refresh_config
) -> None:
    """The value is CONSUMED, not merely stored on the stage.

    A staleness of 0.4 regenerates under the default 0.35 edit band and merely
    edits once the band is widened past it — so this fails both if the config
    never arrives and if it arrives on an attribute nothing reads.
    """
    refresh_config(page_edit=0.44)

    assert _build(store)._doc_planner._policy(0.4) == "edit"


def test_an_injected_stage_still_wins_over_config(store, refresh_config) -> None:
    """Config decides the DEFAULT, never "whether" — the embedder rule, applied.

    A composer built by hand out of explicitly constructed stages must keep the
    thresholds those stages were built with. Anything else would make a DI seam
    an operator setting could veto, and a test could no longer pin a stage's
    behaviour without also knowing the deployment's config.
    """
    refresh_config(**{f: v for f, (_, _, v) in WIRING.items()})

    orch = RefreshOrchestrator(
        store=store,
        graph_indexer=GraphDeltaIndexer(
            store, parser=FakeParser({}), closure_max_depth=2
        ),
        reconciler=MemoryReconciler(
            store=store, drift_keep=0.99, drift_invalidate=0.01
        ),
        doc_planner=DocStalenessPlanner(
            store=store, keep=0.01, edit=0.02, regen=0.03, new_page_min=1
        ),
    )

    assert orch._graph_indexer._closure_max_depth == 2
    assert (orch._reconciler._drift_keep, orch._reconciler._drift_invalidate) == (
        0.99,
        0.01,
    )
    planner = orch._doc_planner
    assert (planner._keep, planner._edit, planner._regen, planner._new_page_min) == (
        0.01,
        0.02,
        0.03,
        1,
    )
