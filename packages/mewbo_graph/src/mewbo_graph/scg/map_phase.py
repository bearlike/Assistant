"""Map-job phase-progress sink — a dependency-injection seam (down-only).

A map job's progress is persisted in the API *run* store so it rides the
``RunSseGenerator`` plumbing the search UI already tails — an API/transport
concern, deliberately NOT in this engine. The relocated ``scg`` SessionTools
must still report phase transitions (``finalize`` etc.) without importing
**up** into ``mewbo_api``.

So the API registers a concrete writer here at startup
(``MapPhaseSink.register(...)``) and the plugin emits through it. With no sink
registered — a core/graph-only install, or the API never initialised — every
emit is a no-op returning ``None``: the SCG structure write already happened,
the phase write is purely cosmetic progress, so its absence never fails a map.

This mirrors the wiki ``emit_phase`` invariant (one write feeds both the
SSE-tailed indexing page and the snapshot-polling landing card) while keeping
the layering acyclic — the wiki helper can write its own (relocated) store
directly; the map-job sink writes the API's run store, so it must be injected.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from mewbo_core.common import get_logger

logging = get_logger(name="mewbo_graph.scg.map_phase")

# (job_id, phase) -> appended event idx, or None when no real store is wired.
MapPhaseWriter = Callable[[str, str], "int | None"]


class MapPhaseSink:
    """Process-wide injectable writer of map-job phase progress."""

    _writer: ClassVar[MapPhaseWriter | None] = None

    @classmethod
    def register(cls, writer: MapPhaseWriter | None) -> None:
        """Install the concrete phase writer (called by the API at startup)."""
        cls._writer = writer

    @classmethod
    def reset(cls) -> None:
        """Clear the registered writer (test isolation / core-only teardown)."""
        cls._writer = None

    @classmethod
    def emit(cls, job_id: str, phase: str) -> int | None:
        """Emit *phase* for *job_id* via the registered writer; ``None`` if none.

        Best-effort and isolated: a writer that raises is swallowed (the phase
        is cosmetic) so a transport hiccup never aborts an otherwise-good map.
        """
        if cls._writer is None:
            return None
        try:
            return cls._writer(job_id, phase)
        except Exception:  # noqa: BLE001 — cosmetic progress, never fatal
            logging.warning("map-phase sink failed for job %s phase %s", job_id, phase)
            return None


__all__ = ["MapPhaseSink", "MapPhaseWriter"]
