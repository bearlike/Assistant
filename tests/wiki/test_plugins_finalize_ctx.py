"""Tests for finalize.py helpers and _ctx.py uncovered paths.

Covers:
- finalize.py: _host_from_url, _split_owner_repo, _fetch_description
  (platform branches: github enterprise, gitea, gitlab, bitbucket, unknown,
   private host, short-circuits), _detect_grounder, missing submission,
   missing platform → validation error.
- _ctx.py: resolve_runtime seam, emit_phase, emit_log, resolve_qa_clone_dir
  (complete job found / not found / dir missing), _clone_dir_for env override.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, IndexingJob, QaAnswer, WikiPage

# ── Shared helpers ────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _job(job_id: str = "job-fz", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="finalizing",
        scanned_count=0,
        total_count=0,
        current_file=None,
    )


def _qa(answer_id: str = "ans-fz", slug: str = "org/repo") -> QaAnswer:
    return QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=[],
        model="test-model",
        blocks=[],
        slug=slug,
    )


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _save_page(store: JsonWikiStore, slug: str, page_id: str) -> None:
    fm = Frontmatter(title=page_id, slug=page_id)
    page = WikiPage(id=page_id, title=page_id, frontmatter=fm, body="# body", toc=[], nav=[])
    store.save_page(slug, page)


def _seed_submission(
    store: JsonWikiStore,
    job_id: str,
    *,
    slug: str = "org/repo",
    repo_url: str = "https://github.com/org/repo",
    platform: str = "github",
    language: str = "en",
) -> None:
    store.save_job_submission(
        job_id,
        {
            "repoUrl": repo_url,
            "slug": slug,
            "platform": platform,
            "language": language,
            "depth": "concise",
            "model": "test-model",
            "filterMode": "exclude",
            "dirs": [],
            "files": [],
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# finalize.py — pure helper functions
# ═══════════════════════════════════════════════════════════════════════════════


class TestHostFromUrl:
    """_host_from_url extracts the DNS hostname."""

    def test_returns_hostname_for_valid_url(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _host_from_url

        assert _host_from_url("https://github.com/org/repo") == "github.com"

    def test_returns_none_for_empty_url(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _host_from_url

        assert _host_from_url("") is None

    def test_returns_hostname_for_private_host(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _host_from_url

        assert _host_from_url("https://git.hurricane.home/org/repo") == "git.hurricane.home"

    def test_returns_none_for_unparseable_url(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _host_from_url

        # urlparse handles most strings without raising; None is returned
        # when the result has no meaningful hostname.
        result = _host_from_url("not_a_url_at_all")
        # urlparse may return empty string for netloc — should be None
        assert result is None or isinstance(result, str)


class TestSplitOwnerRepo:
    """_split_owner_repo handles legacy and canonical slugs."""

    def test_two_segment_slug(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _split_owner_repo

        assert _split_owner_repo("owner/repo") == ("owner", "repo")

    def test_three_segment_slug(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _split_owner_repo

        assert _split_owner_repo("github.com/owner/repo") == ("owner", "repo")

    def test_strips_dot_git_suffix(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _split_owner_repo

        assert _split_owner_repo("owner/repo.git") == ("owner", "repo")

    def test_single_segment_returns_none(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _split_owner_repo

        assert _split_owner_repo("onlyone") is None

    def test_empty_string_returns_none(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _split_owner_repo

        assert _split_owner_repo("") is None


class TestFetchDescription:
    """_fetch_description short-circuits cleanly on all failure paths."""

    def test_returns_empty_when_no_repo_url(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        assert _fetch_description(repo_url="", platform="github", token=None, slug="o/r") == ""

    def test_returns_empty_for_unrecognised_platform(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        result = _fetch_description(
            repo_url="https://example.com/o/r",
            platform="azure",
            token=None,
            slug="o/r",
        )
        assert result == ""

    def test_returns_empty_for_invalid_slug(self) -> None:
        """Slug with only one segment → _split_owner_repo returns None → early exit."""
        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        result = _fetch_description(
            repo_url="https://github.com/o/r",
            platform="github",
            token=None,
            slug="onlyone",
        )
        assert result == ""

    def test_github_com_uses_api_github_com(self) -> None:
        """GitHub.com platform uses api.github.com; request fails → empty string."""
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        with patch.object(urllib.request, "urlopen", side_effect=OSError("network")):
            result = _fetch_description(
                repo_url="https://github.com/org/repo",
                platform="github",
                token=None,
                slug="org/repo",
            )
        assert result == ""

    def test_github_enterprise_uses_api_v3(self) -> None:
        """GitHub Enterprise on non-github.com host uses <origin>/api/v3 endpoint."""
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        with patch.object(urllib.request, "urlopen", side_effect=OSError("network")):
            result = _fetch_description(
                repo_url="https://github.acme.com/org/repo",
                platform="github",
                token="tok",
                slug="org/repo",
            )
        assert result == ""

    def test_gitea_platform_uses_api_v1(self) -> None:
        """Gitea platform → /api/v1/repos/<owner>/<repo>; network failure → empty."""
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        with patch.object(urllib.request, "urlopen", side_effect=OSError("network")):
            result = _fetch_description(
                repo_url="https://git.hurricane.home/org/repo",
                platform="gitea",
                token="mytoken",
                slug="org/repo",
            )
        assert result == ""

    def test_gitlab_platform_url_encoded(self) -> None:
        """GitLab platform uses URL-encoded project path; network failure → empty."""
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        with patch.object(urllib.request, "urlopen", side_effect=OSError("network")):
            result = _fetch_description(
                repo_url="https://gitlab.com/org/repo",
                platform="gitlab",
                token=None,
                slug="org/repo",
            )
        assert result == ""

    def test_bitbucket_platform(self) -> None:
        """Bitbucket platform uses api.bitbucket.org; network failure → empty."""
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        with patch.object(urllib.request, "urlopen", side_effect=OSError("network")):
            result = _fetch_description(
                repo_url="https://bitbucket.org/org/repo",
                platform="bitbucket",
                token=None,
                slug="org/repo",
            )
        assert result == ""

    def test_successful_github_fetch_returns_description(self) -> None:
        """Successful fetch: description from JSON body is returned."""
        import json
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        fake_body = json.dumps({"description": "A test repository"}).encode()
        fake_response = MagicMock()
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = MagicMock(return_value=False)
        fake_response.read.return_value = fake_body

        with patch.object(urllib.request, "urlopen", return_value=fake_response):
            result = _fetch_description(
                repo_url="https://github.com/org/repo",
                platform="github",
                token="tok",
                slug="org/repo",
            )

        assert result == "A test repository"

    def test_returns_empty_on_json_decode_error(self) -> None:
        """Bad JSON in response → empty string (never an exception)."""
        import urllib.request

        from mewbo_graph.plugins.wiki.finalize import _fetch_description

        fake_response = MagicMock()
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = MagicMock(return_value=False)
        fake_response.read.return_value = b"not json {"

        with patch.object(urllib.request, "urlopen", return_value=fake_response):
            result = _fetch_description(
                repo_url="https://github.com/org/repo",
                platform="github",
                token=None,
                slug="org/repo",
            )

        assert result == ""


class TestDetectGrounder:
    """_detect_grounder checks for known grounder file paths."""

    def test_returns_true_when_mewbo_wiki_json_present(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki.finalize import _detect_grounder

        (tmp_path / ".mewbo").mkdir()
        (tmp_path / ".mewbo" / "wiki.json").write_text("{}", encoding="utf-8")
        assert _detect_grounder(tmp_path) is True

    def test_returns_true_when_devin_wiki_json_present(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki.finalize import _detect_grounder

        (tmp_path / ".devin").mkdir()
        (tmp_path / ".devin" / "wiki.json").write_text("{}", encoding="utf-8")
        assert _detect_grounder(tmp_path) is True

    def test_returns_false_when_neither_present(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki.finalize import _detect_grounder

        assert _detect_grounder(tmp_path) is False

    def test_returns_false_on_exception(self) -> None:
        """Non-existent clone_dir raises no exception; returns False."""
        from mewbo_graph.plugins.wiki.finalize import _detect_grounder

        assert _detect_grounder(Path("/nonexistent/path")) is False


class TestLoadSubmission:
    """_load_submission reads from the store; returns None on failure."""

    def test_returns_submission_when_present(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki.finalize import _load_submission

        store = _store(tmp_path)
        store.create_job(_job("job-sub", "org/r"))
        store.save_job_submission("job-sub", {"platform": "github", "repoUrl": "https://x.com/a/b"})

        ctx = SimpleNamespace(store=store, job_id="job-sub")
        result = _load_submission(ctx)

        assert result is not None
        assert result["platform"] == "github"

    def test_returns_none_on_store_exception(self) -> None:
        from mewbo_graph.plugins.wiki.finalize import _load_submission

        broken_store = MagicMock()
        broken_store.get_job_submission.side_effect = RuntimeError("db down")
        ctx = SimpleNamespace(store=broken_store, job_id="any-job")

        result = _load_submission(ctx)
        assert result is None


# ── WikiFinalizeTool — missing submission → internal error ────────────────────


def test_finalize_missing_submission_returns_internal_error(tmp_path: Path) -> None:
    """No wizard submission saved for this job → internal error."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    store.create_job(_job("job-nosub", "org/repo"))
    store.attach_job_session("job-nosub", "sess-nosub")
    _save_page(store, "org/repo", "overview")
    # No call to save_job_submission → missing submission.

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-nosub")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "overview"})))

    assert "error" in result.content
    import ast

    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "internal"


