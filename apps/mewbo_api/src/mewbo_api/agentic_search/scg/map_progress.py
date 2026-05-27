"""Phase-progress writer for map-source (SCG indexing) jobs — spec #19 §16.2.

A map job's progress lives in the *agentic_search* store (alongside search
runs), so it rides the same run-event-log + ``RunSseGenerator`` plumbing. This
module owns the single writer of the current phase — the SCG analogue of the
wiki's ``_ctx.emit_phase`` (which we deliberately do NOT import: the wiki helper
is bound to ``WikiJobCtx`` + the wiki store's ``append_job_event``/``update_job``;
the map job rides ``append_map_job_event``/``update_map_job`` instead).

``MapJobProgress.emit_phase(store, job_id, phase)`` dual-writes in one call:

* appends a ``{"type": "phase", "name": phase}`` event to the map-job event log
  (the live SSE projection the indexing UI tails), AND
* patches ``phase`` + ``phase_started_at`` on the :class:`MapJobRecord` snapshot
  (the durable read the landing card polls).

Both surfaces are derived from the same write, so they can never drift apart —
the invariant the wiki ``emit_phase`` upholds for indexing-page vs landing-card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mewbo_core.common import get_logger

from ..schemas import MapJobPhase, utc_now_iso

if TYPE_CHECKING:
    from ..store import AgenticSearchStoreBase

logging = get_logger(name="api.agentic_search.scg.map_progress")


class MapJobProgress:
    """Atomic writer of map-job phase progress (event log + snapshot)."""

    @staticmethod
    def emit_phase(
        store: AgenticSearchStoreBase, job_id: str, phase: MapJobPhase
    ) -> int:
        """Append a ``phase`` event AND persist phase + start ts on the snapshot.

        Returns the appended event's monotonic idx. The dual write is the
        single source of truth for both the SSE-tailed indexing UI and the
        snapshot-polling landing card — mirrors the wiki ``emit_phase``.

        Both writes are best-effort and isolated: a snapshot-update failure
        never loses the already-appended event (the wiki helper's try/except
        stance), so the live stream stays authoritative.
        """
        started_at = utc_now_iso()
        idx = store.append_map_job_event(job_id, {"type": "phase", "name": phase})
        try:
            store.update_map_job(job_id, phase=phase, phase_started_at=started_at)
        except Exception:
            logging.warning("Map job %s snapshot phase update failed for %s", job_id, phase)
        return idx


__all__ = ["MapJobProgress"]
