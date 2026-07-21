"""Strict tool scope + headless enable_skills, and job/session
reconciliation, for the wiki indexer glue in ``mewbo_api.wiki.jobs``.

Contract tests from the caller's seam (``WikiIndexingJob.start`` /
``WikiIndexingSessionEndHook``), stubbing only ``runtime`` (the I/O boundary).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_api.wiki.jobs import WikiIndexingJob, WikiIndexingSessionEndHook
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob, WizardSubmission


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def runtime(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-abc"
    rt.start_async.return_value = True
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


# ── strict_tool_scope + enable_skills=False for the headless indexer ──────────


def test_start_scopes_the_indexer_strictly(store, runtime, submission):
    """INDEXER_TOOLS must be AUTHORITATIVE, not a permissive floor.

    Before this fix, ``allowed_tools`` was inert for the indexer: the missing
    ``strict_tool_scope`` sent the run down the branch that unions every
    non-MCP builtin back in regardless of the declared 16-tool allowlist.
    """
    WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    kw = runtime.start_async.call_args.kwargs
    assert kw["strict_tool_scope"] is True


def test_indexer_ceiling_grants_resolve_entity_but_not_the_writes(store, runtime, submission):
    """Product decision: the playbook body instructs entity-aware planning via
    ``resolve_entity`` (read-only) — the strict-scope ceiling must grant that
    lookup, or the playbook now instructs a call the allowlist forbids. The
    WRITE tools (``mint_entity``/``relate_entities``) stay off the root's own
    ceiling — those belong to the wiki-enricher child, which is filtered
    against its OWN allowlist, not the parent's."""
    WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    kw = runtime.start_async.call_args.kwargs
    assert "resolve_entity" in kw["allowed_tools"]
    assert "mint_entity" not in kw["allowed_tools"]
    assert "relate_entities" not in kw["allowed_tools"]


def test_start_disables_skill_inheritance_for_the_headless_indexer(store, runtime, submission):
    """A headless product run must not inherit host ``~/.claude`` skills (E-V)."""
    WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    kw = runtime.start_async.call_args.kwargs
    assert kw["enable_skills"] is False


def test_start_passes_no_ladder_when_submission_has_none(store, runtime, submission):
    """No opt-in ladder on the submission → fallback_models=None (inherit config)."""
    assert submission.fallback_models is None
    WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    kw = runtime.start_async.call_args.kwargs
    assert kw["fallback_models"] is None


def test_start_threads_the_opted_in_fallback_ladder(store, runtime, submission):
    """An opted-in ladder on the submission reaches runtime.start_async."""
    sub = submission.model_copy(
        update={"fallback_models": ["openai/gpt-5.4", " ", "gemini-2.5-pro"]}
    )
    WikiIndexingJob.start(sub, runtime=runtime, hook_manager=None)
    kw = runtime.start_async.call_args.kwargs
    # Blank entries dropped; order preserved; a tuple (matches start_async's type).
    assert kw["fallback_models"] == ("openai/gpt-5.4", "gemini-2.5-pro")


def test_start_empty_ladder_list_is_none_not_empty_tuple(store, runtime, submission):
    """An explicit empty list is equivalent to "no ladder" — collapse to None."""
    sub = submission.model_copy(update={"fallback_models": []})
    WikiIndexingJob.start(sub, runtime=runtime, hook_manager=None)
    kw = runtime.start_async.call_args.kwargs
    assert kw["fallback_models"] is None


# ── Job/session reconciliation ──────────────────────────────────────────────────


def _make_job(store: JsonWikiStore, job_id: str, slug: str, status: str) -> str:
    session_id = f"sess-{job_id}"
    store.create_job(
        IndexingJob(
            job_id=job_id, slug=slug, status=status,
            scanned_count=5, total_count=10, current_file=None,
        )
    )
    store.attach_job_session(job_id, session_id)
    return session_id


