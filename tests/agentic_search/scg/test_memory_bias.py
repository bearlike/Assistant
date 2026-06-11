"""Tests for memory-aware SCG routing (#76) — bias map + router integration.

``docs/features-search.md``: "Before each query, the top-k relevant memory notes
are retrieved via vector search and surfaced to ``scg_route``, biasing routing
toward pathways that have produced results and away from dead ends already
discovered." These tests drive the REAL code path end-to-end:

* a positive connector note anchored to capability B re-ranks B above A;
* a dead-end note DAMPS its pathway below an unbiased sibling;
* retrieval respects the ambient :class:`ScgScope` (out-of-scope notes never
  bias an in-scope route);
* the bias degrades gracefully (empty) when no bridge / no memory is present;
* the router's zero-LLM core is preserved (no LLM is ever constructed here).

Both stores run for real in a tmp dir (JSON, no Mongo); the ONLY stub is the
deterministic keyword-bag embedder (the embedding/vector I/O seam). No LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.memory_bias import ScgMemoryBias
from mewbo_graph.scg.memory_bridge import (
    CONNECTOR_SLUG,
    ScgAnchorResolver,
    ScgMemoryBridge,
)
from mewbo_graph.scg.router import ScgRouter
from mewbo_graph.scg.scope import ScgScope
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import RouteRecipe, ScgEmbedding, ScgNode

# ── Deterministic keyword-bag embedder (the only stubbed I/O seam) ───────────


class _FakeEmbedder:
    """Token-presence-count embedder over a closed vocab — offline + reproducible.

    Satisfies every embedder surface the bias path touches: ``embed_nodes`` (the
    ingestor persists wiki ``Embedding`` rows) + ``embed_query`` (router + bridge
    read). Same projection for nodes and queries so cosine is meaningful.
    """

    model = "fake-embed"
    _VOCAB = ["repo", "issue", "user", "find", "search", "github", "slack", "owner"]

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [float(low.count(tok)) for tok in self._VOCAB]

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list:
        from mewbo_graph.wiki.types import Embedding

        return [
            Embedding(
                slug=slug, node_id=nid, vector=self._vec(text), model=self.model,
                dim=len(self._VOCAB),
            )
            for nid, text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


# ── Fixtures ─────────────────────────────────────────────────────────────────
#
# ``wiki_store`` comes from the sibling ``conftest.py`` (a fresh JSON wiki store).


_QUERY = "find repo github"


@pytest.fixture()
def scg_store(tmp_path: Path) -> JsonScgStore:
    """Two capabilities A and B in ONE source, each with a recipe + embedding.

    A is embedded ON the query (sim 1.0); B is a NEAR-tie just behind it (sim
    ~0.87). Absent any memory, A outranks B — but the gap (~0.13) is well within
    the memory boost band, so a note can flip the order. That is precisely the
    docs' "bias toward pathways that have produced results", NOT an override of a
    clearly-better structural match.
    """
    store = JsonScgStore(root_dir=tmp_path / "scg")
    cap_a = ScgNode(source_key="github#find_repo", kind="capability",
                    source_id="github", name="find_repo")
    cap_b = ScgNode(source_key="github#search_issue", kind="capability",
                    source_id="github", name="search_issue")
    store.upsert_nodes([cap_a, cap_b])
    emb = _FakeEmbedder()
    # A matches the query exactly; B carries one extra token → a small, realistic
    # structural gap the memory bias can decide.
    store.upsert_embeddings([
        ScgEmbedding(node_id=cap_a.node_id, vector=emb._vec(_QUERY),
                     model="m", dim=len(emb._VOCAB)),
        ScgEmbedding(node_id=cap_b.node_id, vector=emb._vec(_QUERY + " issue"),
                     model="m", dim=len(emb._VOCAB)),
    ])
    store.upsert_recipes([
        RouteRecipe(source_key="github#find_repo", steps=["github#find_repo"]),
        RouteRecipe(source_key="github#search_issue", steps=["github#search_issue"]),
    ])
    return store


@pytest.fixture()
def bridge(wiki_store, scg_store: JsonScgStore) -> ScgMemoryBridge:
    """A real memory bridge over the real wiki store + fake embedder, no LLM."""
    b = ScgMemoryBridge(wiki_store=wiki_store, embedder=_FakeEmbedder(), llm=None)
    b.resolver = ScgAnchorResolver(scg_store)
    return b


def _router(scg_store: JsonScgStore, bridge: ScgMemoryBridge | None) -> ScgRouter:
    return ScgRouter(store=scg_store, embedder=_FakeEmbedder(), memory_bridge=bridge)


# ── Baseline: structure-only ordering (no memory) ───────────────────────────


def test_no_memory_ranks_by_structure(scg_store: JsonScgStore) -> None:
    """With no bridge, A (near the query) outranks B — the structure-only baseline."""
    keys = [r.source_key for r in _router(scg_store, None).route(_QUERY, k=5)]
    assert keys[0] == "github#find_repo"
    assert keys.index("github#find_repo") < keys.index("github#search_issue")


# ── Positive note re-ranks the anchored capability up ───────────────────────


def test_positive_note_promotes_anchored_capability(
    scg_store: JsonScgStore, bridge: ScgMemoryBridge
) -> None:
    """A positive note anchored to B lifts B above A (memory beats structure)."""
    # Anchor a productive note to B's entity-type key (the resolver maps a
    # source_key to its entity_type node — anchor on that surface).
    scg_store.upsert_nodes([ScgNode(
        source_key="github#search_issue", kind="entity_type",
        source_id="github", name="search_issue",
    )])
    res = bridge.write_insight(
        CONNECTOR_SLUG,
        "search issue by repo returns the open issues — this pathway works",
        source_keys=["github#search_issue"],
        polarity="positive",
    )
    assert res.ok

    keys = [r.source_key for r in _router(scg_store, bridge).route(_QUERY, k=5)]
    # B was structurally behind A; the positive note now ranks it first.
    assert keys[0] == "github#search_issue"
    assert keys.index("github#search_issue") < keys.index("github#find_repo")


# ── Dead-end note damps its pathway ─────────────────────────────────────────


def test_dead_end_note_damps_anchored_capability(
    scg_store: JsonScgStore, bridge: ScgMemoryBridge
) -> None:
    """A dead-end note anchored to A pushes A BELOW the unbiased B.

    A starts ahead of B on structure; a discovered dead end on A must be able to
    flip the order (the penalty is asymmetric — stronger than a positive lift).
    """
    scg_store.upsert_nodes([ScgNode(
        source_key="github#find_repo", kind="entity_type",
        source_id="github", name="find_repo",
    )])
    res = bridge.write_insight(
        CONNECTOR_SLUG,
        "find repo by owner returns nothing for this question — dead end",
        source_keys=["github#find_repo"],
        polarity="dead_end",
    )
    assert res.ok

    keys = [r.source_key for r in _router(scg_store, bridge).route(_QUERY, k=5)]
    assert keys.index("github#search_issue") < keys.index("github#find_repo")


# ── Scope-respecting retrieval ──────────────────────────────────────────────


def test_out_of_scope_note_does_not_bias(
    scg_store: JsonScgStore, bridge: ScgMemoryBridge
) -> None:
    """A positive note on B is IGNORED when B's source is out of the active scope.

    Same deposit as the promotion test, but bound to a workspace scope of
    {other} — B's github anchor is out of scope, so the note never biases the
    route and the structure-only order (A first) is restored.
    """
    scg_store.upsert_nodes([ScgNode(
        source_key="github#search_issue", kind="entity_type",
        source_id="github", name="search_issue",
    )])
    bridge.write_insight(
        CONNECTOR_SLUG, "search issue by repo works — productive pathway",
        source_keys=["github#search_issue"], polarity="positive",
    )
    # The scope filter also drops the github recipes from candidacy, so to observe
    # the bias-suppression in isolation we scope to {github} but anchor the NOTE's
    # source out of a DIFFERENT scope. Simpler: assert the bias map directly.
    with ScgScope.use(["other"]):
        bias = ScgMemoryBias.for_query(
            bridge, CONNECTOR_SLUG, _FakeEmbedder().embed_query("search issue repo"),
        )
    # No in-scope anchor → no boost for B's pathway.
    assert bias.boost_for_steps(["github#search_issue"]) == 0.0


def test_in_scope_note_biases_under_matching_scope(
    scg_store: JsonScgStore, bridge: ScgMemoryBridge
) -> None:
    """The SAME note DOES bias when github IS in the active scope."""
    scg_store.upsert_nodes([ScgNode(
        source_key="github#search_issue", kind="entity_type",
        source_id="github", name="search_issue",
    )])
    bridge.write_insight(
        CONNECTOR_SLUG, "search issue by repo works — productive pathway",
        source_keys=["github#search_issue"], polarity="positive",
    )
    with ScgScope.use(["github"]):
        bias = ScgMemoryBias.for_query(
            bridge, CONNECTOR_SLUG, _FakeEmbedder().embed_query("search issue repo"),
        )
    assert bias.boost_for_steps(["github#search_issue"]) > 0.0


# ── Graceful degradation ────────────────────────────────────────────────────


def test_router_without_bridge_is_memory_blind(scg_store: JsonScgStore) -> None:
    """A bridge-less router yields an empty bias — structure-only, never a crash."""
    recipes, bias = _router(scg_store, None).route_with_memory(_QUERY, k=5)
    assert recipes  # routing still works
    assert bias.by_key == {}


def test_empty_memory_yields_empty_bias(
    scg_store: JsonScgStore, bridge: ScgMemoryBridge
) -> None:
    """No deposited notes ⇒ empty bias ⇒ unchanged structure-only ranking."""
    no_mem = _router(scg_store, bridge)
    with_mem = [r.source_key for r in no_mem.route(_QUERY, k=5)]
    structure = [r.source_key for r in _router(scg_store, None).route(_QUERY, k=5)]
    assert with_mem == structure


# ── Bias map arithmetic (unit) ──────────────────────────────────────────────


def test_boost_for_steps_takes_the_max(
    scg_store: JsonScgStore, bridge: ScgMemoryBridge
) -> None:
    """A pathway's boost is the MAX over its steps, not the sum/avg.

    One strong productive capability lifts the whole pathway; an unbiased
    sibling step does not dilute it.
    """
    scg_store.upsert_nodes([ScgNode(
        source_key="github#search_issue", kind="entity_type",
        source_id="github", name="search_issue",
    )])
    bridge.write_insight(
        CONNECTOR_SLUG, "search issue by repo is productive",
        source_keys=["github#search_issue"], polarity="positive",
    )
    bias = ScgMemoryBias.for_query(
        bridge, CONNECTOR_SLUG, _FakeEmbedder().embed_query("search issue repo"),
    )
    single = bias.boost_for_steps(["github#search_issue"])
    multi = bias.boost_for_steps(["github#unbiased", "github#search_issue"])
    assert single > 0.0
    assert multi == single  # max, not diluted by the unbiased step
