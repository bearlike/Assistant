"""exercises WikiIndexingJob.start/cancel/events_since with stubbed orchestration."""
from __future__ import annotations

from types import SimpleNamespace
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


def test_start_persists_token_credential_by_slug(store, runtime):
    """A token submission durably persists a RepoCredential keyed by slug."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    sub = WizardSubmission(
        repoUrl="https://git.example.com/org/repo",
        slug="git.example.com/org/repo",
        platform="gitea",
        token="ghp_durable",
        depth="comprehensive", language="en",
        model="anthropic/claude-sonnet-4-6",
        filterMode="exclude", dirs=[], files=[],
    )
    WikiIndexingJob.start(sub, runtime=runtime, hook_manager=None)
    cred = CredentialStore.load(store, CredentialScope.from_slug("git.example.com/org/repo"))
    assert cred is not None
    assert cred.kind == "token"
    assert cred.value == "ghp_durable"


def test_start_without_token_persists_no_credential(store, runtime, submission):
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    WikiIndexingJob.start(submission, runtime=runtime, hook_manager=None)
    assert CredentialStore.load(store, CredentialScope.from_slug(submission.slug)) is None


def test_refresh_leaves_durable_credential_resolvable(store, runtime):
    """Re-index reconstructs a token-less submission; the durable credential the
    initial submission saved stays untouched and resolvable — refresh performs
    no restore step of its own. CloneTokenCache is gone (vestigial once every
    token was durably saved at submission); the clone tool's own
    ``resolve_chain`` reads the credential straight from the store at clone
    time for the NEW job, so refresh has nothing to warm."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import Project

    slug = "git.example.com/org/repo"
    store.create_project(Project(
        slug=slug, source="gitea", lang="Python",
        indexedAt="2026-06-07T00:00:00Z", pages=1, desc="x",
    ))
    # An initial job with a token-stripped submission, plus the saved credential.
    first = WikiIndexingJob.start(WizardSubmission(
        repoUrl=f"https://{slug}", slug=slug, platform="gitea", token="ghp_orig",
        depth="comprehensive", language="en", model="anthropic/claude-sonnet-4-6",
        filterMode="exclude", dirs=[], files=[],
    ), runtime=runtime, hook_manager=None)
    cred = CredentialStore.load(store, CredentialScope.from_slug(slug))
    assert cred is not None and cred.kind == "token" and cred.value == "ghp_orig"

    # Refresh: the reconstructed submission has no token, and refresh does not
    # touch the credential store — the durable credential remains exactly as
    # saved, for the clone tool to resolve at clone time.
    refreshed = WikiIndexingJob.refresh(slug, runtime=runtime, hook_manager=None)
    assert refreshed.job_id != first.job_id
    cred_after = CredentialStore.load(store, CredentialScope.from_slug(slug))
    assert cred_after is not None and cred_after.value == "ghp_orig"


def test_refresh_swaps_retired_model_for_default(store, runtime, monkeypatch):
    """A stored submission whose model the proxy has retired is re-resolved to
    the configured wiki/llm default before the reindex starts — otherwise the
    whole reindex fast-fails on an invalid-model 400 (the Beacon regression)."""
    from types import SimpleNamespace

    import mewbo_core.config as core_cfg
    from mewbo_graph.wiki.types import Project

    slug = "git.example.com/acme/beacon"
    store.create_project(Project(
        slug=slug, source="gitea", lang="Python",
        indexedAt="2026-06-07T00:00:00Z", pages=1, desc="x",
    ))
    # Seed a stored submission carrying a now-retired model.
    WikiIndexingJob.start(WizardSubmission(
        repoUrl=f"https://{slug}", slug=slug, platform="gitea", token=None,
        depth="concise", language="en", model="gemini-3-flash-preview",
        filterMode="exclude", dirs=[], files=[],
    ), runtime=runtime, hook_manager=None)

    # Configured wiki default + a proxy that only offers the live model.
    def _fake_value(*keys, default=None):
        if keys == ("wiki", "default_model"):
            return "openai/gpt-5.4-mini"
        if keys == ("llm", "default_model"):
            return "openai/claude-sonnet-4-6"
        return default

    class _Llm:
        def resolve_available_model(self, model, *, fallback, timeout=4.0):
            return model if model == "gemini-3.5-flash" else fallback

    monkeypatch.setattr(core_cfg, "get_config_value", _fake_value)
    monkeypatch.setattr(core_cfg, "get_config", lambda: SimpleNamespace(llm=_Llm()))

    WikiIndexingJob.refresh(slug, runtime=runtime, hook_manager=None)

    kw = runtime.start_async.call_args.kwargs
    assert kw["model_name"] == "openai/gpt-5.4-mini"
    assert "gemini-3-flash-preview" not in kw["user_query"]


# ── WikiIndexingSessionEndHook (defense-in-depth) ────────────


