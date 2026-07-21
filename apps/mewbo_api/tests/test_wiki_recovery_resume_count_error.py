"""``JobRecovery.recover_interrupted`` must not let a
``ResumeCountError`` refusal consume a slug's retry-cap attempt.

Before this fix, ``recovery.py`` bumped the per-slug counter BEFORE calling
``WikiResume.resume`` — unconditionally, whether the resume succeeded, failed,
or was refused. Once ``ResumePlan.build`` started failing closed (raising
``ResumeCountError`` instead of silently returning 0 on a transient store-read
glitch), three glitches in a row would exhaust ``MAX_RETRIES`` and permanently
fail a job that never actually failed — swapping a silent full-rebuild for a
silent permanent failure, which is worse. A refusal must leave the job exactly
as it was, with its retry budget untouched.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from mewbo_api.wiki.recovery import JobRecovery
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob, Project


def _store(tmp_path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _job(store, job_id, slug, status) -> None:
    store.create_job(IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None,
    ))


def _project(store, slug) -> None:
    store.create_project(Project(
        slug=slug, source="gitea", lang="Python",
        indexedAt="2026-06-07T00:00:00Z", pages=1, desc="x",
    ))


def _runtime(store) -> MagicMock:
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-recovery"
    rt.start_async.return_value = True
    rt.is_running.return_value = False
    return rt


class _BrokenGraphCountStore:
    """Wraps a real store but fails the first read ``ResumePlan.build`` makes."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def count_graph_nodes(self, slug, *, commit_sha):
        raise RuntimeError("transient store glitch")


def test_recovery_refusal_does_not_consume_the_retry_cap(tmp_path):
    slug = "host/glitch"
    real_store = _store(tmp_path)
    _project(real_store, slug)
    _job(real_store, "jg", slug, "scanning")
    runtime = _runtime(real_store)
    broken = _BrokenGraphCountStore(real_store)

    # More cycles than MAX_RETRIES — if a refusal consumed the cap, this would
    # have terminally failed the job long before the loop ends.
    for _ in range(JobRecovery.MAX_RETRIES + 2):
        refreshed = JobRecovery.recover_interrupted(broken, runtime)
        assert refreshed == []  # resume raised, so it never made it to "refreshed"

    assert real_store.get_recovery_attempts(slug) == 0
    # Marked "interrupted" (the routine non-terminal handoff), never "failed".
    assert real_store.get_job("jg").status == "interrupted"
    runtime.start_async.assert_not_called()


def test_recovery_still_bumps_on_a_genuine_resume_failure(tmp_path):
    """A non-ResumeCountError failure (the resume genuinely broke) is DIFFERENT
    from a refusal — it must still count against the cap, or a persistently
    broken job would retry forever instead of eventually terminally failing."""
    slug = "host/realfailure"
    store = _store(tmp_path)
    _project(store, slug)
    _job(store, "jf", slug, "scanning")
    runtime = _runtime(store)
    # is_running raising is a genuine bug in the runtime, not a store-count
    # refusal — WikiResume.resume's in-flight guard call will raise straight
    # through as a bare Exception, landing in the generic except branch.
    runtime.is_running.side_effect = RuntimeError("runtime is broken")
    # Give the job an attached session so the in-flight guard is even reached.
    store.attach_job_session("jf", "sess-x")

    JobRecovery.recover_interrupted(store, runtime)

    assert store.get_recovery_attempts(slug) == 1
