"""Granular progress boundaries preserve real units and honest unknowns."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mongomock
from mewbo_core.contracts.progress import StepSpec
from mewbo_graph.plugins.wiki._ctx import ProgressReporter
from mewbo_graph.plugins.wiki.step_plans import PHASE_STEPS
from mewbo_graph.wiki.resolve.scip_python import ScipPythonResolver
from mewbo_graph.wiki.store import MongoWikiStore
from mewbo_graph.wiki.types import make_graph_node


class _Producer:
    """A fake producer that makes resolver positions deterministic."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def produce(self, root: Path, project_name: str, extra_paths: Any) -> dict[str, Any]:
        self.calls.append(project_name)
        return {"documents": [{"relative_path": "mod.py", "occurrences": []}]}


def test_resolver_reports_each_root_and_document_pass(
    tmp_path: Path,
) -> None:
    """Injected progress receives per-root legs and exact document denominators."""
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    reports: list[tuple[str, int, int]] = []
    result = ScipPythonResolver("host/org/repo", producer=_Producer()).resolve(
        tmp_path, (), on_progress=lambda stage, current, total: reports.append(
            (stage, current, total)
        )
    )

    assert result.stats.indexed_roots == 2
    assert reports == [
        ("index", 1, 2),
        ("read", 1, 2),
        ("index", 2, 2),
        ("read", 2, 2),
        ("definitions", 1, 2),
        ("definitions", 2, 2),
        ("occurrences", 1, 2),
        ("occurrences", 2, 2),
    ]


def test_bulk_persistence_reports_the_final_short_batch() -> None:
    """A non-multiple input reports the short final Mongo bulk write."""
    store = MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki")
    nodes = [
        make_graph_node(
            slug="host/org/repo",
            node_id=f"node-{index}",
            type="File",
            name=f"file-{index}.py",
            file=f"file-{index}.py",
            range=(0, 1),
        )
        for index in range(store._BULK_BATCH_SIZE + 1)
    ]
    reports: list[tuple[int, int]] = []

    store.upsert_nodes(
        "host/org/repo", nodes, on_progress=lambda current, total: reports.append(
            (current, total)
        )
    )

    assert reports == [(1, 2), (2, 2)]


def test_unknown_step_starts_without_a_fabricated_denominator() -> None:
    """An uncountable blocking leg still carries a start time."""
    reporter = ProgressReporter(SimpleNamespace(job_id="", store=None))
    reporter.declare((StepSpec(key="clone.git", label="Cloning", group="clone"),))

    with reporter.step("clone.git"):
        pass

    record = reporter._ledger.find("clone.git")
    assert record is not None
    assert record.started_at is not None
    assert record.total is None
    assert record.current is None
    assert record.ended_at is not None
    assert record.ended_at <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_new_step_keys_belong_to_their_phase_plans() -> None:
    """Every declared granular boundary is addressable from its owning phase."""
    expected = {
        "clone": {"clone.resolve_credentials", "clone.git"},
        "graph": {
            "graph.resolve_scip_index",
            "graph.read_scip_index",
            "graph.index_definitions",
            "graph.resolve_occurrences",
            "graph.persist_nodes",
            "graph.persist_edges",
        },
    }

    for phase, keys in expected.items():
        declared = {spec.key for spec in PHASE_STEPS[phase]}
        assert keys <= declared
