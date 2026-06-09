"""Tests for the structured-workspace → wiki-slug resolver (#51).

A ``StructuredResponder`` session is NOT a registered wiki QA answer, so the
old ``resolve_qa_ctx`` path (``find_qa_by_session`` → ``None``) left every wiki
retrieval tool returning "wiki QA ctx not found" — the `workspace` param on
``/v1/structured`` was wired in name only. These tests pin the fallback:
the slug is recovered from the session transcript's ``structured_workspace``
context event, so retrieval tools ground in the workspace.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Embedding, GraphNode, WikiPage

# ── Fakes ──────────────────────────────────────────────────────────────────────


class _FakeSessionStore:
    """Minimal core-session-store double exposing only ``load_transcript``.

    The structured-workspace resolver reads context events the same way the
    real ``SessionStore`` lays them down: ``{"type": "context", "payload": …}``.
    """

    def __init__(self) -> None:
        self._transcripts: dict[str, list[dict]] = {}

    def append_context_event(self, session_id: str, payload: dict) -> None:
        self._transcripts.setdefault(session_id, []).append(
            {"type": "context", "payload": payload}
        )

    def load_transcript(self, session_id: str) -> list[dict]:
        return list(self._transcripts.get(session_id, []))


def _wiki_store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _runtime(wiki_store, session_store=None):
    """Seam-shaped runtime: ``wiki_store`` always, ``session_store`` optionally."""
    if session_store is None:
        return SimpleNamespace(wiki_store=wiki_store)
    return SimpleNamespace(wiki_store=wiki_store, session_store=session_store)


def _seed_workspace(wiki_store: JsonWikiStore, slug: str = "org/repo") -> None:
    """Seed a page + graph node + embedding so retrieval has something to find."""
    wiki_store.save_page(slug, WikiPage(
        id="auth", title="Auth",
        frontmatter={"title": "Auth", "slug": "auth"},
        body="Tokens, sessions, login flow.",
        toc=[], nav=[],
    ))
    wiki_store.upsert_nodes(slug, [
        GraphNode(
            slug=slug, node_id="f1", type="Function", name="authenticate",
            file="auth.py", range=(0, 100), docstring="Verify token.",
        ),
    ])
    wiki_store.upsert_embeddings(slug, [
        Embedding(slug=slug, node_id="f1", vector=[1.0, 0.0], model="m", dim=2),
    ])


# ── 1. resolve_workspace_slug ──────────────────────────────────────────────────


def test_resolve_workspace_slug_reads_latest_context_event(tmp_path: Path) -> None:
    """The slug comes from the most-recent ``structured_workspace`` context event."""
    from mewbo_graph.plugins.wiki._ctx import resolve_workspace_slug

    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-s", {"client_capabilities": ["wiki"]})
    sessions.append_context_event("sess-s", {"structured_workspace": "org/first"})
    sessions.append_context_event("sess-s", {"structured_workspace": "org/repo"})

    slug = resolve_workspace_slug("sess-s", _runtime(_wiki_store(tmp_path), sessions))
    assert slug == "org/repo"  # latest wins


def test_resolve_workspace_slug_none_when_no_event(tmp_path: Path) -> None:
    """No ``structured_workspace`` event → None (a plain session isn't grounded)."""
    from mewbo_graph.plugins.wiki._ctx import resolve_workspace_slug

    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-s", {"client_capabilities": ["wiki"]})
    slug = resolve_workspace_slug("sess-s", _runtime(_wiki_store(tmp_path), sessions))
    assert slug is None


def test_resolve_workspace_slug_falls_back_to_singleton_store(tmp_path: Path) -> None:
    """When the seam carries no ``session_store``, fall back to the cached singleton.

    A seam shaped ``SimpleNamespace(wiki_store=…)`` (no ``session_store``) must
    still resolve — via the process-wide ``get_session_store`` singleton, NOT a
    fresh ``create_session_store()`` per call (the latency/leak the review flagged).
    """
    from mewbo_graph.plugins.wiki import _ctx

    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-s", {"structured_workspace": "org/repo"})

    with patch.object(_ctx, "get_session_store", return_value=sessions):
        slug = _ctx.resolve_workspace_slug("sess-s", _runtime(_wiki_store(tmp_path)))
    assert slug == "org/repo"


def test_get_session_store_constructs_at_most_once(monkeypatch) -> None:
    """The core session store is a process-wide singleton — built ONCE, then cached.

    Pins the BLOCKER fix: ``resolve_runtime`` / ``resolve_workspace_slug`` must not
    re-create the store (a fresh MongoClient + ping + index ensure) per retrieval
    call. We count the construction calls across several ``get_session_store``
    invocations and several ``resolve_runtime`` builds.
    """
    from mewbo_graph.plugins.wiki import _ctx

    calls = {"n": 0}

    def _counting_factory(*_a, **_k):
        calls["n"] += 1
        return _FakeSessionStore()

    # Reset the cached singleton so this test owns the construction count.
    monkeypatch.setattr(_ctx, "_SESSION_STORE", None)
    monkeypatch.setattr(_ctx, "create_session_store", _counting_factory)

    first = _ctx.get_session_store()
    again = _ctx.get_session_store()
    assert first is again  # same instance — cached
    # resolve_runtime rides the SAME singleton, so building the seam repeatedly
    # (once per tool call in production) never re-constructs the store.
    for _ in range(3):
        rt = _ctx.resolve_runtime()
        assert rt.session_store is first
    assert calls["n"] == 1  # constructed exactly once across all of the above


# ── 2. resolve_qa_ctx fallback ─────────────────────────────────────────────────


def test_resolve_qa_ctx_falls_back_to_workspace_slug(tmp_path: Path) -> None:
    """A non-QA session with a workspace event yields a slug-only ctx (answer_id None)."""
    from mewbo_graph.plugins.wiki._ctx import WikiQaCtx, resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-s", {"structured_workspace": "org/repo"})

    ctx = resolve_qa_ctx("sess-s", _runtime(wiki_store, sessions))

    assert ctx is not None
    assert isinstance(ctx, WikiQaCtx)
    assert ctx.answer_id is None  # not a registered QA answer
    assert ctx.slug == "org/repo"
    assert ctx.session_id == "sess-s"
    assert ctx.store is wiki_store


def test_resolve_qa_ctx_still_none_without_qa_or_workspace(tmp_path: Path) -> None:
    """No QA answer AND no workspace event → still None (unchanged behaviour)."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    ctx = resolve_qa_ctx("sess-s", _runtime(_wiki_store(tmp_path), _FakeSessionStore()))
    assert ctx is None


def test_resolve_qa_ctx_prefers_registered_qa_over_workspace(tmp_path: Path) -> None:
    """A real QA answer wins over the workspace fallback (registered ctx is richer)."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx
    from mewbo_graph.wiki.types import QaAnswer

    wiki_store = _wiki_store(tmp_path)
    wiki_store.save_qa(QaAnswer(
        answerId="a1", fromPageId="overview", summarySources=[],
        model="m", blocks=[], slug="org/qa-slug",
    ))
    wiki_store.attach_qa_session("a1", "sess-s")
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-s", {"structured_workspace": "org/repo"})

    ctx = resolve_qa_ctx("sess-s", _runtime(wiki_store, sessions))
    assert ctx is not None
    assert ctx.answer_id == "a1"  # registered QA, not the workspace fallback


# ── 3. e2e grounded-structured: a retrieval tool resolves the slug ─────────────


def test_grounded_structured_search_resolves_workspace_e2e(tmp_path: Path) -> None:
    """End-to-end: a StructuredResponder-style session grounds a wiki search.

    Seeds a wiki workspace, writes the ``structured_workspace`` context event the
    responder lays down, then runs ``wiki_search_pages`` over that session id and
    asserts it returns HITS (grounding works) instead of "wiki QA ctx not found".
    """
    from mewbo_graph.plugins.wiki import search_pages as search_pages_mod
    from mewbo_graph.plugins.wiki.search_pages import WikiSearchPagesTool

    wiki_store = _wiki_store(tmp_path)
    _seed_workspace(wiki_store, "org/repo")
    sessions = _FakeSessionStore()
    # This is exactly what StructuredResponder._prepare writes.
    sessions.append_context_event("sess-struct", {"client_capabilities": ["wiki"]})
    sessions.append_context_event("sess-struct", {"structured_workspace": "org/repo"})

    runtime = _runtime(wiki_store, sessions)
    tool = WikiSearchPagesTool(session_id="sess-struct")
    step = MagicMock(tool_input={"query": "authentication token"})

    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0, 0.0]
    with patch.object(search_pages_mod, "_resolve_runtime", return_value=runtime), \
         patch.object(search_pages_mod, "_make_embedder", return_value=embedder):
        result = asyncio.run(tool.handle(step))

    body = str(result.content)
    assert "wiki QA ctx not found" not in body
    assert "auth" in body  # the seeded page is a hit → grounded


