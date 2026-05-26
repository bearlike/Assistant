"""QA tools tests."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import (
    Embedding,
    GraphNode,
    QaAnswer,
    WikiPage,
)
from mewbo_core.builtin_plugins.wiki import (
    code_search as code_search_mod,
    emit_block as emit_block_mod,
    read_page as read_page_mod,
    search_pages as search_pages_mod,
)
from mewbo_core.builtin_plugins.wiki.code_search import WikiCodeSearchTool
from mewbo_core.builtin_plugins.wiki.emit_block import WikiEmitBlockTool
from mewbo_core.builtin_plugins.wiki.read_page import WikiReadPageTool
from mewbo_core.builtin_plugins.wiki.search_pages import WikiSearchPagesTool


@pytest.fixture
def qa_setup(tmp_path):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    # Seed an answer + session.
    ans = QaAnswer(
        answerId="a1",
        fromPageId="overview",
        summarySources=[],
        model="anthropic/claude-sonnet-4-6",
        blocks=[],
        slug="x/y",
    )
    store.save_qa(ans)
    store.attach_qa_session("a1", "sess-qa-1")
    # Seed some pages + graph for retrieval.
    store.save_page("x/y", WikiPage(
        id="auth", title="Auth",
        frontmatter={"title": "Auth", "slug": "auth"},
        body="Tokens, sessions, login flow.",
        toc=[], nav=[],
    ))
    store.save_page("x/y", WikiPage(
        id="overview", title="Overview",
        frontmatter={"title": "Overview", "slug": "overview"},
        body="System mechanics overview.",
        toc=[], nav=[],
    ))
    store.upsert_nodes("x/y", [
        GraphNode(
            slug="x/y",
            node_id="f1",
            type="Function",
            name="authenticate",
            file="auth.py",
            range=(0, 100),
            docstring="Verify token.",
        ),
    ])
    store.upsert_embeddings("x/y", [
        Embedding(slug="x/y", node_id="f1", vector=[1.0, 0.0], model="m", dim=2),
    ])
    return store, "sess-qa-1"


def _runtime(store):
    return MagicMock(wiki_store=store)


def _fake_embedder(qvec):
    e = MagicMock()
    e.embed_nodes.return_value = [MagicMock(vector=qvec)]
    return e


def test_search_pages_returns_relevant_page(qa_setup):
    store, sid = qa_setup
    tool = WikiSearchPagesTool(session_id=sid)
    step = MagicMock(tool_input={"query": "authentication token"})
    with patch.object(search_pages_mod, "_resolve_runtime", return_value=_runtime(store)), \
         patch.object(search_pages_mod, "_make_embedder", return_value=_fake_embedder([0.0, 0.0])):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "auth" in body  # the auth page should rank first


def test_search_pages_validates_args(qa_setup):
    store, sid = qa_setup
    tool = WikiSearchPagesTool(session_id=sid)
    step = MagicMock(tool_input={"bogus": "x"})
    with patch.object(search_pages_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "validation" in str(result.content)


def test_read_page_returns_full_shape(qa_setup):
    store, sid = qa_setup
    tool = WikiReadPageTool(session_id=sid)
    step = MagicMock(tool_input={"pageId": "auth"})
    with patch.object(read_page_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "Auth" in body  # title
    assert "Tokens" in body  # body


def test_read_page_not_found(qa_setup):
    store, sid = qa_setup
    tool = WikiReadPageTool(session_id=sid)
    step = MagicMock(tool_input={"pageId": "missing"})
    with patch.object(read_page_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "not_found" in str(result.content)


def test_code_search_returns_node_hit(qa_setup):
    store, sid = qa_setup
    tool = WikiCodeSearchTool(session_id=sid)
    step = MagicMock(tool_input={"query": "authenticate", "k": 5})
    with patch.object(code_search_mod, "_resolve_runtime", return_value=_runtime(store)), \
         patch.object(code_search_mod, "_make_embedder", return_value=_fake_embedder([1.0, 0.0])):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "f1" in body or "authenticate" in body


def test_emit_block_persists_open_and_close_events(qa_setup):
    store, sid = qa_setup
    tool = WikiEmitBlockTool(session_id=sid)
    step = MagicMock(tool_input={
        "index": 0,
        "block": {"kind": "p", "text": "Hello, world."},
    })
    with patch.object(emit_block_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "ok" in str(result.content)
    events = store.load_qa_events("a1")
    types = [e["type"] for e in events]
    assert "block_open" in types
    assert "block_close" in types
    # open's index matches
    opened = next(e for e in events if e["type"] == "block_open")
    assert opened["index"] == 0


def test_emit_block_invalid_block_kind(qa_setup):
    store, sid = qa_setup
    tool = WikiEmitBlockTool(session_id=sid)
    step = MagicMock(tool_input={
        "index": 0,
        "block": {"kind": "not_a_real_kind", "text": "x"},
    })
    with patch.object(emit_block_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "validation" in str(result.content)


def test_emit_block_rejects_duplicate_index(qa_setup):
    store, sid = qa_setup
    tool = WikiEmitBlockTool(session_id=sid)
    step = MagicMock(tool_input={"index": 0, "block": {"kind": "p", "text": "x"}})
    with patch.object(emit_block_mod, "_resolve_runtime", return_value=_runtime(store)):
        asyncio.run(tool.handle(step))
        result = asyncio.run(tool.handle(step))
    assert "validation" in str(result.content) or "already" in str(result.content)
