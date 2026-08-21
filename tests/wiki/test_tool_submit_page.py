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


def test_page_aggregate_stays_running_until_every_planned_page_lands(tmp_path: Path) -> None:
    """The first page advances the aggregate rather than completing the phase."""
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    store.create_job(_job("job-pages", "org/repo"))
    store.attach_job_session("job-pages", "sess-pages")
    store.save_job_plan("job-pages", [
        {"id": "one"},
        {"id": "two"},
        {"id": "three"},
    ])
    tool = WikiSubmitPageTool(session_id="sess-pages")

    with patch.object(mod, "_resolve_runtime", return_value=_fake_runtime(store)):
        asyncio.run(tool.handle(_make_action_step(_page_input("one"))))
        job = store.get_job("job-pages")
        assert job is not None and job.progress is not None
        first = job.progress.find("pages.write")
        assert first is not None
        assert (first.state, first.current, first.total) == ("running", 1, 3)

        asyncio.run(tool.handle(_make_action_step(_page_input("two"))))
        asyncio.run(tool.handle(_make_action_step(_page_input("three"))))

    job = store.get_job("job-pages")
    assert job is not None and job.progress is not None
    completed = job.progress.find("pages.write")
    assert completed is not None
    assert (completed.state, completed.current, completed.total) == ("done", 3, 3)


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


# ── Test 2b: a REFRESH counts its own pages ───────────────────────────────────


def test_refresh_counts_pages_the_previous_index_already_wrote(tmp_path: Path) -> None:
    """The verified defect: on a re-index, NOTHING ever counted as new.

    The counter guard asked "does a page with this id exist for the slug", which
    on a refresh is true for every page the previous index wrote. So
    ``pages_submitted`` stayed 0, no ``page_committed`` event fired, and the
    progress bar was dead for the entire re-index of an already-indexed repo.
    """
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    store = _store(tmp_path)
    # A previous, completed index of the SAME slug left its pages behind.
    old = _job("job-old", "org/repo")
    store.create_job(old)
    store.attach_job_session("job-old", "sess-old")
    fresh = _job("job-new", "org/repo")
    store.create_job(fresh)
    store.attach_job_session("job-new", "sess-new")

    runtime = _fake_runtime(store)
    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        old_tool = WikiSubmitPageTool(session_id="sess-old")
        asyncio.run(old_tool.handle(_make_action_step(_page_input("overview", "# v1"))))
        new_tool = WikiSubmitPageTool(session_id="sess-new")
        asyncio.run(new_tool.handle(_make_action_step(_page_input("overview", "# v2"))))

    assert store.get_job_submitted_count("job-new") == 1
    committed = [
        e for e in store.load_job_events("job-new") if e["type"] == "page_committed"
    ]
    assert [e["pageId"] for e in committed] == ["overview"]
    job = store.get_job("job-new")
    assert job is not None and job.pages_submitted == 1
    # The page itself is the new run's content, and the OLD job's count is untouched.
    page = store.get_page("org/repo", "overview")
    assert page is not None and page.body == "# v2"
    assert store.get_job_submitted_count("job-old") == 1


# ── Test 2c: a resumed job does not re-write a page it already wrote ──────────


def test_resume_skips_a_page_this_job_already_wrote(tmp_path: Path) -> None:
    """The per-page skip was ADVISORY PROSE in ``ResumePlan.summary()`` only.

    Nothing enforced it, so a resumed run could regenerate a page it had already
    written — paying for a full page-writer generation to overwrite work that
    was already correct.
    """
    import mewbo_graph.plugins.wiki.submit_page as mod
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool
    from mewbo_graph.wiki.resume import ResumePlan

    store = _store(tmp_path)
    store.create_job(_job("job-res", "org/repo"))
    store.attach_job_session("job-res", "sess-res")

    runtime = _fake_runtime(store)
    tool = WikiSubmitPageTool(session_id="sess-res")
    with patch.object(mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(_make_action_step(_page_input("overview", "# first"))))
        # The run is interrupted and resumed; the plan says this page is done.
        store.save_resume_plan(
            "job-res",
            ResumePlan(pages_done=frozenset({"overview"}), total_pages=1).to_persisted(),
        )
        result = asyncio.run(
            tool.handle(_make_action_step(_page_input("overview", "# regenerated")))
        )

    assert "skipped" in result.content
    page = store.get_page("org/repo", "overview")
    assert page is not None
    assert page.body == "# first"  # the already-correct page is left alone
    assert store.get_job_submitted_count("job-res") == 1


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