def test_hook_returns_assertion_when_failed_job_had_a_clean_session(tmp_path) -> None:
    """job=failed + session ended with error=None ⇒ returns an OutcomeAssertion
    AND logs the mismatch on the job's own event log (both readers matter)."""
    from mewbo_core.hooks import OutcomeAssertion

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = MagicMock(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    job_id = "job-failed"
    session_id = _make_job(store, job_id, "org/repo", "failed")
    result = hook(session_id, error=None)

    assert isinstance(result, OutcomeAssertion)
    assert result.reason == "job_not_complete"
    updated = store.get_job(job_id)
    assert updated is not None and updated.status == "failed", "job status must stay failed"
    events = store.load_job_events(job_id)
    logs = [e for e in events if e.get("type") == "log" and e.get("level") == "warning"]
    assert logs, "expected an outcome-assertion mismatch log entry"
    assert "outcome-assertion mismatch" in logs[0]["text"]


def test_hook_returns_none_when_failed_job_session_also_errored(tmp_path) -> None:
    """job=failed + session ALSO ended with an error → no assertion, no extra
    log (the session already honestly derives failed from its own error)."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = MagicMock(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    job_id = "job-failed-2"
    session_id = _make_job(store, job_id, "org/repo", "failed")
    result = hook(session_id, error="boom")

    assert result is None
    events = store.load_job_events(job_id)
    assert not [e for e in events if e.get("type") == "log"]


def test_hook_returns_assertion_for_non_terminal_job_with_clean_session(tmp_path) -> None:
    """The MAJORITY laundering case: job still non-terminal (e.g. "scanning",
    died mid-clone) but the session ended clean — must ALSO assert, not just
    the "failed" branch. The job still gets marked interrupted regardless."""
    from mewbo_core.hooks import OutcomeAssertion

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = MagicMock(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    job_id = "job-scanning"
    session_id = _make_job(store, job_id, "org/repo", "scanning")
    result = hook(session_id, error=None)

    assert isinstance(result, OutcomeAssertion)
    assert result.reason == "job_not_complete"
    updated = store.get_job(job_id)
    assert updated is not None and updated.status == "interrupted"


def test_hook_returns_none_for_non_terminal_job_with_error(tmp_path) -> None:
    """Non-terminal + the session ALSO errored → no assertion (already honest),
    but the interrupted handoff to recovery must still happen."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = MagicMock(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    job_id = "job-scanning-2"
    session_id = _make_job(store, job_id, "org/repo", "scanning")
    result = hook(session_id, error="boom")

    assert result is None
    updated = store.get_job(job_id)
    assert updated is not None and updated.status == "interrupted"


def test_hook_returns_none_for_cancelled_job_with_clean_session(tmp_path) -> None:
    """A deliberate cancel is not a mismatch, even if the session ended clean."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = MagicMock(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    job_id = "job-cancelled"
    session_id = _make_job(store, job_id, "org/repo", "cancelled")
    result = hook(session_id, error=None)

    assert result is None
    updated = store.get_job(job_id)
    assert updated is not None and updated.status == "cancelled"
    assert not [e for e in store.load_job_events(job_id) if e.get("type") == "log"]


def test_hook_still_no_ops_on_complete_job(tmp_path) -> None:
    """The happy path stays untouched: complete is the only true no-op status."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = MagicMock(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    job_id = "job-complete"
    session_id = _make_job(store, job_id, "org/repo", "complete")
    result = hook(session_id, error=None)

    assert result is None
    updated = store.get_job(job_id)
    assert updated is not None and updated.status == "complete"
    assert not [e for e in store.load_job_events(job_id) if e.get("type") == "log"]


def test_hook_promotes_session_status_end_to_end(tmp_path) -> None:
    """Full pipeline, using the REAL HookManager/SessionRuntime/summarize_session
    (only the LLM/orchestrator turn loop is bypassed — the completion +
    outcome_assertion events are appended in the same order
    ``Orchestrator._run_with_session_context_async``'s finally block does):
    a session ending "completed" over a non-terminal wiki job derives
    ``unmet_goal``; over a terminal-complete job it stays ``completed``.

    This is the exact fix for the 17-of-33 majority laundering case — the
    session reads clean, the job never finished.
    """
    from types import SimpleNamespace

    from mewbo_core.hooks import HookManager
    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.session_store import SessionStore

    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    session_store = SessionStore(root_dir=str(tmp_path / "sessions"))
    session_runtime = SessionRuntime(session_store=session_store)
    hook_manager = HookManager()
    hook_manager.on_session_end.append(
        WikiIndexingSessionEndHook(SimpleNamespace(wiki_store=wiki_store))
    )

    def _drive(job_id: str, slug: str, status: str) -> dict[str, object]:
        session_id = session_runtime.resolve_session()
        wiki_store.create_job(IndexingJob(
            job_id=job_id, slug=slug, status=status,
            scanned_count=1, total_count=1, current_file=None,
        ))
        wiki_store.attach_job_session(job_id, session_id)
        # Mirrors Orchestrator's finally block: the completion event lands
        # first, THEN any outcome_assertion events the hooks report.
        session_runtime.append_event(session_id, {
            "type": "completion",
            "payload": {"done": True, "done_reason": "completed", "task_result": "ok"},
        })
        for assertion in hook_manager.run_on_session_end(session_id, None):
            session_runtime.append_event(session_id, {
                "type": "outcome_assertion",
                "payload": assertion.model_dump(mode="json"),
            })
        return session_runtime.summarize_session(session_id)

    non_terminal = _drive("job-e2e-nonterm", "org/laundered", "scanning")
    assert non_terminal["status"] == "unmet_goal"

    terminal = _drive("job-e2e-term", "org/finished", "complete")
    assert terminal["status"] == "completed"


# ── The resume prompt no longer carries a ref: line ─────────────────────────
# The clone tool now resolves job.commit_sha SERVER-SIDE and ignores any ref
# the model supplies on a resume — a rendered ref: line would tell the model
# to pass a value the clone tool silently overrides.


def test_render_resume_query_never_renders_a_ref_line(store) -> None:
    from mewbo_api.wiki.jobs import _render_resume_query
    from mewbo_graph.wiki.resume import ResumePlan
    from mewbo_graph.wiki.types import IndexingJob

    job = IndexingJob(
        jobId="j1", slug="org/repo", status="interrupted",
        scannedCount=0, totalCount=0, currentFile=None,
        model="anthropic/claude-sonnet-4-6", commitSha="deadbeef1234",
    )
    plan = ResumePlan.build(store, job)

    rendered = _render_resume_query(store, job, plan)

    assert "ref:" not in rendered
    assert "deadbeef1234" not in rendered
    assert "RESUME" in rendered
