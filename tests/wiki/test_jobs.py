"""exercises WikiIndexingJob.start/cancel/events_since with stubbed orchestration."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_api.wiki.jobs import WikiIndexingJob
from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import WizardSubmission


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def runtime(store):
    rt = MagicMock()
    rt.wiki_store = store
    # resolve_session returns a stable session_id
    rt.resolve_session.return_value = "sess-abc"
    rt.start_async.return_value = True
    rt.cancel.return_value = True
    return rt


@pytest.fixture
def submission():
    return WizardSubmission(
        repoUrl="https://github.com/bearlike/Assistant",
        slug="bearlike/Assistant",
        platform="github",
        token=None,
        depth="comprehensive",
        language="en",
        model="anthropic/claude-sonnet-4-6",
        filterMode="exclude",
        dirs=[],
        files=[],
    )


def test_start_creates_job_and_kicks_session(store, runtime, submission):
    job = WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    assert job.job_id  # generated
    assert job.slug == "bearlike/Assistant"
    assert job.status == "queued"
    assert store.get_job(job.job_id) is not None
    # session linked
    runtime.resolve_session.assert_called_once()
    assert runtime.resolve_session.call_args.kwargs["session_tag"] == f"wiki:job:{job.job_id}"
    runtime.start_async.assert_called_once()
    # allowed_tools includes indexer + spawn + read tools
    kw = runtime.start_async.call_args.kwargs
    assert "wiki_clone_repo" in kw["allowed_tools"]
    assert "spawn_agent" in kw["allowed_tools"]
    assert "wiki_load_grounder" in kw["allowed_tools"]
    # skill_instructions contains the wiki-indexer playbook
    assert "wiki_load_grounder" in kw["skill_instructions"]
    # cwd is a path under the clone root
    assert "wiki" in kw["cwd"].lower() and job.job_id in kw["cwd"]
    # user_query carries the submission shape
    assert "bearlike/Assistant" in kw["user_query"]
    assert kw["model_name"] == "anthropic/claude-sonnet-4-6"
    # submission persisted on the job record (needed by Task 2.5c finalize)
    assert store.get_job_submission(job.job_id) is not None


def test_start_persists_submission_without_token(store, runtime):
    """The submission token must NOT be persisted."""
    sub = WizardSubmission(
        repoUrl="https://github.com/x/y",
        slug="x/y",
        platform="github",
        token="ghp_supersecret",
        depth="concise",
        language="en",
        model="anthropic/claude-sonnet-4-6",
        filterMode="exclude",
        dirs=[], files=[],
    )
    job = WikiIndexingJob.start(sub, runtime=runtime, hook_manager=None)
    persisted = store.get_job_submission(job.job_id)
    assert persisted is not None
    assert "token" not in persisted or not persisted.get("token")


def test_cancel_appends_terminal_event_and_calls_runtime(store, runtime, submission):
    job = WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    result = WikiIndexingJob.cancel(job.job_id, runtime=runtime)
    assert result is True
    runtime.cancel.assert_called_once_with("sess-abc")
    events = store.load_job_events(job.job_id)
    assert any(e["type"] == "cancelled" for e in events)


def test_cancel_idempotent(store, runtime, submission):
    job = WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    WikiIndexingJob.cancel(job.job_id, runtime=runtime)
    second = WikiIndexingJob.cancel(job.job_id, runtime=runtime)
    # store.cancel_job is idempotent — second call returns False
    assert second is False


def test_events_since_returns_post_idx_events(store, runtime, submission):
    job = WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    # Simulate the orchestration appending events.
    idx1 = store.append_job_event(
        job.job_id,
        {"type": "queued", "jobId": job.job_id, "slug": job.slug, "totalCount": 10},
    )
    store.append_job_event(
        job.job_id,
        {"type": "scanning", "file": "a.py", "index": 1, "totalCount": 10},
    )
    store.append_job_event(
        job.job_id,
        {"type": "scanned", "file": "a.py", "index": 1, "totalCount": 10},
    )
    events = WikiIndexingJob.events_since(job.job_id, after_idx=idx1, store=store)
    types = [e["type"] for e in events]
    assert types == ["scanning", "scanned"]


def test_cancel_unknown_job_returns_false(runtime):
    result = WikiIndexingJob.cancel("does-not-exist", runtime=runtime)
    assert result is False
