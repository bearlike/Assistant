"""Backfill artifact attribution onto pre-isolation wiki rows.

Graph nodes/edges/embeddings, entities/entity-edges/entity-embeddings and pages
gained ``commit_sha``/``job_id`` so a completed re-index can supersede the prior
commit's artifacts instead of unioning into them. Rows written before that had
neither field. This class stamps the existing rows so the new machinery has
something to reason about, and so the FIRST re-index after the migration reaps
the accumulated union rather than adding to it.

Design (the runnable Mongo CLI is ``scripts/backfill_wiki_artifact_isolation.py``):

- **Atomic + DI'd.** The collections it reads/writes are injected (duck-typed
  ``distinct``/``find``/``update_many``), so this stays a pure-Python engine — no
  ``pymongo`` import, no layering violation — and a test drives it against an
  in-memory fake with no live Mongo.
- **Idempotent by construction.** Every write is gated on
  ``commit_sha {$exists: false}``. After the first pass every row carries the
  field (a real value or ``None``), so a second pass matches nothing. Rows the
  store writes post-isolation always carry it too, so the backfill never touches
  a correctly-attributed row.
- **Non-destructive.** It only ``$set``s two fields; it never deletes. Superseding
  the union is the NEXT index's job (``supersede_graph_artifacts``), not the
  migration's — so a first pass cannot lose data even if its attribution is
  imperfect.

Attribution policy (see :meth:`attribution_for`): per-row commit provenance was
never recorded and cannot be reconstructed, so attribution is decided at
whole-slug granularity from the ``wiki_jobs`` record — the slug's latest index
that resolved a commit. That commit's artifacts are the live ones under the old
upsert-merge semantics; the stragglers of earlier commits are conservatively
attributed to it too and get reaped by the next re-index's supersede. A slug with
NO job that ever resolved a commit (a catalog workspace, or a clone that never
succeeded) is genuinely un-attributable: its rows are stamped ``None``/``None``,
which supersede then PRESERVES — the honest outcome for a row no index owns.
"""
from __future__ import annotations

from typing import Any, Protocol


class _Collection(Protocol):
    """The tiny slice of a Mongo collection this backfill uses (duck-typed)."""

    def distinct(self, field: str) -> list[Any]: ...
    def find(self, filt: dict[str, Any]) -> Any: ...
    def update_many(self, filt: dict[str, Any], update: dict[str, Any]) -> Any: ...


# The artifact collections that gained ``commit_sha``/``job_id``. Pages are
# included for provenance; they supersede by plan-prune, not by commit.
ARTIFACT_COLLECTIONS: tuple[str, ...] = (
    "wiki_graph_nodes",
    "wiki_graph_edges",
    "wiki_embeddings",
    "wiki_entities",
    "wiki_entity_edges",
    "wiki_entity_embeddings",
    "wiki_pages",
)


class ArtifactIsolationBackfill:
    """Stamp pre-isolation artifact rows with their owning commit + job.

    ``jobs`` is the ``wiki_jobs`` collection; ``artifacts`` maps each artifact
    collection name (see :data:`ARTIFACT_COLLECTIONS`) to its collection.
    """

    def __init__(
        self, *, jobs: _Collection, artifacts: dict[str, _Collection]
    ) -> None:
        """Inject the jobs collection + the artifact collections by name."""
        self._jobs = jobs
        self._artifacts = artifacts

    @staticmethod
    def attribution_for(jobs: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        """Return ``(commit_sha, job_id)`` to stamp a slug's legacy rows with.

        The slug's latest index that resolved a commit — a ``complete`` one when
        any exists, else the latest attempt that at least got past clone. Ordered
        by ``phase_started_at`` (ISO-8601, so lexicographic order is chronological)
        with ``job_id`` as a stable tiebreak. ``(None, None)`` when no job ever
        resolved a commit: the slug is genuinely un-attributable and its rows stay
        commit-less, which supersede preserves.
        """
        with_commit = [j for j in jobs if j.get("commit_sha")]
        if not with_commit:
            return (None, None)
        complete = [j for j in with_commit if j.get("status") == "complete"]
        pool = complete or with_commit
        latest = max(
            pool,
            key=lambda j: (j.get("phase_started_at") or "", str(j.get("job_id") or "")),
        )
        return (latest.get("commit_sha"), latest.get("job_id"))

    def run(self) -> dict[str, Any]:
        """Stamp every un-attributed artifact row; return a summary report.

        Idempotent: the ``$exists: false`` gate means a re-run stamps nothing.
        The report carries per-collection modified counts and the per-slug
        attribution decision, so an operator can see exactly what was inferred.
        """
        slugs: set[str] = set()
        for col in self._artifacts.values():
            slugs.update(str(s) for s in col.distinct("slug"))

        collections: dict[str, int] = {name: 0 for name in self._artifacts}
        per_slug: dict[str, dict[str, Any]] = {}

        for slug in sorted(slugs):
            jobs = list(self._jobs.find({"slug": slug}))
            commit_sha, job_id = self.attribution_for(jobs)
            set_doc = {"commit_sha": commit_sha, "job_id": job_id}
            for name, col in self._artifacts.items():
                res = col.update_many(
                    {"slug": slug, "commit_sha": {"$exists": False}},
                    {"$set": set_doc},
                )
                collections[name] += int(getattr(res, "modified_count", 0) or 0)
            per_slug[slug] = {
                "commit_sha": commit_sha,
                "job_id": job_id,
                "attributed": commit_sha is not None,
            }

        return {
            "collections": collections,
            "slugs": per_slug,
            "total_modified": sum(collections.values()),
        }


__all__ = ["ARTIFACT_COLLECTIONS", "ArtifactIsolationBackfill"]
