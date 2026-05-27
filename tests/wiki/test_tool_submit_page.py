"""Tests for WikiSubmitPageTool — TDD: tests written first."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-sp", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="finalizing",
        scanned_count=10,
        total_count=10,
        current_file=None,
    )


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _page_input(page_id: str = "overview", body: str = "# Overview\n\nBody text.") -> dict:
    return {
        "pageId": page_id,
        "frontmatter": {
            "title": "Overview",
            "slug": page_id,
        },
        "body": body,
    }


# ── Test 1: submit 2 pages → counter = 2, both readable ───────────────────────


def test_submit_page_persists_and_increments(tmp_path: Path) -> None:
    """Submit 2 different pages → counter = 2; both are readable via store.get_page."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    job = _job("job-sp1", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-sp1", "sess-sp1")

    runtime = _fake_runtime(store)
    tool = WikiSubmitPageTool(session_id="sess-sp1")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        res1 = asyncio.run(tool.handle(_make_action_step(_page_input("overview"))))
        arch_input = _page_input("architecture", "# Arch\n\nDetails.")
        res2 = asyncio.run(tool.handle(_make_action_step(arch_input)))

    # No errors
    assert "error" not in res1.content
    assert "error" not in res2.content

    # Pages readable
    pg1 = store.get_page("org/repo", "overview")
    assert pg1 is not None
    assert pg1.body == "# Overview\n\nBody text."

    pg2 = store.get_page("org/repo", "architecture")
    assert pg2 is not None
    assert pg2.body == "# Arch\n\nDetails."

    # Counter = 2
    count = store.get_job_submitted_count("job-sp1")
    assert count == 2

    # Result includes pages_total
    assert "pages_total" in res2.content


# ── Test 2: re-submit same pageId does not increment counter ──────────────────


def test_submit_page_overwrites_does_not_increment(tmp_path: Path) -> None:
    """Submit pageId='x' twice → counter = 1; second body wins."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    job = _job("job-sp2", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-sp2", "sess-sp2")

    runtime = _fake_runtime(store)
    tool = WikiSubmitPageTool(session_id="sess-sp2")

    first_body = "# First body"
    second_body = "# Second body (updated)"

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(_make_action_step(_page_input("readme", first_body))))
        asyncio.run(tool.handle(_make_action_step(_page_input("readme", second_body))))

    # Counter should be 1 (idempotent re-submit)
    count = store.get_job_submitted_count("job-sp2")
    assert count == 1

    # Second body wins
    pg = store.get_page("org/repo", "readme")
    assert pg is not None
    assert pg.body == second_body


# ── Test 3: invalid pageId format ─────────────────────────────────────────────


def test_submit_page_validates_pageid_format(tmp_path: Path) -> None:
    """pageId with '/' or '_' is rejected with a validation error."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    job = _job("job-sp3", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-sp3", "sess-sp3")

    runtime = _fake_runtime(store)
    tool = WikiSubmitPageTool(session_id="sess-sp3")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        # slash in pageId
        res_slash = asyncio.run(tool.handle(_make_action_step({
            "pageId": "foo/bar",
            "frontmatter": {"title": "T", "slug": "foo/bar"},
            "body": "body",
        })))
        # underscore in pageId
        res_under = asyncio.run(tool.handle(_make_action_step({
            "pageId": "foo_bar",
            "frontmatter": {"title": "T", "slug": "foo_bar"},
            "body": "body",
        })))

    assert "validation" in res_slash.content
    assert "validation" in res_under.content

    # Nothing persisted
    assert store.get_job_submitted_count("job-sp3") == 0


# ── Test 4: empty body → validation error ─────────────────────────────────────


def test_submit_page_validates_body_nonempty(tmp_path: Path) -> None:
    """Empty body → validation error; nothing persisted."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    job = _job("job-sp4", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-sp4", "sess-sp4")

    runtime = _fake_runtime(store)
    tool = WikiSubmitPageTool(session_id="sess-sp4")

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({
            "pageId": "intro",
            "frontmatter": {"title": "Intro", "slug": "intro"},
            "body": "",
        })))

    assert "validation" in result.content
    assert store.get_job_submitted_count("job-sp4") == 0


# ── Test 5: mermaid fences preserved verbatim ─────────────────────────────────


def test_submit_page_preserves_mermaid_fences(tmp_path: Path) -> None:
    """Body with a mermaid fence is saved verbatim without modification."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    job = _job("job-sp5", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-sp5", "sess-sp5")

    runtime = _fake_runtime(store)
    tool = WikiSubmitPageTool(session_id="sess-sp5")

    mermaid_body = "# Diagram\n\n```mermaid\nflowchart TD\n    A --> B\n    B --> C\n```\n"

    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({
            "pageId": "diagram-page",
            "frontmatter": {"title": "Diagram", "slug": "diagram-page"},
            "body": mermaid_body,
        })))

    assert "error" not in result.content

    pg = store.get_page("org/repo", "diagram-page")
    assert pg is not None
    assert "```mermaid" in pg.body
    assert "flowchart TD" in pg.body
    assert pg.body == mermaid_body
