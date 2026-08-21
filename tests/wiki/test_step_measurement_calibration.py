"""Regression coverage for persisted wiki step-cost calibration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_core.contracts.progress import ProgressLedger
from mewbo_graph.plugins.wiki.finalize import _measurements_for_finalize
from mewbo_graph.plugins.wiki.step_plans import GRAPH_STEPS, planned_steps
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    IndexingJob,
    Project,
    StepMeasurement,
    WikiPage,
    make_graph_node,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _project(
    measurements: dict[str, StepMeasurement] | None = None,
) -> Project:
    return Project(
        slug="acme/repo",
        source="git",
        lang="en",
        indexedAt="",
        pages=0,
        desc="",
        stepMeasurements=measurements or {},
    )


def test_first_index_plan_uses_declared_defaults() -> None:
    """An uncalibrated project keeps the valid declaration intact."""
    plan = planned_steps(None, "graph")

    assert plan == GRAPH_STEPS
    assert all(spec.weight > 0 for spec in plan)


def test_completed_ledger_writes_measurements_and_next_plan_uses_them() -> None:
    """Completed steps persist costs while unmeasured steps retain defaults."""
    ledger = ProgressLedger.from_plan(GRAPH_STEPS)
    ledger.enter("graph.parse", _NOW)
    ledger.advance("graph.parse", current=20, total=20)
    ledger.finish("graph.parse", _NOW + timedelta(seconds=100))

    measurements = _measurements_for_finalize(
        _project(), ledger, now=_NOW + timedelta(seconds=100)
    )
    persisted = _project(measurements)
    next_plan = {spec.key: spec for spec in planned_steps(persisted, "graph")}
    defaults = {spec.key: spec for spec in GRAPH_STEPS}

    assert measurements["graph.parse"] == StepMeasurement(seconds=100, units=20)
    assert next_plan["graph.parse"].weight == 5.0
    unmeasured = "graph.resolve_scip_index"
    assert next_plan[unmeasured].weight == defaults[unmeasured].weight


def test_blend_preserves_rate_when_repository_unit_count_changes() -> None:
    """The blended duration follows the newer run's unit count."""
    result = StepMeasurement(seconds=100, units=10).blended_with(
        StepMeasurement(seconds=300, units=20)
    )

    assert result == StepMeasurement(seconds=225, units=20)


def test_skipped_step_retains_previous_measurement() -> None:
    """A resumed phase skip cannot turn prior work into a free estimate."""
    ledger = ProgressLedger.from_plan(GRAPH_STEPS)
    ledger.finish("graph.parse", _NOW, state="skipped", note="reused on resume")
    prior = _project({"graph.parse": StepMeasurement(seconds=80, units=16)})

    measurements = _measurements_for_finalize(prior, ledger, now=_NOW)

    assert measurements["graph.parse"] == StepMeasurement(seconds=80, units=16)


def test_finalize_persists_completed_measurements(tmp_path: Path) -> None:
    """Finalize folds the job ledger into the durable project snapshot."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = JsonWikiStore(root_dir=tmp_path)
    store.upsert_nodes(
        "acme/repo",
        [
            make_graph_node(
                slug="acme/repo",
                node_id="acme/repo:file",
                type="File",
                name="a.py",
                file="a.py",
                range=(0, 0),
            )
        ],
    )
    ledger = ProgressLedger.from_plan(GRAPH_STEPS)
    ledger.enter("graph.parse", _NOW)
    ledger.advance("graph.parse", current=20, total=20)
    ledger.finish("graph.parse", _NOW + timedelta(seconds=100))
    store.create_job(
        IndexingJob(
            jobId="job-calibrated",
            slug="acme/repo",
            status="finalizing",
            scannedCount=20,
            totalCount=20,
            currentFile=None,
            progress=ledger,
        )
    )
    store.attach_job_session("job-calibrated", "session-calibrated")
    store.save_job_submission(
        "job-calibrated",
        {
            "repoUrl": "https://example.com/acme/repo",
            "slug": "acme/repo",
            "platform": "git",
            "language": "en",
            "depth": "concise",
            "model": "test",
            "filterMode": "exclude",
            "dirs": [],
            "files": [],
        },
    )
    store.save_page(
        "acme/repo",
        WikiPage(
            id="overview",
            title="Overview",
            frontmatter=Frontmatter(title="Overview", slug="overview"),
            body="# Overview",
            toc=[],
            nav=[],
        ),
    )
    tool = WikiFinalizeTool(session_id="session-calibrated")
    action_step = MagicMock(tool_input={"landingPageId": "overview"})

    with patch.object(
        mod, "_resolve_runtime", return_value=SimpleNamespace(wiki_store=store)
    ):
        asyncio.run(tool.handle(action_step))

    persisted = store.get_project("acme/repo")
    assert persisted is not None
    assert persisted.step_measurements["graph.parse"].units == 20
    assert persisted.step_measurements["graph.parse"].seconds >= 100


def test_unreadable_measurement_uses_declared_default() -> None:
    """A corrupt stored calibration costs only an imprecise estimate."""
    project = SimpleNamespace(step_measurements={"graph.parse": object()})

    plan = {spec.key: spec for spec in planned_steps(project, "graph")}
    defaults = {spec.key: spec for spec in GRAPH_STEPS}

    assert plan["graph.parse"].weight == defaults["graph.parse"].weight
