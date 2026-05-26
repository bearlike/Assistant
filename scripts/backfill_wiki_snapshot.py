#!/usr/bin/env python3
"""Backfill ``branch``/``commit_sha``/``commit_short``/``maintainer_edited``
onto existing ``wiki_projects`` documents.

Runs inside the API container so ``/tmp/mewbo/wiki/clones/`` is the
right path. For each project:

1. Find the latest completed job for the slug.
2. Locate the on-disk clone dir for that job.
3. Resolve HEAD SHA + branch via ``git rev-parse``.
4. Detect grounder presence (``.mewbo/wiki.json`` or ``.devin/wiki.json``).
5. ``$set`` the four new fields on the wiki_projects document.

Idempotent — re-running just rewrites the same values.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pymongo import MongoClient

CLONE_ROOT = Path(os.environ.get("MEWBO_WIKI_CLONE_ROOT", "/tmp/mewbo/wiki/clones"))
GROUNDER_PATHS = (".mewbo/wiki.json", ".devin/wiki.json")


def rev_parse(repo: Path, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", *args],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"").decode(errors="ignore").strip() or None


def detect_grounder(repo: Path) -> bool:
    return any((repo / rel).exists() for rel in GROUNDER_PATHS)


def latest_clone_for_slug(db, slug: str) -> Path | None:
    """Return the clone dir for the latest completed job, or None."""
    jobs = db.wiki_jobs.find(
        {"slug": slug, "status": "complete"}, {"job_id": 1, "_id": 1}
    ).sort([("_id", -1)])
    for job in jobs:
        candidate = CLONE_ROOT / job["job_id"]
        if (candidate / ".git").exists():
            return candidate
    return None


def main() -> int:
    uri = os.environ.get("MEWBO_MONGODB_URI", "mongodb://mewbo:mewbo@localhost:27017/?authSource=admin")
    database = os.environ.get("MEWBO_MONGODB_DATABASE", "mewbo")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[database]

    updated = 0
    skipped = 0
    for project in db.wiki_projects.find({}, {"slug": 1, "_id": 0}):
        slug = project["slug"]
        clone = latest_clone_for_slug(db, slug)

        update = {
            "branch": None,
            "commit_sha": None,
            "commit_short": None,
            "maintainer_edited": False,
        }
        if clone is not None:
            sha = rev_parse(clone, ["HEAD"])
            br = rev_parse(clone, ["--abbrev-ref", "HEAD"])
            if br == "HEAD":
                br = None
            update["branch"] = br
            update["commit_sha"] = sha
            update["commit_short"] = sha[:7] if sha else None
            update["maintainer_edited"] = detect_grounder(clone)

        res = db.wiki_projects.update_one({"slug": slug}, {"$set": update})
        if res.modified_count:
            updated += 1
            print(
                f"updated {slug}: branch={update['branch']!r} "
                f"commit_short={update['commit_short']!r} "
                f"maintainer_edited={update['maintainer_edited']}"
            )
        else:
            skipped += 1
            print(f"skipped {slug} (no change)")

    print(f"\ndone: {updated} updated, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
