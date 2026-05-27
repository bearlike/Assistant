"""Tests for WikiListPagesTool — wiki_list_pages tool (list_pages.py)."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Frontmatter, QaAnswer, WikiPage

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _save_page(store: JsonWikiStore, slug: str, page_id: str, title: str) -> None:
    fm = Frontmatter(title=title, slug=page_id)
    page = WikiPage(id=page_id, title=title, frontmatter=fm, body="# body", toc=[], nav=[])
    store.save_page(slug, page)


def _qa(answer_id: str = "ans-lp", slug: str = "org/repo") -> QaAnswer:
    return QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=[],
        model="test-model",
        blocks=[],
        slug=slug,
    )


def _run_list_pages(
    store: JsonWikiStore,
    tool_input: dict,
    *,
    session_id: str = "sess-lp",
) -> MagicMock:
    """Run WikiListPagesTool.handle() with a fake runtime, return the result."""
    import mewbo_graph.plugins.wiki.list_pages as lp_mod
    from mewbo_graph.plugins.wiki.list_pages import WikiListPagesTool

    runtime = _fake_runtime(store)
    tool = WikiListPagesTool(session_id=session_id)

    with patch.object(lp_mod, "_resolve_runtime", return_value=runtime):
        return asyncio.run(tool.handle(_make_action_step(tool_input)))


# ── Test 1: returns all pages when no filter ────────────────────────────────


def test_list_pages_returns_all_pages_when_no_filter(tmp_path: Path) -> None:
    """Store 3 pages for slug, list with no filter — all 3 appear."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.save_qa(_qa("ans-1", slug=slug))
    store.attach_qa_session("ans-1", "sess-lp1")

    for pid, title in [("overview", "Overview"), ("api", "API Reference"), ("guide", "Guide")]:
        _save_page(store, slug, pid, title)

    result = _run_list_pages(store, {}, session_id="sess-lp1")

    assert "error" not in result.content
    payload = ast.literal_eval(result.content)
    assert payload["count"] == 3
    page_titles = {r["title"] for r in payload["pages"]}
    assert "Overview" in page_titles
    assert "API Reference" in page_titles
    assert "Guide" in page_titles


# ── Test 2: title_contains filters results ──────────────────────────────────


def test_list_pages_filter_by_title_contains(tmp_path: Path) -> None:
    """title_contains='auth' returns only pages whose title includes 'auth'."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.save_qa(_qa("ans-2", slug=slug))
    store.attach_qa_session("ans-2", "sess-lp2")

    for pid, title in [
        ("auth", "Authentication"),
        ("authz", "Authorization"),
        ("overview", "Overview"),
    ]:
        _save_page(store, slug, pid, title)

    result = _run_list_pages(store, {"title_contains": "auth"}, session_id="sess-lp2")

    payload = ast.literal_eval(result.content)
    assert payload["count"] == 2
    titles = {r["title"] for r in payload["pages"]}
    assert "Overview" not in titles
    assert "Authentication" in titles
    assert "Authorization" in titles


# ── Test 3: title_contains is case-insensitive ──────────────────────────────


def test_list_pages_filter_is_case_insensitive(tmp_path: Path) -> None:
    """title_contains='AUTH' matches 'Authentication' (case-insensitive)."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.save_qa(_qa("ans-3", slug=slug))
    store.attach_qa_session("ans-3", "sess-lp3")

    _save_page(store, slug, "auth", "Authentication")
    _save_page(store, slug, "guide", "Guide")

    result = _run_list_pages(store, {"title_contains": "AUTH"}, session_id="sess-lp3")

    payload = ast.literal_eval(result.content)
    assert payload["count"] == 1
    assert payload["pages"][0]["title"] == "Authentication"


# ── Test 4: results sorted alphabetically by title ──────────────────────────


