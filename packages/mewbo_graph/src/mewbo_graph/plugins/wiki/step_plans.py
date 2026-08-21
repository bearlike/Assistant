"""Declared step plans for the normal wiki indexing pipeline.

The ledger contains one record per declaration, never per repository unit, so
these plans remain bounded regardless of a repository's size.
"""
from __future__ import annotations

import math
from typing import Any

from mewbo_core.contracts.progress import StepSpec

from mewbo_graph.wiki.types import Project

# Provisional relative defaults. Replace these hand-set weights with measured
# durations once they exist; the graph phase is deliberately weighted for the
# parse, resolution, persistence, and embedding work it actually owns.
CLONE_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="clone.resolve_credentials",
        label="Resolving clone credentials",
        group="clone",
        weight=0.2,
    ),
    StepSpec(
        key="clone.git",
        label="Cloning repository",
        group="clone",
        weight=0.8,
    ),
    StepSpec(
        key="clone.count_files",
        label="Counting checkout files",
        group="clone",
        weight=0.4,
    ),
    StepSpec(
        key="clone.record_checkout",
        label="Recording checkout",
        group="clone",
        weight=0.2,
    ),
)

SCAN_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="scan.discover", label="Discovering source files", group="scan", weight=0.7
    ),
    StepSpec(
        key="scan.inspect_files",
        label="Inspecting source files",
        group="scan",
        unit="files",
        weight=1.5,
    ),
    StepSpec(
        key="scan.persist_manifest",
        label="Persisting file manifest",
        group="scan",
        weight=0.4,
    ),
)

GRAPH_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="graph.discover_files",
        label="Preparing graph files",
        group="graph",
        weight=0.5,
    ),
    StepSpec(
        key="graph.parse",
        label="Parsing source files",
        group="graph",
        unit="files",
        weight=5.0,
    ),
    StepSpec(
        key="graph.resolve_scip_index",
        label="Indexing Python projects",
        group="graph",
        unit="roots",
        weight=2.0,
    ),
    StepSpec(
        key="graph.read_scip_index",
        label="Reading Python indexes",
        group="graph",
        unit="roots",
        weight=0.8,
    ),
    StepSpec(
        key="graph.index_definitions",
        label="Indexing symbol definitions",
        group="graph",
        unit="documents",
        weight=0.7,
    ),
    StepSpec(
        key="graph.resolve_occurrences",
        label="Resolving symbol occurrences",
        group="graph",
        unit="documents",
        weight=1.5,
    ),
    StepSpec(
        key="graph.validate",
        label="Validating code graph",
        group="graph",
        weight=1.0,
    ),
    StepSpec(
        key="graph.persist_nodes",
        label="Persisting graph nodes",
        group="graph",
        unit="batches",
        weight=1.5,
    ),
    StepSpec(
        key="graph.persist_edges",
        label="Persisting graph edges",
        group="graph",
        unit="batches",
        weight=1.5,
    ),
    StepSpec(
        key="graph.embed",
        label="Embedding graph nodes",
        group="graph",
        unit="nodes",
        weight=5.0,
    ),
    StepSpec(
        key="graph.persist_embeddings",
        label="Persisting graph embeddings",
        group="graph",
        weight=1.0,
    ),
    StepSpec(
        key="graph.record_fingerprint",
        label="Recording graph fingerprint",
        group="graph",
        weight=0.3,
    ),
)

ENRICH_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="enrich.mint_entities",
        label="Minting entities",
        group="enrich",
        unit="entities",
        weight=5.0,
    ),
)

PLAN_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="plan.compose", label="Composing page plan", group="plan", weight=1.5
    ),
    StepSpec(
        key="plan.validate",
        label="Validating page plan",
        group="plan",
        unit="pages",
        weight=0.8,
    ),
    StepSpec(
        key="plan.persist", label="Persisting page plan", group="plan", weight=0.3
    ),
)

PAGES_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="pages.write",
        label="Writing documentation pages",
        group="pages",
        unit="pages",
        weight=8.0,
    ),
)