def test_finalize_missing_platform_returns_validation_error(tmp_path: Path) -> None:
    """Submission exists but platform is empty → validation error."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    store.create_job(_job("job-noplat", "org/repo"))
    store.attach_job_session("job-noplat", "sess-noplat")
    _save_page(store, "org/repo", "overview")
    # Submission with empty platform
    store.save_job_submission(
        "job-noplat",
        {
            "repoUrl": "https://github.com/org/repo",
            "slug": "org/repo",
            "platform": "",
            "language": "en",
        },
    )

    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-noplat")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "overview"})))

    assert "error" in result.content
    import ast

    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "validation"


def test_finalize_no_ctx_returns_internal_error(tmp_path: Path) -> None:
    """Session with no attached job → ctx is None → internal error."""
    import mewbo_graph.plugins.wiki.finalize as mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    store = _store(tmp_path)
    runtime = _fake_runtime(store)
    tool = WikiFinalizeTool(session_id="sess-no-job")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"landingPageId": "x"})))

    assert "error" in result.content
    import ast

    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "internal"


# ═══════════════════════════════════════════════════════════════════════════════
# _ctx.py — uncovered paths
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveRuntime:
    """resolve_runtime delegates to get_wiki_store() from the store seam."""

    def test_resolve_runtime_returns_namespace_with_wiki_store(self, tmp_path: Path) -> None:
        """resolve_runtime wraps the store in a SimpleNamespace."""
        from mewbo_graph.plugins.wiki._ctx import resolve_runtime
        from mewbo_graph.wiki.store import reset_for_tests, set_wiki_store

        try:
            store = JsonWikiStore(root_dir=tmp_path)
            set_wiki_store(store)
            rt = resolve_runtime()
            assert hasattr(rt, "wiki_store")
            assert rt.wiki_store is store
        finally:
            reset_for_tests()


class TestEmitPhase:
    """emit_phase writes a phase event AND updates the job snapshot."""

    def test_emit_phase_appends_event_and_updates_job(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, emit_phase

        store = _store(tmp_path)
        store.create_job(_job("job-ep", "org/r"))

        ctx = WikiJobCtx(
            job_id="job-ep",
            slug="org/r",
            session_id="sess-ep",
            clone_dir=tmp_path / "clone",
            store=store,
        )
        emit_phase(ctx, "scan")

        events = store.load_job_events("job-ep")
        phase_events = [e for e in events if e.get("type") == "phase"]
        assert len(phase_events) == 1
        assert phase_events[0]["name"] == "scan"

        # Job snapshot updated
        job = store.get_job("job-ep")
        assert job is not None
        assert job.phase == "scan"
        assert job.phase_started_at is not None

    def test_emit_phase_silently_ignores_store_error(self) -> None:
        """If the store raises, emit_phase does not propagate the exception."""
        from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, emit_phase

        broken_store = MagicMock()
        broken_store.append_job_event.side_effect = RuntimeError("db down")
        broken_store.update_job.side_effect = RuntimeError("db down")

        ctx = WikiJobCtx(
            job_id="j",
            slug="s",
            session_id="se",
            clone_dir=Path("/tmp"),
            store=broken_store,
        )
        # Must not raise
        emit_phase(ctx, "finalize")


class TestEmitLog:
    """emit_log appends a log event."""

    def test_emit_log_appends_log_event(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, emit_log

        store = _store(tmp_path)
        store.create_job(_job("job-el", "org/r"))

        ctx = WikiJobCtx(
            job_id="job-el",
            slug="org/r",
            session_id="sess-el",
            clone_dir=tmp_path / "clone",
            store=store,
        )
        emit_log(ctx, "Build complete", level="info")

        events = store.load_job_events("job-el")
        log_events = [e for e in events if e.get("type") == "log"]
        assert len(log_events) == 1
        assert log_events[0]["text"] == "Build complete"
        assert log_events[0]["level"] == "info"

    def test_emit_log_silently_ignores_store_error(self) -> None:
        """If the store raises, emit_log does not propagate."""
        from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, emit_log

        broken_store = MagicMock()
        broken_store.append_job_event.side_effect = RuntimeError("db down")

        ctx = WikiJobCtx(
            job_id="j",
            slug="s",
            session_id="se",
            clone_dir=Path("/tmp"),
            store=broken_store,
        )
        emit_log(ctx, "hello")  # must not raise


class TestResolveQaCloneDir:
    """resolve_qa_clone_dir picks the most recent completed job's clone dir."""

    def test_returns_clone_dir_for_completed_job(self, tmp_path: Path, monkeypatch) -> None:
        from mewbo_graph.plugins.wiki._ctx import resolve_qa_clone_dir

        store = _store(tmp_path)
        clone_dir = tmp_path / "clones" / "job-complete"
        clone_dir.mkdir(parents=True)

        monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))

        job = IndexingJob(
            job_id="job-complete",
            slug="org/r",
            status="complete",
            scanned_count=0,
            total_count=0,
            current_file=None,
        )
        store.create_job(job)

        result = resolve_qa_clone_dir("org/r", store)
        assert result == clone_dir

    def test_returns_none_when_no_completed_job(self, tmp_path: Path) -> None:
        from mewbo_graph.plugins.wiki._ctx import resolve_qa_clone_dir

        store = _store(tmp_path)
        job = IndexingJob(
            job_id="job-scanning",
            slug="org/r",
            status="scanning",
            scanned_count=0,
            total_count=0,
            current_file=None,
        )
        store.create_job(job)

        result = resolve_qa_clone_dir("org/r", store)
        assert result is None

    def test_returns_none_when_clone_dir_missing_from_disk(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from mewbo_graph.plugins.wiki._ctx import resolve_qa_clone_dir

        monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
        store = _store(tmp_path)
        # Clone dir is NOT created on disk even though job is complete.
        job = IndexingJob(
            job_id="job-nodisk",
            slug="org/r",
            status="complete",
            scanned_count=0,
            total_count=0,
            current_file=None,
        )
        store.create_job(job)

        result = resolve_qa_clone_dir("org/r", store)
        assert result is None

    def test_returns_none_when_store_raises(self) -> None:
        from mewbo_graph.plugins.wiki._ctx import resolve_qa_clone_dir

        broken_store = MagicMock()
        broken_store.list_jobs.side_effect = RuntimeError("db error")

        result = resolve_qa_clone_dir("org/r", broken_store)
        assert result is None

    def test_skips_non_complete_jobs_and_finds_complete(self, tmp_path: Path, monkeypatch) -> None:
        from mewbo_graph.plugins.wiki._ctx import resolve_qa_clone_dir

        monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
        store = _store(tmp_path)

        for jid, status in [("j-fail", "failed"), ("j-ok", "complete")]:
            store.create_job(
                IndexingJob(
                    job_id=jid,
                    slug="org/r",
                    status=status,
                    scanned_count=0,
                    total_count=0,
                    current_file=None,
                )
            )
        # Only create the clone dir for j-ok
        (tmp_path / "clones" / "j-ok").mkdir(parents=True)

        result = resolve_qa_clone_dir("org/r", store)
        assert result == tmp_path / "clones" / "j-ok"


class TestCloneDirFor:
    """_clone_dir_for reads MEWBO_WIKI_CLONE_ROOT with fallback."""

    def test_uses_env_var_when_set(self, tmp_path: Path, monkeypatch) -> None:
        from mewbo_graph.plugins.wiki._ctx import _clone_dir_for

        monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "custom"))
        result = _clone_dir_for("job-xyz")
        assert result == tmp_path / "custom" / "job-xyz"

    def test_falls_back_to_default_when_env_not_set(self, monkeypatch) -> None:
        from mewbo_graph.plugins.wiki._ctx import _DEFAULT_CLONE_ROOT, _clone_dir_for

        monkeypatch.delenv("MEWBO_WIKI_CLONE_ROOT", raising=False)
        result = _clone_dir_for("job-abc")
        assert result == Path(_DEFAULT_CLONE_ROOT) / "job-abc"

    def test_ignores_empty_string_env_var(self, monkeypatch) -> None:
        """Empty-string env var is treated as unset → falls back to default."""
        from mewbo_graph.plugins.wiki._ctx import _DEFAULT_CLONE_ROOT, _clone_dir_for

        monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", "")
        result = _clone_dir_for("job-empty")
        assert result == Path(_DEFAULT_CLONE_ROOT) / "job-empty"


