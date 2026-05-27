"""Tests for the wiki per-session context resolvers in _ctx.py."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_graph.wiki.types import IndexingJob, QaAnswer

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path)


def _runtime(store):
    """Minimal runtime-like object with a wiki_store attribute."""
    return SimpleNamespace(wiki_store=store)


def _job(job_id: str = "job-ctx", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="queued",
        scanned_count=0,
        total_count=5,
        current_file=None,
    )


def _qa(answer_id: str = "ans-ctx", slug: str = "org/repo") -> QaAnswer:
    return QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=[],
        model="anthropic/claude-sonnet-4-6",
        blocks=[],
        slug=slug,
    )


# ── 1. resolve_job_ctx ────────────────────────────────────────────────────────


def test_resolve_job_ctx_returns_full_context(tmp_path: Path) -> None:
    """A registered job + session yields a WikiJobCtx with all fields populated."""
    from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, resolve_job_ctx

    store = _store(tmp_path)
    job = _job("job-ctx-1", slug="org/myrepo")
    store.create_job(job)
    store.attach_job_session("job-ctx-1", "sess-job-1")

    ctx = resolve_job_ctx("sess-job-1", _runtime(store))

    assert ctx is not None
    assert isinstance(ctx, WikiJobCtx)
    assert ctx.job_id == "job-ctx-1"
    assert ctx.slug == "org/myrepo"
    assert ctx.session_id == "sess-job-1"
    assert isinstance(ctx.clone_dir, Path)
    assert ctx.store is store


def test_resolve_job_ctx_returns_none_for_unknown_session(tmp_path: Path) -> None:
    """Unknown session → resolve_job_ctx returns None (no crash)."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    store = _store(tmp_path)
    ctx = resolve_job_ctx("sess-nonexistent", _runtime(store))
    assert ctx is None


def test_resolve_job_ctx_returns_none_when_no_store(tmp_path: Path) -> None:
    """Missing wiki_store on runtime → returns None gracefully."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    runtime = SimpleNamespace()  # no wiki_store attribute
    ctx = resolve_job_ctx("sess-any", runtime)
    assert ctx is None


# ── 2. resolve_qa_ctx ─────────────────────────────────────────────────────────


def test_resolve_qa_ctx_returns_full_context(tmp_path: Path) -> None:
    """A registered QA answer + session yields a WikiQaCtx with all fields."""
    from mewbo_graph.plugins.wiki._ctx import WikiQaCtx, resolve_qa_ctx

    store = _store(tmp_path)
    answer = _qa("ans-ctx-1", slug="org/myrepo")
    store.save_qa(answer)
    store.attach_qa_session("ans-ctx-1", "sess-qa-1")

    ctx = resolve_qa_ctx("sess-qa-1", _runtime(store))

    assert ctx is not None
    assert isinstance(ctx, WikiQaCtx)
    assert ctx.answer_id == "ans-ctx-1"
    assert ctx.session_id == "sess-qa-1"
    assert ctx.store is store


def test_resolve_qa_ctx_returns_none_for_unknown_session(tmp_path: Path) -> None:
    """Unknown session → resolve_qa_ctx returns None (no crash)."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    store = _store(tmp_path)
    ctx = resolve_qa_ctx("sess-nonexistent", _runtime(store))
    assert ctx is None


def test_resolve_qa_ctx_returns_none_when_no_store(tmp_path: Path) -> None:
    """Missing wiki_store on runtime → returns None gracefully."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    runtime = SimpleNamespace()
    ctx = resolve_qa_ctx("sess-any", runtime)
    assert ctx is None


# ── 3. clone_dir resolution ───────────────────────────────────────────────────


def test_clone_dir_uses_job_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clone_dir is derived from MEWBO_WIKI_CLONE_ROOT env var + job_id."""
    from mewbo_graph.plugins.wiki._ctx import resolve_job_ctx

    store = _store(tmp_path)
    store.create_job(_job("job-cdir", slug="org/r"))
    store.attach_job_session("job-cdir", "sess-cdir")

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    ctx = resolve_job_ctx("sess-cdir", _runtime(store))

    assert ctx is not None
    assert ctx.clone_dir == tmp_path / "clones" / "job-cdir"