FINALIZE_STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="finalize.reconcile_pages",
        label="Reconciling pages",
        group="finalize",
        weight=0.7,
    ),
    StepSpec(
        key="finalize.validate_diagrams",
        label="Validating diagrams",
        group="finalize",
        unit="pages",
        weight=0.7,
    ),
    StepSpec(
        key="finalize.resolve_metadata",
        label="Resolving repository metadata",
        group="finalize",
        weight=0.5,
    ),
    StepSpec(
        key="finalize.verify_graph",
        label="Verifying code graph",
        group="finalize",
        weight=0.7,
    ),
    StepSpec(
        key="finalize.persist_project",
        label="Persisting project",
        group="finalize",
        weight=0.4,
    ),
    StepSpec(
        key="finalize.supersede",
        label="Superseding prior artifacts",
        group="finalize",
        weight=0.8,
    ),
    StepSpec(
        key="finalize.publish",
        label="Publishing wiki",
        group="finalize",
        weight=0.3,
    ),
)

PHASE_STEPS: dict[str, tuple[StepSpec, ...]] = {
    "clone": CLONE_STEPS,
    "scan": SCAN_STEPS,
    "graph": GRAPH_STEPS,
    "enrich": ENRICH_STEPS,
    "plan": PLAN_STEPS,
    "pages": PAGES_STEPS,
    "finalize": FINALIZE_STEPS,
}


def planned_steps(project: Project | None, phase: str) -> tuple[StepSpec, ...]:
    """Return *phase*'s default specs calibrated by a project's prior run.

    Cost: ``O(declared steps)``. Weights are relative-only, so a measured step
    can meaningfully share a plan with a defaulted step. Unit-counted steps use
    seconds per unit when both readings have positive counts, which keeps a
    repository that grew from inheriting raw duration from a smaller checkout.
    An unreadable project or measurement is simply absent and leaves defaults.
    """
    defaults = PHASE_STEPS.get(phase, ())
    if project is None:
        return defaults
    try:
        measurements = project.step_measurements
    except Exception:
        return defaults
    calibrated: list[StepSpec] = []
    for spec in defaults:
        try:
            measurement = measurements.get(spec.key)
            seconds = measurement.seconds if measurement is not None else None
            units = measurement.units if measurement is not None else None
            if seconds is None or not math.isfinite(seconds):
                calibrated.append(spec)
                continue
            if spec.unit is not None and units is not None:
                if units <= 0:
                    calibrated.append(spec)
                    continue
                weight = seconds / units
            else:
                weight = seconds
            if not math.isfinite(weight) or weight <= 0:
                calibrated.append(spec)
                continue
            calibrated.append(spec.model_copy(update={"weight": weight}))
        except Exception:
            calibrated.append(spec)
    return tuple(calibrated)


def planned_steps_for_slug(store: Any, slug: str, phase: str) -> tuple[StepSpec, ...]:
    """Load and calibrate *phase*'s plan for *slug*, falling back safely.

    Cost: ``O(one record + declared steps)``. Calibration informs an estimate;
    a store read failure must never fail an index, so it quietly returns defaults.
    """
    try:
        project = store.get_project(slug)
    except Exception:
        project = None
    return planned_steps(project, phase)


def planned_pipeline_steps_for_slug(store: Any, slug: str) -> tuple[StepSpec, ...]:
    """Return the calibrated, fixed plan for one whole indexing job.

    Cost: ``O(one record + declared steps)``. The reporter declares this complete
    plan before the first phase begins, fixing the ledger denominator for the
    run. ``pages.write`` remains one aggregate step: the page count becomes known
    when the plan is committed, but its relative LLM-generation weight must be
    present from the beginning rather than appended as pages arrive.
    """
    try:
        project = store.get_project(slug)
    except Exception:
        project = None
    return tuple(
        spec for phase in PHASE_STEPS for spec in planned_steps(project, phase)
    )


__all__ = [
    "CLONE_STEPS",
    "ENRICH_STEPS",
    "FINALIZE_STEPS",
    "GRAPH_STEPS",
    "PAGES_STEPS",
    "PHASE_STEPS",
    "PLAN_STEPS",
    "SCAN_STEPS",
    "planned_pipeline_steps_for_slug",
    "planned_steps",
    "planned_steps_for_slug",
]