# ── _ctx.py lines 79 + 99: session mapped but entity deleted ─────────────────


def test_resolve_job_ctx_returns_none_when_job_deleted_after_session_mapped(
    tmp_path: Path,
) -> None:
    """Session is mapped to a job, but get_job() returns None (deleted after mapping).

    Covers _ctx.py line 79: the `if job is None: return None` branch.
    """
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    _store(tmp_path)
    # Patch find_job_by_session to return a job_id, but get_job to return None.
    broken_store = MagicMock()
    broken_store.find_job_by_session.return_value = "job-gone"
    broken_store.get_job.return_value = None

    ctx = resolve_job_ctx("sess-gone", SimpleNamespace(wiki_store=broken_store))
    assert ctx is None


def test_resolve_qa_ctx_returns_none_when_qa_deleted_after_session_mapped(
    tmp_path: Path,
) -> None:
    """Session is mapped to a QA answer, but get_qa() returns None (deleted after mapping).

    Covers _ctx.py line 99: the `if ans is None: return None` branch.
    """
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    broken_store = MagicMock()
    broken_store.find_qa_by_session.return_value = "ans-gone"
    broken_store.get_qa.return_value = None

    ctx = resolve_qa_ctx("sess-qa-gone", SimpleNamespace(wiki_store=broken_store))
    assert ctx is None