def test_grounded_structured_search_without_workspace_is_ungrounded(tmp_path: Path) -> None:
    """The regression guard: NO workspace event → the old not-found error stands.

    This is the pre-fix behaviour for a truly unscoped session; it proves the
    fallback is gated on the workspace event, not blanket-applied.
    """
    from mewbo_graph.plugins.wiki import search_pages as search_pages_mod
    from mewbo_graph.plugins.wiki.search_pages import WikiSearchPagesTool

    wiki_store = _wiki_store(tmp_path)
    _seed_workspace(wiki_store, "org/repo")
    runtime = _runtime(wiki_store, _FakeSessionStore())  # empty transcript
    tool = WikiSearchPagesTool(session_id="sess-bare")
    step = MagicMock(tool_input={"query": "authentication token"})

    with patch.object(search_pages_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(step))
    assert "wiki QA ctx not found" in str(result.content)


def test_emit_block_refuses_grounded_structured_session(tmp_path: Path) -> None:
    """``wiki_emit_block`` must refuse a slug-only ctx (``answer_id is None``).

    A grounded structured-response session has a workspace slug but NO QA event
    log; emitting answer blocks is QA-only, so the tool returns a clear error
    instead of NPE-ing on ``load_qa_events``/``append_qa_event``.
    """
    from mewbo_graph.plugins.wiki import emit_block as emit_block_mod
    from mewbo_graph.plugins.wiki.emit_block import WikiEmitBlockTool

    wiki_store = _wiki_store(tmp_path)
    _seed_workspace(wiki_store, "org/repo")
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-struct", {"structured_workspace": "org/repo"})

    runtime = _runtime(wiki_store, sessions)
    tool = WikiEmitBlockTool(session_id="sess-struct")
    step = MagicMock(tool_input={"index": 0, "block": {"kind": "p", "text": "hi"}})

    with patch.object(emit_block_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "requires a registered QA answer" in body
    # No QA event was written for this (non-QA) session — the guard short-circuited.
    assert wiki_store.find_qa_by_session("sess-struct") is None
