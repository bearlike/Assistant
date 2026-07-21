"""Tests for the structured-workspace → wiki-slug resolver.

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
from mewbo_graph.wiki.types import Embedding, Project, WikiPage, make_graph_node

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
        make_graph_node(
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
    """The regression guard: NO workspace event AND no project → still ungrounded.

    It proves the workspace fallback is gated on the workspace event rather than
    blanket-applied. It now also guards the project tier: a wiki IS seeded here,
    and a session carrying no project identity at all must NOT be grounded into
    it. Grounding an answer in an arbitrary repository's wiki is worse than
    grounding it in none.
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
    assert "no wiki indexed" in str(result.content)


# ── Tier 3: an ordinary task session grounds in its own project's wiki ────────


def _seed_project(wiki_store: JsonWikiStore, slug: str) -> None:
    """Register *slug* as an INDEXED project, which is what tier 3 matches on."""
    wiki_store.create_project(Project(
        slug=slug, source="gitea", lang="Python",
        indexedAt="2026-07-19T00:00:00Z", pages=1, desc="",
    ))


class _TaggedSessionStore(_FakeSessionStore):
    """``_FakeSessionStore`` plus the tag reverse-index tier 3 also reads."""

    def __init__(self) -> None:
        super().__init__()
        self._tags: dict[str, list[str]] = {}

    def tag(self, session_id: str, tag: str) -> None:
        self._tags.setdefault(session_id, []).append(tag)

    def tags_for_session(self, session_id: str) -> list[str]:
        return list(self._tags.get(session_id, []))


def test_ordinary_session_grounds_in_its_projects_wiki(tmp_path: Path) -> None:
    """A plain task session bound to project "repo" resolves the indexed slug.

    This is the whole point of the tier: nothing scopes the session — no QA
    answer, no ``structured_workspace`` — so its own project is the binding.
    """
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    _seed_project(wiki_store, "git.example.com/acme/beacon")
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-task", {"project": "beacon"})

    ctx = resolve_qa_ctx("sess-task", _runtime(wiki_store, sessions))

    assert ctx is not None
    assert ctx.slug == "git.example.com/acme/beacon"
    # Shape-identical to the workspace tier, which is what makes every tool
    # already safe under tier 2 safe here too.
    assert ctx.answer_id is None


def test_ordinary_session_matches_a_vcs_tag_by_owner_and_repo(tmp_path: Path) -> None:
    """A pickup session's ``vcs:<owner/repo>`` tag is the PRECISE match key."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    _seed_project(wiki_store, "git.example.com/acme/beacon")
    sessions = _TaggedSessionStore()
    sessions.tag("sess-pickup", "vcs:acme/beacon:issue:42")

    ctx = resolve_qa_ctx("sess-pickup", _runtime(wiki_store, sessions))

    assert ctx is not None
    assert ctx.slug == "git.example.com/acme/beacon"


def test_an_ambiguous_repo_name_refuses_to_resolve(tmp_path: Path) -> None:
    """Same repo name under two owners → NO slug, never an arbitrary winner.

    Answering from the wrong repository's wiki is worse than answering from
    none, so the bare-name tier refuses rather than guesses.
    """
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    _seed_project(wiki_store, "git.example.com/acme/beacon")
    _seed_project(wiki_store, "github.com/other/beacon")
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-ambig", {"project": "beacon"})

    assert resolve_qa_ctx("sess-ambig", _runtime(wiki_store, sessions)) is None


def test_a_projects_owner_repo_disambiguates_a_shared_name(tmp_path: Path) -> None:
    """The precise key is tried FIRST, so a vcs tag resolves what a name cannot."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    _seed_project(wiki_store, "git.example.com/acme/beacon")
    _seed_project(wiki_store, "github.com/other/beacon")
    sessions = _TaggedSessionStore()
    sessions.append_context_event("sess-both", {"project": "beacon"})
    sessions.tag("sess-both", "vcs:other/beacon:pr:7")

    ctx = resolve_qa_ctx("sess-both", _runtime(wiki_store, sessions))

    assert ctx is not None
    assert ctx.slug == "github.com/other/beacon"


def test_a_project_with_no_indexed_wiki_stays_ungrounded(tmp_path: Path) -> None:
    """A real project that was never indexed resolves to nothing, not to a neighbour."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    _seed_project(wiki_store, "git.example.com/acme/beacon")
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-other", {"project": "unindexed"})

    assert resolve_qa_ctx("sess-other", _runtime(wiki_store, sessions)) is None


def test_a_managed_worktree_matches_its_parent_project(tmp_path: Path) -> None:
    """``project`` is an opaque ``managed:<uuid>`` there, so ``repo`` is the key."""
    from mewbo_graph.plugins.wiki._ctx import resolve_qa_ctx

    wiki_store = _wiki_store(tmp_path)
    _seed_project(wiki_store, "git.example.com/acme/beacon")
    sessions = _FakeSessionStore()
    sessions.append_context_event(
        "sess-wt", {"project": "managed:deadbeef", "repo": "beacon"}
    )

    ctx = resolve_qa_ctx("sess-wt", _runtime(wiki_store, sessions))

    assert ctx is not None
    assert ctx.slug == "git.example.com/acme/beacon"


def test_emit_answer_refuses_grounded_structured_session(tmp_path: Path) -> None:
    """``wiki_emit_answer`` must refuse a slug-only ctx (``answer_id is None``).

    A grounded structured-response session has a workspace slug but NO QA event
    log; emitting the answer is QA-only, so the tool returns a clear error
    instead of NPE-ing on ``load_qa_events``/``append_qa_event``.
    """
    from mewbo_graph.plugins.wiki import emit_answer as emit_answer_mod
    from mewbo_graph.plugins.wiki.emit_answer import WikiEmitAnswerTool

    wiki_store = _wiki_store(tmp_path)
    _seed_workspace(wiki_store, "org/repo")
    sessions = _FakeSessionStore()
    sessions.append_context_event("sess-struct", {"structured_workspace": "org/repo"})

    runtime = _runtime(wiki_store, sessions)
    tool = WikiEmitAnswerTool(session_id="sess-struct")
    step = MagicMock(tool_input={"blocks": [
        {"kind": "p", "text": "hi"},
        {"kind": "sources", "items": ["wiki:x"]},
    ]})

    with patch.object(emit_answer_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(step))
    body = str(result.content)
    assert "requires a registered QA answer" in body
    # No QA event was written for this (non-QA) session — the guard short-circuited.
    assert wiki_store.find_qa_by_session("sess-struct") is None
