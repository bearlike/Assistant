"""QA tools tests."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki import (
    code_search as code_search_mod,
    emit_block as emit_block_mod,
    read_page as read_page_mod,
    search_pages as search_pages_mod,
)
from mewbo_graph.plugins.wiki.code_search import WikiCodeSearchTool
from mewbo_graph.plugins.wiki.emit_block import WikiEmitBlockTool
from mewbo_graph.plugins.wiki.read_page import WikiReadPageTool
from mewbo_graph.plugins.wiki.search_pages import WikiSearchPagesTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Embedding,
    GraphNode,
    QaAnswer,
    WikiPage,
)


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


# ---------------------------------------------------------------------------
# Terminal status on the snapshot (#41) — the MCP poll's done-signal
# ---------------------------------------------------------------------------


def test_qa_answer_defaults_to_running_status():
    """A freshly-minted QaAnswer reports ``status='running'`` (serialized)."""
    ans = QaAnswer(
        answerId="a-status",
        fromPageId="overview",
        summarySources=[],
        model="m",
        blocks=[],
        slug="x/y",
    )
    assert ans.status == "running"
    # Serialized snapshot (what GET /v1/wiki/qa/<id> returns) carries the field.
    assert ans.model_dump(by_alias=True)["status"] == "running"


def test_inprogress_snapshot_reports_running(qa_setup):
    """A non-terminal block (``p``) leaves the persisted snapshot ``running``."""
    store, sid = qa_setup
    tool = WikiEmitBlockTool(session_id=sid)
    step = MagicMock(tool_input={"index": 0, "block": {"kind": "p", "text": "Hi."}})
    with patch.object(emit_block_mod, "_resolve_runtime", return_value=_runtime(store)):
        asyncio.run(tool.handle(step))
    assert store.get_qa("a1").status == "running"


def test_terminal_sources_block_sets_complete_on_snapshot(qa_setup):
    """The accept-state ``sources`` block flips the persisted status to ``complete``."""
    store, sid = qa_setup
    tool = WikiEmitBlockTool(session_id=sid)
    step = MagicMock(tool_input={
        "index": 0,
        "block": {"kind": "sources", "items": ["src/main.py"]},
    })
    with patch.object(emit_block_mod, "_resolve_runtime", return_value=_runtime(store)):
        result = asyncio.run(tool.handle(step))
    assert "ok" in str(result.content)
    # The reloaded snapshot — exactly what the GET route serializes — is terminal.
    reloaded = store.get_qa("a1")
    assert reloaded is not None
    assert reloaded.status == "complete"
    assert reloaded.model_dump(by_alias=True)["status"] == "complete"


# ---------------------------------------------------------------------------
# Terminal-tool loop contract (#61) — should_terminate_run → terminal_reason
# ---------------------------------------------------------------------------


def test_emit_block_terminal_reason_is_completed():
    """``WikiEmitBlockTool`` inherits ``terminal_reason() == "completed"``.

    Regression guard for #61: the tool overrides ``should_terminate_run`` but
    declared no ``terminal_reason``, so ``tool_use_loop`` raised AttributeError
    when it selected the terminating tool. The reason now lives on the shared
    ``WikiSessionTool`` base — this test fails if that base method is removed,
    because the base IS the body under test (the tool defines no own override).
    """
    tool = WikiEmitBlockTool(session_id="sess-qa-1")
    assert tool.terminal_reason() == "completed"


def test_emit_block_terminate_then_reason_matches_loop_selector(qa_setup):
    """Drive the real ``should_terminate_run → terminal_reason`` selector contract.

    Mirrors the ``tool_use_loop`` step: a non-terminal block leaves the tool
    NOT terminating; the accept-state ``sources`` block flips
    ``should_terminate_run()`` True and ``terminal_reason()`` resolves to
    ``"completed"`` WITHOUT raising — the exact pair the loop reads at the
    terminating-tool seam.
    """
    store, sid = qa_setup
    tool = WikiEmitBlockTool(session_id=sid)
    with patch.object(emit_block_mod, "_resolve_runtime", return_value=_runtime(store)):
        # Non-terminal block: the tool does not request termination.
        non_terminal = MagicMock(tool_input={
            "index": 0, "block": {"kind": "p", "text": "Hi."},
        })
        asyncio.run(tool.handle(non_terminal))
        assert tool.should_terminate_run() is False

        # Accept-state sources block: termination requested, reason completed.
        terminal = MagicMock(tool_input={
            "index": 1, "block": {"kind": "sources", "items": ["src/main.py"]},
        })
        asyncio.run(tool.handle(terminal))
    assert tool.should_terminate_run() is True
    assert tool.terminal_reason() == "completed"
