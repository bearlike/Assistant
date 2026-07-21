"""Artifact-isolation backfill — attribution policy + idempotency.

The migration stamps ``commit_sha``/``job_id`` onto pre-isolation rows. Two
properties matter and neither needs a live Mongo: the per-slug attribution
DECISION (latest completed commit, else latest attempt with a commit, else
un-attributable) and IDEMPOTENCY (a second run changes nothing). The
``ArtifactIsolationBackfill`` class takes duck-typed collections, so a faithful
in-memory fake exercises the real ``run``/``attribution_for`` code — the fake
implements only the Mongo operator subset the backfill uses (``$exists`` +
equality), kept minimal so it cannot pass by lying about a richer query.
"""
from __future__ import annotations

from types import SimpleNamespace

from mewbo_graph.wiki.backfill import ARTIFACT_COLLECTIONS, ArtifactIsolationBackfill


class _FakeCollection:
    """In-memory stand-in for a Mongo collection (the ops the backfill uses)."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = [dict(d) for d in (docs or [])]

    def distinct(self, field: str) -> list:
        return list({d[field] for d in self.docs if field in d})

    def find(self, filt: dict) -> list[dict]:
        return [dict(d) for d in self.docs if self._match(d, filt)]

    def update_many(self, filt: dict, update: dict):
        n = 0
        for d in self.docs:
            if self._match(d, filt):
                d.update(update["$set"])
                n += 1
        return SimpleNamespace(modified_count=n)

    @staticmethod
    def _match(doc: dict, filt: dict) -> bool:
        for key, cond in filt.items():
            if isinstance(cond, dict) and "$exists" in cond:
                if (key in doc) != cond["$exists"]:
                    return False
            elif doc.get(key) != cond:
                return False
        return True


def _artifacts(rows_by_slug: dict[str, int]) -> dict[str, _FakeCollection]:
    """Every artifact collection seeded with *rows_by_slug* legacy (no-commit) docs."""
    cols: dict[str, _FakeCollection] = {}
    for name in ARTIFACT_COLLECTIONS:
        docs: list[dict] = []
        for slug, count in rows_by_slug.items():
            docs.extend({"slug": slug, "node_id": f"{slug}-{i}"} for i in range(count))
        cols[name] = _FakeCollection(docs)
    return cols


# ── attribution policy (pure) ────────────────────────────────────────────────


def test_attribution_prefers_latest_completed_commit() -> None:
    jobs = [
        {
            "job_id": "j1",
            "status": "complete",
            "commit_sha": "old",
            "phase_started_at": "2026-01-01",
        },
        {
            "job_id": "j2",
            "status": "complete",
            "commit_sha": "new",
            "phase_started_at": "2026-02-01",
        },
        {
            "job_id": "j3",
            "status": "failed",
            "commit_sha": "newest",
            "phase_started_at": "2026-03-01",
        },
    ]
    assert ArtifactIsolationBackfill.attribution_for(jobs) == ("new", "j2")


def test_attribution_falls_back_to_latest_attempt_with_a_commit() -> None:
    jobs = [
        {"job_id": "j1", "status": "failed", "commit_sha": "c1", "phase_started_at": "2026-01-01"},
        {
            "job_id": "j2",
            "status": "interrupted",
            "commit_sha": "c2",
            "phase_started_at": "2026-02-01",
        },
    ]
    assert ArtifactIsolationBackfill.attribution_for(jobs) == ("c2", "j2")


def test_attribution_is_none_when_no_job_resolved_a_commit() -> None:
    jobs = [{"job_id": "j1", "status": "failed", "commit_sha": None}]
    assert ArtifactIsolationBackfill.attribution_for(jobs) == (None, None)


# ── run: stamps, then is idempotent ──────────────────────────────────────────


def test_backfill_stamps_derivable_and_marks_unattributable() -> None:
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
        # org/empty has no job that ever resolved a commit → un-attributable.
        {"job_id": "je", "slug": "org/empty", "status": "failed", "commit_sha": None},
    ])
    artifacts = _artifacts({"org/done": 3, "org/empty": 2})
    report = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run()

    assert report["slugs"]["org/done"] == {
        "commit_sha": "deadbeef",
        "job_id": "jc",
        "attributed": True,
    }
    assert report["slugs"]["org/empty"] == {"commit_sha": None, "job_id": None, "attributed": False}
    # Derivable rows carry the commit; un-attributable rows carry an explicit None.
    nodes = artifacts["wiki_graph_nodes"].docs
    done = [d for d in nodes if d["slug"] == "org/done"]
    empty = [d for d in nodes if d["slug"] == "org/empty"]
    assert done and all(d["commit_sha"] == "deadbeef" and d["job_id"] == "jc" for d in done)
    assert empty and all(d["commit_sha"] is None and d["job_id"] is None for d in empty)


def test_backfill_is_idempotent() -> None:
    """The second pass changes nothing — the ``$exists`` gate skips stamped rows."""
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
    ])
    artifacts = _artifacts({"org/done": 4})
    backfill = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts)

    first = backfill.run()
    second = backfill.run()

    assert first["total_modified"] > 0
    assert second["total_modified"] == 0


def test_backfill_leaves_already_attributed_rows_untouched() -> None:
    """A row the store already stamped (post-isolation write) is never re-touched."""
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "newcommit",
         "phase_started_at": "2026-05-01"},
    ])
    nodes = _FakeCollection([
        {"slug": "org/done", "node_id": "fresh", "commit_sha": "newcommit", "job_id": "jc"},
        {"slug": "org/done", "node_id": "legacy"},  # pre-isolation, no field
    ])
    artifacts = {name: _FakeCollection() for name in ARTIFACT_COLLECTIONS}
    artifacts["wiki_graph_nodes"] = nodes

    ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run()

    fresh = next(d for d in nodes.docs if d["node_id"] == "fresh")
    legacy = next(d for d in nodes.docs if d["node_id"] == "legacy")
    assert fresh["commit_sha"] == "newcommit"  # untouched (already had a value)
    assert legacy["commit_sha"] == "newcommit"  # stamped by the backfill