def _make_non_terminal_job(store: JsonWikiStore, job_id: str, slug: str, status: str) -> str:
    """Seed an indexing job with *status* attached to a session; return session_id."""
    session_id = f"sess-{job_id}"
    store.create_job(
        IndexingJob(
            job_id=job_id, slug=slug, status=status,
            scanned_count=5, total_count=10, current_file=None,
        )
    )
    store.attach_job_session(job_id, session_id)
    return session_id


def test_indexing_session_end_hook_marks_non_terminal_job_interrupted(tmp_path) -> None:
    """When a non-terminal indexing job's session ends, mark it interrupted.

    This is the defense-in-depth: an infra failure inside a phase
    tool causes the LLM to exit cleanly (done_reason="completed" with an error
    field), leaving the wiki job in a scanning/queued state. The hook promotes
    it to 'interrupted' so JobRecovery picks it up on next restart.
    """
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = SimpleNamespace(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    for status in ("queued", "scanning", "finalizing"):
        job_id = f"job-{status}"
        session_id = _make_non_terminal_job(store, job_id, "org/repo", status)
        hook(session_id, error=None)
        updated = store.get_job(job_id)
        assert updated is not None and updated.status == "interrupted", (
            f"expected 'interrupted' after session end on status={status!r}, "
            f"got {updated.status!r}"
        )


def test_indexing_session_end_hook_no_ops_on_terminal_job(tmp_path) -> None:
    """A terminal job (complete/failed/cancelled) is never touched by the hook."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = SimpleNamespace(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)

    for status in ("complete", "failed", "cancelled"):
        job_id = f"job-terminal-{status}"
        session_id = _make_non_terminal_job(store, job_id, "org/repo", status)
        hook(session_id, error=None)
        updated = store.get_job(job_id)
        assert updated is not None and updated.status == status, (
            f"terminal job with status={status!r} must not be modified"
        )


def test_indexing_session_end_hook_no_ops_on_non_wiki_session(tmp_path) -> None:
    """A session not backing any wiki job is a cheap no-op."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = SimpleNamespace(wiki_store=store)
    hook = WikiIndexingSessionEndHook(runtime)
    # No exception, no side effect.
    hook("sess-unknown", error=None)


# ── ref threading into the rendered indexer query ──────────────────────────────


def test_render_user_query_omits_ref_when_none(submission) -> None:
    """No ref → no ``ref:`` line (byte-identical to prior default-branch queries)."""
    from mewbo_api.wiki.jobs import _render_user_query

    rendered = _render_user_query(submission)
    assert "ref:" not in rendered


def test_render_user_query_includes_ref_when_set(submission) -> None:
    """A chosen ref renders a single ``ref:`` line after the slug."""
    from mewbo_api.wiki.jobs import _render_user_query

    sub = submission.model_copy(update={"ref": "develop"})
    rendered = _render_user_query(sub)
    assert "  ref: develop\n" in rendered
    # Placed between repoUrl/slug and platform so the block reads logically.
    assert rendered.index("ref: develop") < rendered.index("platform:")
    assert rendered.index("slug:") < rendered.index("ref: develop")


# ── auth note derives from durable credential presence (Finder-B B4) ────────────


def test_render_user_query_auth_note_credential_on_file(store, submission):
    """[B4] A durable credential on file → 'credential on file', not 'public
    repo'. Derived from the store, not submission.token — which refresh strips,
    so a private-repo refresh used to render the wrong 'public repo' note."""
    from mewbo_api.wiki.jobs import _render_user_query
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    CredentialStore.save(
        store,
        CredentialScope.from_slug(submission.slug),
        RepoCredential(kind="token", value="ghp_x", username=None),
    )
    rendered = _render_user_query(submission, store=store)
    assert "credential on file" in rendered
    assert "token=null" in rendered


def test_render_user_query_auth_note_none_on_file(store, submission):
    """[B4] No durable credential → 'none on file — public repo assumed', still
    instructing token=null in the note."""
    from mewbo_api.wiki.jobs import _render_user_query

    rendered = _render_user_query(submission, store=store)
    assert "none on file" in rendered
    assert "public repo assumed" in rendered
    assert "token=null" in rendered


def test_render_user_query_auth_note_host_scoped_credential(store):
    """[B4] A HOST-scoped credential (shared across a host's repos) also counts
    as 'on file' — the note mirrors what resolve_chain would find, not just the
    repo scope."""
    from mewbo_api.wiki.jobs import _render_user_query
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    sub = WizardSubmission(
        repoUrl="https://git.example.com/org/repo", slug="git.example.com/org/repo",
        platform="gitea", token=None, depth="comprehensive", language="en",
        model="anthropic/claude-sonnet-4-6", filterMode="exclude", dirs=[], files=[],
    )
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.example.com"),
        RepoCredential(kind="token", value="ghp_host", username=None),
    )
    rendered = _render_user_query(sub, store=store)
    assert "credential on file" in rendered
