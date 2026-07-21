#!/usr/bin/env python3
"""Backfill ``commit_sha``/``job_id`` onto pre-isolation wiki artifact rows.

Graph nodes/edges/embeddings, entities/entity-edges/entity-embeddings and pages
gained per-job/commit attribution so a completed re-index can supersede the prior
commit's artifacts instead of unioning into them. Rows written before that carry
neither field. This script stamps them so the machinery has something to reason
about and the FIRST re-index after it reaps the accumulated union.

Runs against the deployed Mongo (same env vars as ``backfill_wiki_snapshot.py``).
The decision + write logic lives in ``mewbo_graph.wiki.backfill`` so it is unit
tested without a live database; this file only wires the collections.

Properties (see ``ArtifactIsolationBackfill``):
- **Idempotent** — every write is gated on ``commit_sha {$exists: false}``, so a
  re-run stamps nothing. Safe to run repeatedly.
- **Non-destructive** — it only ``$set``s two fields; it never deletes. Reaping
  the union is the next index's ``supersede_graph_artifacts``, not this script's.
- **Un-attributable rows are marked, not guessed** — a slug whose jobs never
  resolved a commit (a catalog workspace, or a clone that never succeeded) has
  its rows stamped ``None``/``None``, which supersede then preserves.
"""
from __future__ import annotations

import os
import sys

from pymongo import MongoClient

from mewbo_graph.wiki.backfill import ARTIFACT_COLLECTIONS, ArtifactIsolationBackfill


def main() -> int:
    uri = os.environ.get(
        "MEWBO_MONGODB_URI", "mongodb://mewbo:mewbo@localhost:27017/?authSource=admin"
    )
    database = os.environ.get("MEWBO_MONGODB_DATABASE", "mewbo")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[database]

    backfill = ArtifactIsolationBackfill(
        jobs=db["wiki_jobs"],
        artifacts={name: db[name] for name in ARTIFACT_COLLECTIONS},
    )
    report = backfill.run()

    for slug, decision in sorted(report["slugs"].items()):
        if decision["attributed"]:
            print(
                f"{slug}: attributed to commit {decision['commit_sha'][:12]} "
                f"(job {decision['job_id']})"
            )
        else:
            print(f"{slug}: un-attributable — stamped null (preserved by supersede)")

    print("\nper-collection rows stamped:")
    for name, count in sorted(report["collections"].items()):
        print(f"  {name}: {count}")
    print(f"\ndone: {report['total_modified']} rows stamped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
