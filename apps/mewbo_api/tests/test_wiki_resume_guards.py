"""The in-flight resume guard and the resume-side half of the fallback-ladder
threading, for ``mewbo_api.wiki.resume.WikiResume``.

Contract tests from the caller's seam, stubbing only ``runtime`` (the one I/O
boundary ``WikiResume`` depends on).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_api.wiki.resume import WikiResume
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def runtime(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-resume"
    rt.start_async.return_value = True
    rt.is_running.return_value = False
    return rt


def _seed_interrupted_job(store, *, job_id="j1", slug="org/repo", submission=None):
    store.create_job(IndexingJob(
        jobId=job_id, slug=slug, status="interrupted",
        scannedCount=0, totalCount=0, currentFile=None,
        model="anthropic/claude-sonnet-4-6", commitSha="abc123",
    ))
    store.save_job_submission(job_id, submission or {
        "repoUrl": f"https://{slug}", "slug": slug, "platform": "github",
        "depth": "comprehensive", "language": "en",
        "model": "anthropic/claude-sonnet-4-6", "filterMode": "exclude",
        "dirs": [], "files": [],
    })
    return job_id


# ── In-flight guard ─────────────────────────────────────────────────────────


def test_resume_raises_when_session_already_running(store, runtime):
    """A resume must never stack on a job whose session is still in flight —
    the clone dir is named by job_id, so a stacked resume races the same
    checkout."""
    job_id = _seed_interrupted_job(store)
    store.attach_job_session(job_id, "sess-already-running")
    runtime.is_running.return_value = True

    with pytest.raises(RuntimeError):
        WikiResume.resume(store, runtime, job_id)

    # No mutation happened — the guard fires BEFORE any write.
    assert store.get_job(job_id).status == "interrupted"
    runtime.start_async.assert_not_called()


def test_resume_proceeds_when_no_session_attached_yet(store, runtime):
    """A job that never got a session attached (e.g. first resume ever) has
    nothing to guard against — resume proceeds normally."""
    job_id = _seed_interrupted_job(store)
    result = WikiResume.resume(store, runtime, job_id)
    assert result["job_id"] == job_id
    runtime.start_async.assert_called_once()


def test_resume_proceeds_when_session_not_running(store, runtime):
    job_id = _seed_interrupted_job(store)
    store.attach_job_session(job_id, "sess-idle")
    runtime.is_running.return_value = False

    result = WikiResume.resume(store, runtime, job_id)
    assert result["job_id"] == job_id
    runtime.start_async.assert_called_once()


# ── Fallback ladder (resume half): threads through the reconstructed
# submission sidecar the exact same way it does at initial start() ──────────


def test_resume_threads_fallback_ladder_from_persisted_submission(store, runtime):
    job_id = _seed_interrupted_job(store, submission={
        "repoUrl": "https://org/repo", "slug": "org/repo", "platform": "github",
        "depth": "comprehensive", "language": "en",
        "model": "anthropic/claude-sonnet-4-6", "filterMode": "exclude",
        "dirs": [], "files": [], "fallbackModels": ["openai/gpt-5.4", "gemini-2.5-pro"],
    })
    WikiResume.resume(store, runtime, job_id)
    kw = runtime.start_async.call_args.kwargs
    assert kw["fallback_models"] == ("openai/gpt-5.4", "gemini-2.5-pro")


def test_resume_no_ladder_when_submission_carries_none(store, runtime):
    job_id = _seed_interrupted_job(store)
    WikiResume.resume(store, runtime, job_id)
    kw = runtime.start_async.call_args.kwargs
    assert kw["fallback_models"] is None


# ── ResumeCountError: fail-closed count reads must REFUSE, never rebuild ────


class _BrokenGraphCountStore:
    """Wraps a real store but fails the FIRST read ``ResumePlan.build`` makes.

    Delegates everything else so the job/session/submission state a real
    ``resume()`` call touches before reaching ``ResumePlan.build`` still works.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def count_graph_nodes(self, slug, *, commit_sha):
        raise RuntimeError("transient store glitch")


def test_resume_refuses_when_resume_plan_count_fails(store, runtime):
    """A transient store-read failure must REFUSE the resume, not silently
    fall back to a full rebuild (that empty plan IS the expensive rebuild
    fail-closed counting exists to prevent)."""
    from mewbo_graph.wiki.resume import ResumeCountError

    job_id = _seed_interrupted_job(store)
    broken = _BrokenGraphCountStore(store)

    with pytest.raises(ResumeCountError):
        WikiResume.resume(broken, runtime, job_id)

    # No mutation happened — the raise fires before the job is reset to
    # "scanning" or any resume plan is persisted.
    assert store.get_job(job_id).status == "interrupted"
    assert store.get_resume_plan(job_id) is None
    runtime.start_async.assert_not_called()


# ── restart plumbing: ResumePlan.for_restart(), not the bare constructor ────


def test_resume_restart_renders_the_restart_instruction(store, runtime):
    """``restart=True`` must use ``ResumePlan.for_restart()`` (restart=True on
    the plan), not the bare ``ResumePlan()`` constructor — the two are
    structurally identical (empty skip set) but only ``for_restart()`` renders
    the REBUILD instruction instead of the RESUME-and-reuse one."""
    job_id = _seed_interrupted_job(store)
    # Seed a populated graph so a checkpoint resume WOULD normally skip it —
    # proving restart=True ignores that and forces a rebuild regardless.
    from mewbo_graph.wiki.types import make_graph_node

    store.upsert_nodes("org/repo", [
        make_graph_node(
            slug="org/repo", node_id="n1", type="Function", name="f",
            file="a.py", range=(0, 1),
        ),
    ])

    WikiResume.resume(store, runtime, job_id, restart=True)

    kw = runtime.start_async.call_args.kwargs
    assert "RESTART" in kw["user_query"]
    assert "rebuild this index from scratch" in kw["user_query"]
    persisted = store.get_resume_plan(job_id)
    assert persisted is not None
    assert persisted["restart"] is True
    assert persisted["skip"] == []