def test_list_pages_result_sorted_by_title(tmp_path: Path) -> None:
    """Returned pages are alphabetically sorted by title."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.save_qa(_qa("ans-4", slug=slug))
    store.attach_qa_session("ans-4", "sess-lp4")

    for pid, title in [("z", "Zebra"), ("a", "Apple"), ("m", "Mango")]:
        _save_page(store, slug, pid, title)

    result = _run_list_pages(store, {}, session_id="sess-lp4")

    payload = ast.literal_eval(result.content)
    titles = [r["title"] for r in payload["pages"]]
    assert titles == sorted(titles, key=str.lower)


# ── Test 5: empty store returns empty list ──────────────────────────────────


def test_list_pages_returns_empty_when_no_pages(tmp_path: Path) -> None:
    """No pages saved for slug → count=0, pages=[]."""
    store = _store(tmp_path)
    slug = "org/empty"
    store.save_qa(_qa("ans-5", slug=slug))
    store.attach_qa_session("ans-5", "sess-lp5")

    result = _run_list_pages(store, {}, session_id="sess-lp5")

    payload = ast.literal_eval(result.content)
    assert payload["count"] == 0
    assert payload["pages"] == []


# ── Test 6: no QA ctx → internal error ─────────────────────────────────────


def test_list_pages_returns_error_when_no_ctx(tmp_path: Path) -> None:
    """Session not attached to any QA answer → error with code=internal."""
    store = _store(tmp_path)
    # Store is empty — no QA answer registered for this session.
    result = _run_list_pages(store, {}, session_id="sess-unknown")

    assert "error" in result.content
    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "internal"


# ── Test 7: validation error on unknown arg ─────────────────────────────────


def test_list_pages_validation_error_on_unknown_arg(tmp_path: Path) -> None:
    """Passing an extra field not in the schema → validation error."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.save_qa(_qa("ans-7", slug=slug))
    store.attach_qa_session("ans-7", "sess-lp7")

    result = _run_list_pages(store, {"bogus": "field"}, session_id="sess-lp7")

    assert "error" in result.content
    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "validation"


# ── Test 8: pageId is exposed in payload ────────────────────────────────────


def test_list_pages_payload_includes_page_id(tmp_path: Path) -> None:
    """Each item in pages includes both pageId and title."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.save_qa(_qa("ans-8", slug=slug))
    store.attach_qa_session("ans-8", "sess-lp8")

    _save_page(store, slug, "my-page-id", "My Page Title")

    result = _run_list_pages(store, {}, session_id="sess-lp8")

    payload = ast.literal_eval(result.content)
    assert payload["count"] == 1
    entry = payload["pages"][0]
    assert entry["pageId"] == "my-page-id"
    assert entry["title"] == "My Page Title"


# ── Test 9: title_contains with whitespace stripped ─────────────────────────


@pytest.mark.parametrize("needle", ["  auth  ", "auth", "AUTH"])
def test_list_pages_strips_and_lowercases_needle(tmp_path: Path, needle: str) -> None:
    """Needle with surrounding whitespace or mixed case still matches correctly."""
    store = _store(tmp_path)
    slug = "org/repo"
    qa_id = f"ans-strip-{needle.strip()}"
    sess_id = f"sess-strip-{needle.strip()}"
    store.save_qa(_qa(qa_id, slug=slug))
    store.attach_qa_session(qa_id, sess_id)

    _save_page(store, slug, "auth-guide", "Auth Guide")
    _save_page(store, slug, "overview", "Overview")

    result = _run_list_pages(store, {"title_contains": needle}, session_id=sess_id)

    payload = ast.literal_eval(result.content)
    assert payload["count"] == 1
    assert payload["pages"][0]["title"] == "Auth Guide"


# ── Test 10: store exception in list_pages → internal error (line 64-65) ────


def test_list_pages_returns_error_when_store_list_pages_raises(tmp_path: Path) -> None:
    """If ctx.store.list_pages() raises, WikiListPagesTool returns internal error."""
    import mewbo_graph.plugins.wiki.list_pages as lp_mod
    from mewbo_graph.plugins.wiki.list_pages import WikiListPagesTool
    from mewbo_graph.wiki.types import QaAnswer

    store = _store(tmp_path)
    slug = "org/fail"
    qa = QaAnswer(
        answer_id="ans-fail",
        from_page_id="x",
        summary_sources=[],
        model="test",
        blocks=[],
        slug=slug,
    )
    store.save_qa(qa)
    store.attach_qa_session("ans-fail", "sess-fail-store")

    # Replace the store's list_pages with one that raises
    broken_store = MagicMock(wraps=store)
    broken_store.find_qa_by_session.return_value = "ans-fail"
    broken_store.get_qa.return_value = qa
    broken_store.list_pages.side_effect = RuntimeError("db exploded")

    runtime = SimpleNamespace(wiki_store=broken_store)
    tool = WikiListPagesTool(session_id="sess-fail-store")

    with patch.object(lp_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({})))

    assert "error" in result.content
    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "internal"
