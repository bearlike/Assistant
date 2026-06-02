"""Tests for WikiCommitPlanTool — TDD: tests written first."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import IndexingJob

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-cp", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="scanning",
        scanned_count=5,
        total_count=10,
        current_file=None,
    )


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _two_page_plan() -> list[dict]:
    return [
        {"id": "overview", "title": "Overview", "description": "High-level overview"},
        {"id": "architecture", "title": "Architecture", "description": "System design"},
    ]


# ── Test 1: plan persisted + finalizing event emitted ─────────────────────────


def test_commit_plan_persists_plan_and_emits_finalizing(tmp_path: Path) -> None:
    """A 2-page plan is saved; a finalizing event with correct counts is emitted."""
    import mewbo_core.builtin_plugins.wiki.commit_plan as mod
    from mewbo_core.builtin_plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    job = _job("job-cp1", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-cp1", "sess-cp1")

    runtime = _fake_runtime(store)
    tool = WikiCommitPlanTool(session_id="sess-cp1")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"pages": _two_page_plan()})))

    # No error in result
    assert "error" not in result.content
    assert "committed" in result.content

    # Finalizing event emitted
    events = store.load_job_events("job-cp1")
    finalizing = [e for e in events if e["type"] == "finalizing"]
    assert len(finalizing) == 1
    ev = finalizing[0]
    assert ev["scannedCount"] == job.scanned_count
    assert ev["totalCount"] == job.total_count

    # Plan is persisted and retrievable
    saved = store.get_job_plan("job-cp1")
    assert saved is not None
    assert len(saved) == 2
    assert saved[0]["id"] == "overview"
    assert saved[1]["id"] == "architecture"


# ── Test 2: empty pages list → validation error, no event ─────────────────────


def test_commit_plan_empty_returns_validation_error(tmp_path: Path) -> None:
    """Empty pages list → validation error; no event is appended."""
    import mewbo_core.builtin_plugins.wiki.commit_plan as mod
    from mewbo_core.builtin_plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    job = _job("job-cp2", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-cp2", "sess-cp2")

    runtime = _fake_runtime(store)
    tool = WikiCommitPlanTool(session_id="sess-cp2")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"pages": []})))

    assert "validation" in result.content

    # No events appended
    events = store.load_job_events("job-cp2")
    assert len(events) == 0


# ── Test 3: second call overwrites the first plan ─────────────────────────────


def test_commit_plan_overwrites_previous_plan(tmp_path: Path) -> None:
    """Calling commit_plan twice: the second call's plan wins."""
    import mewbo_core.builtin_plugins.wiki.commit_plan as mod
    from mewbo_core.builtin_plugins.wiki.commit_plan import WikiCommitPlanTool

    store = _store(tmp_path)
    job = _job("job-cp3", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-cp3", "sess-cp3")

    runtime = _fake_runtime(store)
    tool = WikiCommitPlanTool(session_id="sess-cp3")

    first_plan = [{"id": "intro", "title": "Intro", "description": ""}]
    second_plan = [
        {"id": "alpha", "title": "Alpha", "description": ""},
        {"id": "beta", "title": "Beta", "description": ""},
        {"id": "gamma", "title": "Gamma", "description": ""},
    ]

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(_make_action_step({"pages": first_plan})))
        asyncio.run(tool.handle(_make_action_step({"pages": second_plan})))

    saved = store.get_job_plan("job-cp3")
    assert saved is not None
    assert len(saved) == 3
    assert saved[0]["id"] == "alpha"
