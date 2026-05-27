"""Tests for ScgMemoryBridge + ScgAnchorResolver — the learned-layer flywheel.

The bridge reuses #13's memory substrate (``InsightIngestor`` +
``memory_vector_search``) with ``corpus="connector"`` instead of re-implementing
atomic-note/anchor machinery. The two correctness properties under test:

* **Anchors resolve** — ``ScgAnchorResolver`` (a ``StructureProvider``) maps
  ``source_key`` anchors to live ``ScgNode``s, so the ingestor creates a live
  ``ANCHORS`` edge. Without that edge, ``memory_vector_search`` (default
  ``exclude_invalidated=True``) would silently drop the insight.
* **Corpus isolation** — ``read_insights`` filters on ``corpus="connector"`` so
  connector insights never bleed into the code corpus and vice-versa.

Real wiki JSON store in a tmp dir, a fake embedder (no proxy), no LLM.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.memory_bridge import (
    CONNECTOR_SLUG,
    ScgAnchorResolver,
    ScgMemoryBridge,
)
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import ScgNode
from mewbo_graph.wiki.memory import InsightIngestor
from mewbo_graph.wiki.memory_types import MemoryNode

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Deterministic keyword-bag embedder — no proxy, no LLM.

    Each text maps to a small fixed-width vector of token-presence counts over a
    closed vocabulary, so cosine similarity is meaningful and reproducible.
    """

    _VOCAB = ["repo", "issue", "user", "rate", "limit", "field", "id", "github"]

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [float(low.count(tok)) for tok in self._VOCAB]

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list[MemoryNode]:
        from mewbo_graph.wiki.types import Embedding

        return [
            Embedding(
                slug=slug, node_id=nid, vector=self._vec(text), model="fake", dim=len(self._VOCAB)
            )
            for nid, text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


# ── Fixtures ─────────────────────────────────────────────────────────────────
#
# ``wiki_store`` is provided by the sibling ``conftest.py`` (auto-injected for
# every test in this directory) — don't redefine it here.


@pytest.fixture()
def scg_store(tmp_path: Path) -> JsonScgStore:
    """A fresh SCG JSON store seeded with two connector nodes."""
    store = JsonScgStore(root_dir=tmp_path / "scg")
    store.upsert_nodes(
        [
            ScgNode(source_key="github#Repo", kind="entity_type", source_id="github", name="Repo"),
            ScgNode(
                source_key="github#Issue", kind="entity_type", source_id="github", name="Issue"
            ),
        ]
    )
    return store


@pytest.fixture()
def bridge(wiki_store, scg_store: JsonScgStore) -> ScgMemoryBridge:
    """A bridge wired to the real wiki store, a fake embedder, and no LLM."""
    b = ScgMemoryBridge(wiki_store=wiki_store, embedder=_FakeEmbedder(), llm=None)
    # The resolver is what makes connector anchors resolve; wire it explicitly.
    b.resolver = ScgAnchorResolver(scg_store)
    return b


# ── ScgAnchorResolver ────────────────────────────────────────────────────────


def test_resolver_resolves_known_source_keys(scg_store: JsonScgStore) -> None:
    """resolve_many returns a dict for keys that exist; misses are omitted."""
    resolver = ScgAnchorResolver(scg_store)
    out = resolver.resolve_many(CONNECTOR_SLUG, ["github#Repo", "github#Missing"])
    assert set(out) == {"github#Repo"}
    assert resolver.resolve(CONNECTOR_SLUG, "github#Repo") is not None
    assert resolver.resolve(CONNECTOR_SLUG, "github#Missing") is None


def test_resolver_satisfies_structure_provider_protocol(scg_store: JsonScgStore) -> None:
    """ScgAnchorResolver is a structural StructureProvider (the #13 seam)."""
    from mewbo_graph.wiki.structure_provider import StructureProvider

    assert isinstance(ScgAnchorResolver(scg_store), StructureProvider)


# ── write_insight → read_insights round-trip ─────────────────────────────────


def test_write_then_read_returns_insight(bridge: ScgMemoryBridge) -> None:
    """An insight written with a resolvable anchor is retrievable by query."""
    res = bridge.write_insight(
        CONNECTOR_SLUG,
        "github Repo entity is queryable by id",
        source_keys=["github#Repo"],
    )
    assert res.ok
    qvec = _FakeEmbedder().embed_query("repo id github")
    hits = bridge.read_insights(CONNECTOR_SLUG, qvec, k=5)
    contents = [n.content for n in hits]
    assert "github Repo entity is queryable by id" in contents


def test_anchor_creates_live_anchors_edge(bridge: ScgMemoryBridge, wiki_store) -> None:
    """The resolved anchor produces a live ANCHORS edge to the source_key."""
    res = bridge.write_insight(
        CONNECTOR_SLUG, "github Issue has a user field", source_keys=["github#Issue"]
    )
    node_id = res.claims[0].node_id
    assert node_id is not None
    edges = wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=node_id)
    anchors = [e for e in edges if e.type == "ANCHORS"]
    assert [e.target for e in anchors] == ["github#Issue"]


def test_unresolvable_anchor_is_dropped(bridge: ScgMemoryBridge, wiki_store) -> None:
    """An anchor with no matching ScgNode is dropped (no ANCHORS edge)."""
    res = bridge.write_insight(
        CONNECTOR_SLUG, "github user rate limit applies", source_keys=["github#Ghost"]
    )
    node_id = res.claims[0].node_id
    assert node_id is not None
    edges = wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=node_id)
    assert [e for e in edges if e.type == "ANCHORS"] == []
    assert any("dropped unresolved anchor" in w for w in res.claims[0].warnings)


# ── corpus isolation ─────────────────────────────────────────────────────────


def test_corpus_filter_isolates_connector_from_code(bridge: ScgMemoryBridge, wiki_store) -> None:
    """read_insights returns only connector-corpus notes, never code-corpus ones."""
    # A connector insight via the bridge (corpus="connector", anchored).
    bridge.write_insight(
        CONNECTOR_SLUG, "github Repo field id is bound", source_keys=["github#Repo"]
    )
    # A code-corpus note sharing vocabulary, written directly to the same slug
    # via the same ingestor path but corpus="code" + a (fake) live anchor edge.
    # The SCG resolver is passed at construction so the "id" anchor resolves for
    # both notes (no post-construction private mutation).
    ing = InsightIngestor.from_store(
        wiki_store, embedder=_FakeEmbedder(), provider=bridge.resolver
    )
    ing.ingest(
        CONNECTOR_SLUG,
        "github Repo id is a code symbol",
        anchors=["github#Repo"],
        corpus="code",
    )

    qvec = _FakeEmbedder().embed_query("github repo id")
    hits = bridge.read_insights(CONNECTOR_SLUG, qvec, k=10)
    contents = [n.content for n in hits]
    assert "github Repo field id is bound" in contents
    assert "github Repo id is a code symbol" not in contents
    assert all(n.corpus == "connector" for n in hits)


def test_read_insights_empty_when_no_connector_notes(bridge: ScgMemoryBridge) -> None:
    """No connector notes ⇒ empty result (BM25/cosine over an empty pool)."""
    qvec = _FakeEmbedder().embed_query("anything")
    assert bridge.read_insights(CONNECTOR_SLUG, qvec, k=5) == []
