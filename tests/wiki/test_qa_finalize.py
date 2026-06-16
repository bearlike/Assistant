"""QA finalization — QaFinalizer (close/enrich), terminal emit, and the session-end hook.

The terminal ``sources`` block is the answer's accept state: emitting it drives
``QaFinalizer.close`` (reconcile snapshot + ``complete``). ``accessed_sources`` is the
deterministic probe trail folded from ``access`` events; ``models_used`` is stamped by
the ``QaSessionEndHook`` net from the session transcript. These tests pin all of it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mongomock
import pytest
from mewbo_api.wiki.jobs import QaSessionEndHook
from mewbo_graph.entities.types import Entity
from mewbo_graph.plugins.wiki import emit_block as emit_block_mod
from mewbo_graph.wiki.memory_types import MemoryFilter
from mewbo_graph.wiki.qa import AccessedSourceResolver, QaFinalizer, QaMemoryDepositor
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore
from mewbo_graph.wiki.types import Frontmatter, GraphNode, QaAnswer, WikiPage


@pytest.fixture
def store(tmp_path):
    s = JsonWikiStore(root_dir=tmp_path / "wiki")
    s.save_qa(QaAnswer(
        answerId="a1", fromPageId="landing-page", summarySources=[],
        model="anthropic/claude-sonnet-4-6", blocks=[], slug="org/repo",
    ))
    s.attach_qa_session("a1", "sess-1")
    s.append_qa_event("a1", {"type": "meta", "answerId": "a1"})
    # Deterministic probe trail — graph nodes + a file read + a page (dupes collapse).
    s.append_qa_event("a1", {"type": "access", "refs": ["graph:n7", "src/app.py#L1-20"]})
    s.append_qa_event("a1", {"type": "access", "refs": ["graph:n7", "wiki:landing-page"]})
    s.append_qa_event("a1", {"type": "block_open", "index": 0,
                             "block": {"kind": "p", "text": "The answer."}})
    s.append_qa_event("a1", {"type": "block_open", "index": 1, "block": {
        "kind": "sources",
        "items": ["wiki:landing-page", "src/app.py#L1-20", "graph:n7"],
    }})
    return s


def test_close_reconciles_blocks_curated_and_accessed(store):
    """close() folds blocks + curated sources + the deterministic accessed trail."""
    assert store.get_qa("a1").blocks == []  # precondition: empty snapshot
    assert QaFinalizer.close(store, "a1") is True

    snap = store.get_qa("a1")
    assert [b.root.kind for b in snap.blocks] == ["p", "sources"]
    assert snap.summary_sources == ["wiki:landing-page"]  # curated — wiki pages only
    # Deterministic trail, de-duplicated, first-seen order preserved:
    assert snap.accessed_sources == ["graph:n7", "src/app.py#L1-20", "wiki:landing-page"]
    assert store.load_qa_events("a1")[-1]["type"] == "complete"


def test_tag_page_citations_reschemes_only_real_pages(store):
    """A bare wiki-page path in a sources block is re-schemed ``wiki:<id>`` (#70).

    Without this the console's ``fileCitations`` treats the page as a source FILE
    and the ``SourceCard`` 404s against ``/source`` (pages aren't in the clone).
    Membership in the REAL page-id set is the authority — code files, colon /
    line-range refs, and already-schemed refs are left untouched.
    """
    store.save_page("org/repo", WikiPage(
        id="architecture-overview", title="Architecture",
        frontmatter=Frontmatter(title="Architecture", slug="architecture-overview"),
        body="# x", toc=[], nav=[],
    ))
    block = {"kind": "sources", "items": [
        "architecture-overview",        # bare page id → wiki:
        "pages/architecture-overview",  # ``pages/`` prefix → wiki:
        "src/app.py#L1-9",              # file line-range → untouched
        "src/app.py",                   # bare file (not a page) → untouched
        "graph:n7",                     # already schemed → untouched
        "wiki:landing-page",            # already wiki → untouched
    ]}
    tagged = QaFinalizer.tag_page_citations(block, store, "org/repo")
    assert tagged["items"] == [
        "wiki:architecture-overview",
        "wiki:architecture-overview",
        "src/app.py#L1-9",
        "src/app.py",
        "graph:n7",
        "wiki:landing-page",
    ]


def test_accessed_source_resolver_humanises_graph_hashes(store):
    """``graph:<node_id>`` provenance refs resolve to readable labels (#70).

    An AST node → its ``file#Symbol`` key; an abstract entity → ``name (type)``;
    an unresolved id (stale graph) → ``unknown (<hash[:8]>)``. File / page refs
    pass through. Non-destructive: the snapshot keeps the raw ids.
    """
    store.upsert_nodes("org/repo", [GraphNode(
        slug="org/repo", node_id="ast1", type="Function", name="verify",
        file="src/app.py", range=(0, 9),
    )])
    ent = Entity(name="Session Runtime", type="subsystem")
    store.upsert_entities("org/repo", [ent])

    out = AccessedSourceResolver.resolve_refs(store, "org/repo", [
        "graph:ast1",          # AST node → file#Symbol
        f"graph:{ent.id}",     # abstract entity → name (type)
        "graph:deadbeef0000",  # unresolved → unknown (hash[:8])
        "src/app.py#L1-9",     # file ref → untouched
        "wiki:landing-page",   # page ref → untouched
    ])
    assert out == [
        "graph:src/app.py#verify",
        "graph:Session Runtime (subsystem)",
        "graph:unknown (deadbeef)",
        "src/app.py#L1-9",
        "wiki:landing-page",
    ]


def test_accessed_source_resolver_passthrough_without_graph_refs(store):
    """No ``graph:`` refs ⇒ no graph scan, list returned verbatim (cheap path)."""
    refs = ["src/app.py#L1-9", "wiki:landing-page"]
    assert AccessedSourceResolver.resolve_refs(store, "org/repo", refs) == refs


def test_close_is_idempotent(store):
    """A second close after a terminal event is a no-op (SSE reconnect / double end safe)."""
    assert QaFinalizer.close(store, "a1") is True
    n = len(store.load_qa_events("a1"))
    assert QaFinalizer.close(store, "a1") is False
    assert len(store.load_qa_events("a1")) == n


def test_close_error_emits_error_not_complete(store):
    """A halted run gets a terminal error event but still reconciles partial blocks."""
    QaFinalizer.close(store, "a1", error="halted_no_progress")
    last = store.load_qa_events("a1")[-1]
    assert last["type"] == "error"
    assert last["error"]["message"] == "halted_no_progress"
    assert [b.root.kind for b in store.get_qa("a1").blocks] == ["p", "sources"]


def test_enrich_stamps_models_even_after_close(store):
    """models_used is stamped post-close (the hook fires after the terminal emit closed it)."""
    QaFinalizer.close(store, "a1")
    QaFinalizer.enrich(store, "a1", models=["m-a", "m-a", "m-b"])
    assert store.get_qa("a1").models_used == ["m-a", "m-b"]  # de-duplicated


def test_summary_sources_prefers_explicit_summary_ready(store):
    """An explicit summary_ready wins over derivation from the sources block."""
    store.append_qa_event("a1", {"type": "summary_ready",
                                  "sources": ["wiki:overview", "wiki:auth"]})
    QaFinalizer.close(store, "a1")
    assert store.get_qa("a1").summary_sources == ["wiki:overview", "wiki:auth"]


def test_terminal_sources_block_closes_via_emit(store, monkeypatch):
    """Emitting the sources block IS the accept state: it closes + requests terminate."""
    monkeypatch.setattr(
        emit_block_mod, "_resolve_runtime", lambda: SimpleNamespace(wiki_store=store)
    )
    store.save_qa(QaAnswer(answerId="a2", fromPageId="", summarySources=[],
                           model="m", blocks=[], slug="org/repo"))
    store.attach_qa_session("a2", "sess-2")

    tool = emit_block_mod.WikiEmitBlockTool("sess-2")
    step = SimpleNamespace(
        tool_input={"index": 0, "block": {"kind": "sources", "items": ["wiki:x"]}}
    )
    asyncio.run(tool.handle(step))

    assert tool.should_terminate_run() is True  # the loop stops cleanly here
    assert store.load_qa_events("a2")[-1]["type"] == "complete"
    assert [b.root.kind for b in store.get_qa("a2").blocks] == ["sources"]


def test_session_end_hook_finalizes_and_stamps_models(store):
    """The hook closes a QA session + stamps transcript models; a non-QA session no-ops."""
    transcript = [
        {"type": "llm_call_start", "payload": {"model": "openai/claude-sonnet-4-6"}},
        {"type": "llm_call_start", "payload": {"model": "openai/claude-sonnet-4-6"}},
        {"type": "llm_call_start", "payload": {"model": "openai/haiku"}},  # a probe sub-model
    ]
    runtime = SimpleNamespace(
        wiki_store=store,
        load_events=lambda sid: transcript if sid == "sess-1" else [],
    )
    hook = QaSessionEndHook(runtime)

    hook("other-session", None)  # non-QA → cheap no-op
    assert all(e.get("type") != "complete" for e in store.load_qa_events("a1"))

    hook("sess-1", None)         # QA → finalized + models + accessed trail
    snap = store.get_qa("a1")
    assert store.load_qa_events("a1")[-1]["type"] == "complete"
    assert snap.models_used == ["openai/claude-sonnet-4-6", "openai/haiku"]
    assert snap.accessed_sources == ["graph:n7", "src/app.py#L1-20", "wiki:landing-page"]


# ── Backend-divergence regression (the JSON-only tests above missed this) ──────


def _seed_for_close(s):
    """Create + attach + log a QA answer ready for close(), on any backend."""
    s.save_qa(QaAnswer(answerId="z1", fromPageId="lp", summarySources=[],
                       model="m", blocks=[], slug="o/r"))
    s.attach_qa_session("z1", "sessZ")
    s.append_qa_event("z1", {"type": "meta", "answerId": "z1"})
    # A probe touched a graph node, a source range, AND read a page → all recorded.
    s.append_qa_event("z1", {"type": "access",
                             "refs": ["graph:n1", "src/a.py#L1-9", "wiki:lp"]})
    s.append_qa_event("z1", {"type": "block_open", "index": 0,
                             "block": {"kind": "p", "text": "a"}})
    s.append_qa_event("z1", {"type": "block_open", "index": 1,
                             "block": {"kind": "sources", "items": ["wiki:lp"]}})
    return s


@pytest.mark.parametrize(
    "make_store",
    [
        lambda tmp: JsonWikiStore(root_dir=tmp / "wiki"),
        lambda tmp: MongoWikiStore(client=mongomock.MongoClient(), database="t"),
    ],
    ids=["json", "mongo"],
)
def test_close_preserves_session_and_idx_on_both_backends(make_store, tmp_path):
    """close() must not drop session_id or reset the event-idx counter (Mongo regression).

    The Mongo ``save_qa`` is a full-doc replace that packs ``event_count`` +
    ``session_id``, so a mid-stream ``save_qa`` reset the idx counter (the terminal
    ``complete`` append then collided at idx 0) AND dropped the session mapping (the
    on_session_end net no-op'd). ``QaFinalizer`` now uses the non-destructive
    ``update_qa_fields``. A JSON-only test missed this — so this runs BOTH backends.
    """
    s = _seed_for_close(make_store(tmp_path))
    events_before = len(s.load_qa_events("z1"))  # meta + access + 2 blocks = 4

    assert QaFinalizer.close(s, "z1") is True

    # 1. The session mapping survived → the on_session_end net can still resolve it.
    assert s.find_qa_by_session("sessZ") == "z1"
    # 2. The terminal complete event persisted.
    evs = s.load_qa_events("z1")
    assert any(e.get("type") == "complete" for e in evs)
    # 3. The idx counter was preserved (not reset) → the next append doesn't collide.
    nxt = s.append_qa_event("z1", {"type": "probe"})
    assert nxt == events_before + 1  # complete was idx=4, this is idx=5
    # 4. The snapshot reconciled regardless of backend.
    snap = s.get_qa("z1")
    assert [b.root.kind for b in snap.blocks] == ["p", "sources"]
    assert snap.accessed_sources == ["graph:n1", "src/a.py#L1-9", "wiki:lp"]


# ── QaMemoryDepositor — the post-QA memory flywheel (Gitea #13) ─────────────────


SLUG = "org/repo"


def _gn(nid, typ, name, f):
    return GraphNode(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


@pytest.fixture
def deposit_store(tmp_path):
    """A store with code nodes the answer's accessed refs anchor to + a finalized answer."""
    s = JsonWikiStore(root_dir=tmp_path / "wiki")
    # Seed code graph: a File node (src/app.py) + a symbol the probe touched via
    # ``graph:n7`` so BOTH anchor kinds (graph-id and bare-path) resolve.
    s.upsert_nodes(
        SLUG,
        [
            _gn("n7", "Function", "verify", "src/app.py"),
            _gn("nF", "File", "src/app.py", "src/app.py"),
        ],
    )
    s.save_qa(
        QaAnswer(
            answerId="d1",
            fromPageId="landing-page",
            summarySources=["wiki:landing-page"],
            model="anthropic/claude-sonnet-4-6",
            blocks=[
                {"kind": "p", "text": "Auth tokens are verified in verify() before a request."},
                {"kind": "sources", "items": ["wiki:landing-page", "graph:n7"]},
            ],
            accessedSources=["graph:n7", "src/app.py#L1-20", "wiki:landing-page"],
            slug=SLUG,
        )
    )
    return s


def test_deposit_ingests_one_anchored_qa_memory(deposit_store):
    """A finalized answer becomes a QA memory note anchored to the cited code entities."""
    snap = deposit_store.get_qa("d1")
    assert deposit_store.query_memory(SLUG) == []  # precondition: no memory yet

    count = QaMemoryDepositor.deposit(deposit_store, snap, question="How are auth tokens verified?")
    assert count >= 1

    notes = deposit_store.query_memory(SLUG)
    assert len(notes) == 1
    note = notes[0]
    # Provenance + labels mark it as a QA-sourced flywheel deposit.
    assert note.provenance.source == "qa"
    assert note.provenance.author_agent == "wiki-qa"
    assert note.provenance.session_id == "d1"
    assert "qa" in note.labels
    # The direct-answer paragraph is the distilled claim (lead p block flattened).
    assert "verify" in note.content.lower()

    # It is GRAFTED onto the multiplex: ANCHORS edges to the cited code entities.
    anchors = {
        e.target for e in deposit_store.list_memory_edges(SLUG, node_id=note.node_id)
        if e.type == "ANCHORS"
    }
    # ``graph:n7`` → entity_key ``src/app.py#verify``; ``src/app.py#L1-20`` → File
    # entity_key ``src/app.py``. ``wiki:landing-page`` is a page ref → NOT anchored.
    assert "src/app.py#verify" in anchors
    assert "src/app.py" in anchors
    assert all(not a.startswith("wiki:") for a in anchors)


def test_deposit_is_idempotent(deposit_store):
    """A retry/recovery re-deposits the SAME content-addressed node — no duplicate."""
    snap = deposit_store.get_qa("d1")
    QaMemoryDepositor.deposit(deposit_store, snap)
    first = deposit_store.query_memory(SLUG)
    assert len(first) == 1

    QaMemoryDepositor.deposit(deposit_store, snap)  # idempotent
    second = deposit_store.query_memory(SLUG)
    assert len(second) == 1
    assert second[0].node_id == first[0].node_id


def test_deposit_filters_by_qa_source(deposit_store):
    """The QA note is retrievable via the ``source=qa`` facet (flywheel reuse path)."""
    QaMemoryDepositor.deposit(deposit_store, deposit_store.get_qa("d1"))
    qa_notes = deposit_store.query_memory(SLUG, filt=MemoryFilter(source="qa"))
    assert len(qa_notes) == 1


def test_deposit_skips_empty_slug(deposit_store):
    """A slug-less answer is skipped — never pollute the empty-string corpus."""
    snap = deposit_store.get_qa("d1")
    blank = snap.model_copy(update={"slug": ""})
    assert QaMemoryDepositor.deposit(deposit_store, blank) == 0
    assert deposit_store.query_memory("") == []


def test_deposit_skips_answer_without_paragraph(deposit_store):
    """No distillable claim (no lead p block) → a no-op, never a crash."""
    snap = deposit_store.get_qa("d1")
    no_p = snap.model_copy(update={"blocks": []})
    assert QaMemoryDepositor.deposit(deposit_store, no_p) == 0
    assert deposit_store.query_memory(SLUG) == []


def test_session_end_hook_deposits_qa_memory(deposit_store):
    """End-to-end: the on_session_end hook closes the answer AND deposits the QA memory."""
    deposit_store.attach_qa_session("d1", "sess-d")
    deposit_store.append_qa_event("d1", {"type": "meta", "answerId": "d1"})
    deposit_store.append_qa_event(
        "d1", {"type": "access", "refs": ["graph:n7", "src/app.py#L1-20"]}
    )
    deposit_store.append_qa_event(
        "d1",
        {"type": "block_open", "index": 0,
         "block": {"kind": "p", "text": "Auth tokens are verified in verify()."}},
    )
    deposit_store.append_qa_event(
        "d1",
        {"type": "block_open", "index": 1,
         "block": {"kind": "sources", "items": ["wiki:landing-page", "graph:n7"]}},
    )
    runtime = SimpleNamespace(wiki_store=deposit_store, load_events=lambda sid: [])
    QaSessionEndHook(runtime)("sess-d", None)

    # The answer is closed AND a QA memory note was grafted in the same hook pass.
    assert deposit_store.get_qa("d1").status == "complete"
    notes = deposit_store.query_memory(SLUG)
    assert len(notes) == 1
    assert notes[0].provenance.source == "qa"
