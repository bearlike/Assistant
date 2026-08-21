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
from mewbo_graph.plugins.wiki import emit_answer as emit_answer_mod
from mewbo_graph.wiki.memory_types import MemoryFilter
from mewbo_graph.wiki.qa import AccessedSourceResolver, QaFinalizer, QaMemoryDepositor
from mewbo_graph.wiki.qa_access import ACCESS_TOPN, QaAccessRecord
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore
from mewbo_graph.wiki.types import Frontmatter, QaAnswer, WikiPage, make_graph_node


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
    """close() folds blocks + cited sources (pages + file/graph) + the accessed trail."""
    assert store.get_qa("a1").blocks == []  # precondition: empty snapshot
    assert QaFinalizer.close(store, "a1") is True

    snap = store.get_qa("a1")
    assert [b.root.kind for b in snap.blocks] == ["p", "sources"]
    # Cited sources = the curated page FIRST, then the file/graph evidence folded off
    # the accessed trail — the answer's real files/symbols, not just pages.
    assert snap.summary_sources == ["wiki:landing-page", "graph:n7", "src/app.py#L1-20"]
    # Deterministic trail, de-duplicated, first-seen order preserved:
    assert snap.accessed_sources == ["graph:n7", "src/app.py#L1-20", "wiki:landing-page"]
    assert store.load_qa_events("a1")[-1]["type"] == "complete"


