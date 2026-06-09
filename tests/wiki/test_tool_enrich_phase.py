"""enrich is a first-class indexing phase between graph and plan."""
from __future__ import annotations

from mewbo_graph.wiki.types import IndexingJob


def test_indexing_job_accepts_enrich_phase():
    job = IndexingJob(
        jobId="j1", slug="org/repo", status="scanning",
        scannedCount=0, totalCount=0, currentFile=None, phase="enrich",
    )
    assert job.phase == "enrich"