def test_tag_page_citations_reschemes_only_real_pages(store):
    """A bare wiki-page path in a sources block is re-schemed ``wiki:<id>``.

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


def test_tag_page_citations_unwraps_the_src_href_scheme(store):
    """A page cited as ``src:<page-id>`` re-schemes to ``wiki:<id>``.

    ``src:`` is the INLINE HREF wrapper the answer prose uses, so the model
    reaches for it in the sources list too. Treating it as "already schemed"
    let it bypass the page authority: the console read ``src:overview`` as a
    file PATH, fetched it from ``/source`` (which holds no pages), and rendered
    a dead "Source unavailable" card. The wrapper carries no claim about what
    the target is, so it is unwrapped before the authority decides — a wrapped
    FILE ref still falls through untagged, just without the dead prefix.
    """
    store.save_page("org/repo", WikiPage(
        id="overview", title="Mewbo Architecture Overview",
        frontmatter=Frontmatter(title="Mewbo Architecture Overview", slug="overview"),
        body="# x", toc=[], nav=[],
    ))
    block = {"kind": "sources", "items": [
        "src:overview",                    # wrapped page id → wiki:
        "src:Mewbo Architecture Overview",  # wrapped page TITLE → wiki:
        "overview",                        # bare page id still works
        "src:app.py",                      # wrapped file (not a page) → unwrapped
        "graph:n7",                        # a real scheme → untouched
    ]}
    tagged = QaFinalizer.tag_page_citations(block, store, "org/repo")
    assert tagged["items"] == [
        "wiki:overview",
        "wiki:overview",
        "wiki:overview",
        "app.py",
        "graph:n7",
    ]


def test_tag_page_citations_matches_title_form_refs(store):
    """A page cited by its human TITLE re-schemes to ``wiki:<id>``.

    The QA model frequently cites a page by its title ("Agent X Search
    Subsystem") rather than its slug id ("agent-x-search-subsystem"); the bare
    title then reads as a file path and ``GET /source`` 404s. Matching the
    slugified title against the page authority fixes it, while a slug-form ref
    still works, a code ``path#L..`` ref stays untouched, and an unrelated phrase
    passes through.
    """
    store.save_page("org/repo", WikiPage(
        id="agent-x-search-subsystem", title="Agent X Search Subsystem",
        frontmatter=Frontmatter(title="Agent X Search Subsystem",
                                slug="agent-x-search-subsystem"),
        body="# x", toc=[], nav=[],
    ))
    block = {"kind": "sources", "items": [
        "agent x search subsystem",     # title form (lowercased) → wiki:<id>
        "Agent X Search Subsystem",     # title form (original casing) → wiki:<id>
        "agent-x-search-subsystem",     # slug-id form still works
        "src/app.py#L1-9",              # code line-range → untouched
        "some unrelated phrase",        # not a page → untouched
    ]}
    tagged = QaFinalizer.tag_page_citations(block, store, "org/repo")
    assert tagged["items"] == [
        "wiki:agent-x-search-subsystem",
        "wiki:agent-x-search-subsystem",
        "wiki:agent-x-search-subsystem",
        "src/app.py#L1-9",
        "some unrelated phrase",
    ]


def test_accessed_trail_is_bounded_and_score_ordered():
    """The fold caps the trail to top-N, scores-first, so graph-nav bulk can't flood.

    Probes record ~dozens of unranked graph-navigation seeds plus a few ranked
    search hits. The folded ``accessed_sources`` trail must be a
    tight, score-ordered top-N — the high-signal scored hits FIRST (descending),
    the unscored navigation seeds AFTER, and the bulk capped out — not the full
    unranked navigation set recorded as if it were grounding.
    """
    events: list[dict] = []
    # 30 unscored graph-navigation seeds — the sprawl the fold must not flood with.
    for i in range(30):
        events.append(
            {"type": "access", "records": [{"ref": f"graph:nav{i}", "op": "nav"}]}
        )
    # A handful of ranked search hits (the only true grounding).
    events.append({"type": "access", "records": [
        {"ref": "graph:hitA", "score": 0.9, "rank": 1, "op": "search"},
        {"ref": "graph:hitB", "score": 0.6, "rank": 2, "op": "search"},
        {"ref": "graph:hitC", "score": 0.3, "rank": 3, "op": "search"},
    ]})

    out = QaFinalizer._accessed_from_events(events)

    # Bounded: 33 distinct refs collapse to the configured top-N.
    assert len(out) == ACCESS_TOPN
    # Scored hits lead, in descending score order.
    assert out[:3] == ["graph:hitA", "graph:hitB", "graph:hitC"]
    # The graph-nav bulk does not flood — only the cap-remainder survives.
    assert sum(1 for r in out if r.startswith("graph:nav")) == ACCESS_TOPN - 3


def test_accessed_trail_dedupes_by_ref_keeping_best_score():
    """A ref seen as both an unscored touch and a scored hit keeps the scored sighting."""
    events = [
        {"type": "access", "records": [{"ref": "graph:x", "op": "nav"}]},
        {"type": "access",
         "records": [{"ref": "graph:x", "score": 0.8, "rank": 1, "op": "search"}]},
        {"type": "access", "records": [{"ref": "src/a.py", "op": "read"}]},
    ]
    out = QaFinalizer._accessed_from_events(events)
    # graph:x deduped to ONE entry, promoted ahead of the unscored read by its score.
    assert out == ["graph:x", "src/a.py"]


def test_accessed_trail_folds_legacy_refs_events():
    """A stored ``access`` event carrying bare ``refs`` strings still folds."""
    events = [
        {"type": "access", "refs": ["graph:n1", "src/a.py#L1-9"]},
        {"type": "access", "refs": ["graph:n1", "wiki:lp"]},
    ]
    out = QaFinalizer._accessed_from_events(events)
    assert out == ["graph:n1", "src/a.py#L1-9", "wiki:lp"]


def test_from_ranked_hits_drops_below_score_floor():
    """``from_ranked_hits`` keeps only hits at/above ratio x top score, carrying rank."""
    ranked = [("a", 1.0), ("b", 0.6), ("c", 0.4), ("d", 0.1)]
    recs = QaAccessRecord.from_ranked_hits(ranked, tool="wiki_code_search", ratio=0.5)
    assert [(r.ref, r.rank) for r in recs] == [("a", 1), ("b", 2)]  # floor 0.5 drops c, d
    assert recs[0].score == 1.0 and recs[1].score == 0.6


def test_accessed_source_resolver_humanises_graph_hashes(store):
    """``graph:<node_id>`` provenance refs resolve to readable labels.

    An AST node → its ``file#Symbol`` key; an abstract entity → ``name (type)``;
    an unresolved id (stale graph) → ``unknown (<hash[:8]>)``. File / page refs
    pass through. Non-destructive: the snapshot keeps the raw ids.
    """
    store.upsert_nodes("org/repo", [make_graph_node(
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


# ── Turn-scoped reconciliation (a continued session's 2nd+ turn) ──


def test_current_turn_events_scopes_to_last_meta():
    """Only events AFTER the most recent ``meta`` belong to the current turn."""
    events = [
        {"type": "meta", "answerId": "a1"},
        {"type": "block_open", "index": 0, "block": {"kind": "p", "text": "turn1"}},
        {"type": "complete", "totalBlocks": 1},
        {"type": "meta", "answerId": "a1"},  # turn 2 starts here
        {"type": "block_open", "index": 0, "block": {"kind": "p", "text": "turn2"}},
    ]
    turn_events = QaFinalizer.current_turn_events(events)
    assert turn_events == events[4:]  # strictly AFTER the last meta (index 3)


def test_current_turn_events_no_meta_returns_all():
    """No ``meta`` event at all (a malformed log) ⇒ scope to everything."""
    events = [{"type": "block_open", "index": 0, "block": {"kind": "p", "text": "x"}}]
    assert QaFinalizer.current_turn_events(events) == events


def test_close_does_not_collide_blocks_across_turns(store):
    """A 2nd turn's close must not merge-by-index with the 1st turn's blocks.

    Both turns' ``wiki_emit_answer`` calls restart block indices at 0 — without
    scoping to the current turn, ``_blocks_from_events`` (a dict keyed by index
    over the WHOLE cumulative log) would let turn 2's index-0 block silently
    overwrite turn 1's, and a turn 2 with FEWER blocks would leave turn 1's
    leftover higher-index blocks bleeding through.
    """
    # store fixture already seeded turn 1: meta + access + block_open(0)="The answer."
    # + block_open(1)=sources. Close it first (mirrors QaSessionEndHook's real order).
    assert QaFinalizer.close(store, "a1") is True
    turn1_blocks = store.get_qa("a1").blocks
    assert [b.root.kind for b in turn1_blocks] == ["p", "sources"]

    # Turn 2: a NEW meta (turn boundary) + a single, DIFFERENT block at index 0.
    store.append_qa_event("a1", {"type": "meta", "answerId": "a1"})
    store.append_qa_event("a1", {"type": "block_open", "index": 0,
                                 "block": {"kind": "p", "text": "Turn 2's answer."}})
    assert QaFinalizer.close(store, "a1") is True

    snap = store.get_qa("a1")
    # Turn 2's snapshot is EXACTLY turn 2's one block — no turn-1 "sources" bleed-through.
    assert len(snap.blocks) == 1
    assert snap.blocks[0].root.text.root == "Turn 2's answer."
    assert snap.status == "complete"


def test_close_idempotency_is_per_turn(store):
    """Turn 1 being terminal must not block turn 2's OWN close."""
    assert QaFinalizer.close(store, "a1") is True  # turn 1 closes normally

    store.append_qa_event("a1", {"type": "meta", "answerId": "a1"})
    store.append_qa_event("a1", {"type": "block_open", "index": 0,
                                 "block": {"kind": "p", "text": "Turn 2."}})
    # Must NOT short-circuit as "already terminal" just because turn 1 has a
    # complete event earlier in the cumulative log.
    assert QaFinalizer.close(store, "a1") is True
    assert store.get_qa("a1").blocks[0].root.text.root == "Turn 2."

    # A genuine re-close of the SAME (now-terminal) turn 2 is still idempotent.
    assert QaFinalizer.close(store, "a1") is False


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
    """An explicit summary_ready wins for the curated PAGE half of the cited sources."""
    store.append_qa_event("a1", {"type": "summary_ready",
                                  "sources": ["wiki:overview", "wiki:auth"]})
    QaFinalizer.close(store, "a1")
    # summary_ready pages lead; the file/graph trail still folds in after them.
    assert store.get_qa("a1").summary_sources == [
        "wiki:overview", "wiki:auth", "graph:n7", "src/app.py#L1-20",
    ]


def test_summary_sources_fold_file_graph_from_accessed_trail(store):
    """Cited sources represent file/graph evidence, not just pages.

    Files are the most-read source but the LLM's curated block is ~100%
    page-slugs, so provenance collapses to pages unless the finalizer folds the
    non-page
    refs off the deterministic accessed trail (already bounded + score-ranked by
    ``qa_access``) into ``summary_sources`` — curated pages first, then the files +
    graph symbols the answer actually grounded on, deduped; a ``wiki:`` trail ref is
    NOT re-added (the curated half already owns pages).
    """
    QaFinalizer.close(store, "a1")
    summary = store.get_qa("a1").summary_sources
    assert summary[0] == "wiki:landing-page"        # curated page leads
    assert "src/app.py#L1-20" in summary            # a real source file — was dropped before
    assert "graph:n7" in summary                    # a grounded graph symbol
    assert summary.count("wiki:landing-page") == 1  # trail's page ref not re-added


def test_summary_sources_reschemes_bare_page_ids_and_titles(store):
    """``summary_sources`` re-schemes bare page refs (slug id OR title).

    The first ``wiki_search_pages`` call records a ``summary_ready`` event whose
    ``sources`` are BARE page ids (``h.id``), not ``wiki:``-schemed; surfaced raw
    the FE treats each as a file path and the cited panel 404s. The same
    title→slug normalization the emit seam uses must apply here so a page cited
    by id OR title resolves to ``wiki:<id>`` in the cited-sources panel.
    """
    for pid, title in [("overview", "Project Overview"), ("auth-flow", "Auth Flow")]:
        store.save_page("org/repo", WikiPage(
            id=pid, title=title,
            frontmatter=Frontmatter(title=title, slug=pid), body="# x", toc=[], nav=[],
        ))
    # Mirrors search_pages.py: bare id "overview" + a title-form ref "Auth Flow".
    store.append_qa_event("a1", {"type": "summary_ready",
                                  "sources": ["overview", "Auth Flow"]})
    QaFinalizer.close(store, "a1")
    # Re-schemed pages lead; the file/graph trail folds in after them.
    assert store.get_qa("a1").summary_sources == [
        "wiki:overview", "wiki:auth-flow", "graph:n7", "src/app.py#L1-20",
    ]


def test_terminal_sources_block_closes_via_emit(store, monkeypatch):
    """The one atomic ``wiki_emit_answer`` call IS the accept state: closes + requests terminate."""
    monkeypatch.setattr(
        emit_answer_mod, "_resolve_runtime", lambda: SimpleNamespace(wiki_store=store)
    )
    store.save_qa(QaAnswer(answerId="a2", fromPageId="", summarySources=[],
                           model="m", blocks=[], slug="org/repo"))
    store.attach_qa_session("a2", "sess-2")

    tool = emit_answer_mod.WikiEmitAnswerTool("sess-2")
    step = SimpleNamespace(
        tool_input={"blocks": [
            {"kind": "p", "text": "The answer."},
            {"kind": "sources", "items": ["wiki:x"]},
        ]}
    )
    asyncio.run(tool.handle(step))

    assert tool.should_terminate_run() is True  # the loop stops cleanly here
    assert store.load_qa_events("a2")[-1]["type"] == "complete"
    assert [b.root.kind for b in store.get_qa("a2").blocks] == ["p", "sources"]


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


# ── Backend divergence (the JSON-only tests above cannot see this) ────────────


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


# ── QaMemoryDepositor — the post-QA memory flywheel ─────────────────


SLUG = "org/repo"


def _gn(nid, typ, name, f):
    return make_graph_node(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


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


# ── No-emit failure honesty + the honest unmet-goal assertion (gpt-oss-120b class) ──


def _empty_answer_store(tmp_path, answer_id="e1", session_id="sess-e"):
    """An answer whose run retrieved plenty but NEVER emitted a block."""
    s = JsonWikiStore(root_dir=tmp_path / "wiki-empty")
    s.save_qa(QaAnswer(answerId=answer_id, fromPageId="lp", summarySources=[],
                       model="gpt-oss-120b", blocks=[], slug="org/repo"))
    s.attach_qa_session(answer_id, session_id)
    s.append_qa_event(answer_id, {"type": "meta", "answerId": answer_id})
    s.append_qa_event(answer_id, {"type": "access", "refs": ["graph:n1", "src/a.py#L1-9"]})
    return s


def test_close_no_blocks_no_error_is_error(tmp_path):
    """A run that never emitted is a FAILURE — never an empty 'complete' (#answer 7d2c0ea0)."""
    s = _empty_answer_store(tmp_path)
    assert QaFinalizer.close(s, "e1") is True

    snap = s.get_qa("e1")
    assert snap.status == "error"
    last = s.load_qa_events("e1")[-1]
    assert last["type"] == "error"
    assert "wiki_emit_answer" in last["error"]["message"]


def _hook_runtime(store, transcript=()):
    """Fake runtime for the session-end hook — no re-drive machinery to fake anymore."""
    calls = []
    runtime = SimpleNamespace(
        wiki_store=store,
        load_events=lambda sid: list(transcript),
        start_async=lambda **kw: calls.append(kw),
    )
    return runtime, calls


def test_session_end_hook_closes_silent_answer_and_asserts_unmet_goal(tmp_path):
    """No-error, zero-block end → close honestly AND report the mismatch upward.

    No re-drive is ever attempted: ``on_session_end`` runs from inside the
    run's own ``finally``, so a same-session re-drive could only ever refuse.
    The completion seam's required-terminal gate catches this case in-band;
    this hook is the net for whatever slips past it.
    """
    s = _empty_answer_store(tmp_path)
    runtime, calls = _hook_runtime(s)
    hook = QaSessionEndHook(runtime)

    assertion = hook("sess-e", None)

    assert calls == []  # never attempts a re-drive
    assert s.get_qa("e1").status == "error"
    assert s.load_qa_events("e1")[-1]["type"] == "error"
    assert assertion is not None
    assert assertion.reason == "qa_answer_not_emitted"
    assert "e1" in assertion.detail

    # Idempotent: the answer is already terminal, so a second session-end
    # settles nothing new and reports no further assertion.
    assert hook("sess-e", None) is None


# ── Re-drive turn boundary: a re-open ARMs on block_open, not on meta alone ───


def test_close_recovers_after_error_without_intervening_meta(tmp_path):
    """A re-drive's blocks reconcile even though NO new ``meta`` opened them.

    This is the production defect: run 1 ends without emitting (``close()``
    stamps an ``error`` terminal), then a recovery run calls ``wiki_emit_answer``
    successfully with NO intervening ``meta`` (a re-drive re-engages the same
    turn). Under the OLD "strictly after the last meta" rule the recovery
    run's ``block_open``s still shared a slice with run 1's ``error`` — the
    idempotency guard matched that stale terminal and ``close()`` returned
    False forever, discarding the recovered answer. Verified by hand against
    the old rule (a slice scoped to "after the last meta" over this exact
    event sequence still contains the ``error`` event): it returns False here.
    """
    s = _empty_answer_store(tmp_path, answer_id="r1", session_id="sess-r")
    assert QaFinalizer.close(s, "r1") is True  # run 1: no blocks -> auto "error"
    assert s.get_qa("r1").status == "error"
    events_at_error = len(s.load_qa_events("r1"))

    # Recovery run: block_opens land directly, no new meta.
    s.append_qa_event("r1", {"type": "block_open", "index": 0,
                             "block": {"kind": "p", "text": "Recovered answer."}})
    s.append_qa_event("r1", {"type": "block_open", "index": 1, "block": {
        "kind": "sources", "items": ["wiki:landing-page"]}})
    assert len(s.load_qa_events("r1")) == events_at_error + 2  # sanity: no re-seed

    assert QaFinalizer.close(s, "r1") is True
    snap = s.get_qa("r1")
    assert snap.status == "complete"
    assert [b.root.kind for b in snap.blocks] == ["p", "sources"]
    assert s.load_qa_events("r1")[-1]["type"] == "complete"


def test_close_refuses_on_stray_access_after_complete(store):
    """A straggler ``access`` after a clean ``complete`` must NOT re-open the turn.

    Guards the specific safeguard the new rule adds: a terminal event ALONE
    does not arm the reopen — only an actual ``block_open`` following one
    does. Without that requirement, a straggler probe ``access`` landing
    after a clean answer would form an "empty" current-turn slice (no
    terminal in it), and ``close()`` would re-run — reconciling zero blocks,
    finding neither blocks nor an explicit error, and re-stamping the
    already-delivered answer as ``error``. Confirmed the guard is load-bearing
    by hand-checking an eager alternative (reopen immediately after ANY
    terminal, no ``block_open`` required): over this exact sequence it
    returns True and corrupts the snapshot to ``status: "error"`` — this test
    would fail against that alternative.
    """
    assert QaFinalizer.close(store, "a1") is True
    snap_before = store.get_qa("a1").model_dump(by_alias=True)

    store.append_qa_event("a1", {"type": "access", "refs": ["graph:strayNode"]})

    assert QaFinalizer.close(store, "a1") is False
    assert store.get_qa("a1").model_dump(by_alias=True) == snap_before
    assert store.load_qa_events("a1")[-1]["type"] == "access"  # no new terminal appended


def test_emit_atomicity_guard_admits_followup_turn(store, monkeypatch):
    """A follow-up turn (a fresh ``meta``) is never refused by the emit guard,
    and turn 2's own blocks — not turn 1's — are what gets reconciled.

    An emit guard scanning the WHOLE cumulative log for any ``block_open``
    makes a follow-up turn (which DOES write its own ``meta``) still see turn
    1's blocks and refuse as "already emitted" — so no follow-up ever delivers
    an answer. The guard asks
    ``QaFinalizer.current_turn_events`` (the same seam ``close()`` uses), so
    a fresh ``meta`` resets its view exactly as it resets ``close()``'s.
    """
    monkeypatch.setattr(
        emit_answer_mod, "_resolve_runtime", lambda: SimpleNamespace(wiki_store=store)
    )
    # Turn 1 (seeded by the `store` fixture: meta + access + 2 block_opens) —
    # close it the way the on_session_end net would, so it is terminal.
    assert QaFinalizer.close(store, "a1") is True

    # Turn 2: a genuine follow-up opens with a fresh meta.
    store.append_qa_event("a1", {"type": "meta", "answerId": "a1"})

    tool = emit_answer_mod.WikiEmitAnswerTool("sess-1")
    step = SimpleNamespace(tool_input={"blocks": [
        {"kind": "p", "text": "Turn 2's answer."},
        {"kind": "sources", "items": ["wiki:x"]},
    ]})
    result = asyncio.run(tool.handle(step))

    assert "already emitted" not in str(result.content)
    assert tool.should_terminate_run() is True
    snap = store.get_qa("a1")
    assert snap.status == "complete"
    # Turn 2's blocks, not turn 1's leftover "The answer." / "wiki:landing-page" pair.
    assert len(snap.blocks) == 2
    assert snap.blocks[0].root.text.root == "Turn 2's answer."


def test_close_idempotency_still_short_circuits_current_turns_own_complete(store):
    """The current turn's own ``complete`` still short-circuits a second ``close()``.

    Unchanged contract carried forward under the new rule — a terminal that
    belongs to the SAME turn (nothing re-opened it) must still make ``close()``
    idempotent, exactly as before.
    """
    assert QaFinalizer.close(store, "a1") is True
    n = len(store.load_qa_events("a1"))
    assert QaFinalizer.close(store, "a1") is False
    assert len(store.load_qa_events("a1")) == n


def test_close_leaves_prior_error_standing_when_redrive_emits_nothing(tmp_path):
    """A re-drive that itself emits no blocks leaves the prior ``error`` standing.

    Mirrors the production scenario's OTHER outcome: if the recovery run
    probes but never reaches ``wiki_emit_answer``, there is no ``block_open``
    to arm the reopen, so the slice still contains run 1's ``error`` and
    ``close()`` correctly refuses (idempotent no-op) rather than silently
    reconciling an empty "complete".
    """
    s = _empty_answer_store(tmp_path, answer_id="r2", session_id="sess-r2")
    assert QaFinalizer.close(s, "r2") is True  # run 1: no blocks -> auto "error"
    snap_before = s.get_qa("r2").model_dump(by_alias=True)

    # Re-drive probes something but never emits a block.
    s.append_qa_event("r2", {"type": "access", "refs": ["graph:n9"]})

    assert QaFinalizer.close(s, "r2") is False
    assert s.get_qa("r2").model_dump(by_alias=True) == snap_before
    assert s.load_qa_events("r2")[-1]["type"] == "access"


def test_session_end_hook_returns_no_assertion_when_already_settled(tmp_path):
    """The mismatch report only fires for the silent-empty case — never on
    blocks/error/cancel, each of which already reached (or explains) its
    terminal state through some other path."""
    # (a) blocks present AND already closed (wiki_emit_answer's own close()
    #     already ran, as it does on the real happy path), no assertion.
    s1 = _empty_answer_store(tmp_path, answer_id="b1", session_id="sess-b")
    s1.append_qa_event("b1", {"type": "block_open", "index": 0,
                              "block": {"kind": "p", "text": "x"}})
    assert QaFinalizer.close(s1, "b1") is True
    r1, c1 = _hook_runtime(s1)
    assert QaSessionEndHook(r1)("sess-b", None) is None
    assert c1 == [] and s1.get_qa("b1").status == "complete"

    # (b) session error → honest error close, no assertion — the loop's own
    #     done_reason already carries the failure; nothing left to promote.
    s2 = _empty_answer_store(tmp_path, answer_id="x1", session_id="sess-x")
    r2, c2 = _hook_runtime(s2)
    assert QaSessionEndHook(r2)("sess-x", "model exploded") is None
    assert c2 == [] and s2.get_qa("x1").status == "error"

    # (c) already cancelled → close() no-ops, no assertion.
    s3 = _empty_answer_store(tmp_path, answer_id="c1", session_id="sess-c")
    s3.append_qa_event("c1", {"type": "cancelled"})
    r3, c3 = _hook_runtime(s3)
    assert QaSessionEndHook(r3)("sess-c", None) is None
    assert c3 == []
    assert not any(e.get("type") in ("complete", "error")
                   for e in s3.load_qa_events("c1") if e.get("type") != "cancelled")
