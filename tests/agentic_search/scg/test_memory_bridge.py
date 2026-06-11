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
def capability_scg_store(tmp_path: Path) -> JsonScgStore:
    """An SCG store seeded with ``capability`` nodes — the MCP-tool-list shape.

    This is the kind an MCP-tool-list source maps to (every tool → one
    ``capability`` node), and the kind the deployed graph carried when connector
    insights were written edge-less: the resolver hard-coded ``entity_type`` and
    silently dropped every ``capability`` anchor. The seam test MUST seed this
    shape — seeding ``entity_type`` (as the legacy fixture did) is exactly the
    stub that hid the bug.
    """
    store = JsonScgStore(root_dir=tmp_path / "scg")
    store.upsert_nodes(
        [
            ScgNode(
                source_key="wikidata#execute_sparql",
                kind="capability",
                source_id="wikidata",
                name="execute_sparql",
            ),
            ScgNode(
                source_key="wikidata#search_items",
                kind="capability",
                source_id="wikidata",
                name="search_items",
            ),
        ]
    )
    return store


@pytest.fixture()
def capability_bridge(wiki_store, capability_scg_store: JsonScgStore) -> ScgMemoryBridge:
    """A bridge over a ``capability``-only SCG (the deployed MCP-tool-list shape)."""
    b = ScgMemoryBridge(wiki_store=wiki_store, embedder=_FakeEmbedder(), llm=None)
    b.resolver = ScgAnchorResolver(capability_scg_store)
    return b


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


# ── polarity + workspace attribution (#76) ───────────────────────────────────


def test_write_records_default_positive_polarity(bridge: ScgMemoryBridge) -> None:
    """A deposit with no explicit polarity reads back as positive evidence."""
    from mewbo_graph.scg.memory_bridge import polarity_label, polarity_of

    res = bridge.write_insight(
        CONNECTOR_SLUG, "github Repo is queryable by id", source_keys=["github#Repo"]
    )
    qvec = _FakeEmbedder().embed_query("repo id github")
    note = next(n for n in bridge.read_insights(CONNECTOR_SLUG, qvec, k=5))
    assert res.ok
    assert polarity_label("positive") in note.labels
    assert polarity_of(note) == "positive"


def test_write_dead_end_polarity_round_trips(bridge: ScgMemoryBridge) -> None:
    """A dead-end deposit carries the dead_end label and reads back as dead_end."""
    from mewbo_graph.scg.memory_bridge import polarity_label, polarity_of

    bridge.write_insight(
        CONNECTOR_SLUG, "github Issue search by user returns nothing here",
        source_keys=["github#Issue"], polarity="dead_end",
    )
    qvec = _FakeEmbedder().embed_query("issue user github")
    note = next(n for n in bridge.read_insights(CONNECTOR_SLUG, qvec, k=5))
    assert polarity_label("dead_end") in note.labels
    assert polarity_of(note) == "dead_end"


def test_write_workspace_is_attribution_not_partition(bridge: ScgMemoryBridge) -> None:
    """A workspace tag is recorded as a label but never partitions reads.

    The note is deposited under workspace ``ws-a`` but a plain (workspace-less)
    read still surfaces it — cross-pollination, per ``docs/features-search.md``.
    """
    bridge.write_insight(
        CONNECTOR_SLUG, "github Repo field id is bound",
        source_keys=["github#Repo"], workspace="ws-a",
    )
    qvec = _FakeEmbedder().embed_query("repo id github")
    note = next(n for n in bridge.read_insights(CONNECTOR_SLUG, qvec, k=5))
    assert "ws:ws-a" in note.labels  # attribution recorded
    # No partition: a read with no workspace bound still returns the note.
    assert "github Repo field id is bound" == note.content


# ── capability-anchored deposits (the deployed MCP-tool-list bug, #81-A) ─────
#
# The REAL seam: an MCP-tool-list source maps every tool to a ``capability``
# node, but the resolver used to hard-code ``ScgNode.make_id(source_key,
# "entity_type")`` — deriving a node id that never existed for a capability —
# so every connector anchor was dropped, no ANCHORS edge was written, and
# ``memory_vector_search`` (default ``exclude_invalidated=True``) silently
# dropped the note on read. These tests pin the capability shape so the fix
# can't regress; they would all FAIL against the pre-fix resolver.


def test_capability_anchor_creates_live_anchors_edge(
    capability_bridge: ScgMemoryBridge, wiki_store
) -> None:
    """(1) A capability source_key anchor produces a live ANCHORS edge."""
    res = capability_bridge.write_insight(
        CONNECTOR_SLUG,
        "github issue rate limit field applies",
        source_keys=["wikidata#execute_sparql"],
    )
    assert res.ok
    node_id = res.claims[0].node_id
    assert node_id is not None
    edges = wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=node_id)
    anchors = [e for e in edges if e.type == "ANCHORS"]
    assert [e.target for e in anchors] == ["wikidata#execute_sparql"]
    # The anchor was NOT dropped — no "dropped unresolved anchor" warning.
    assert not any("dropped unresolved anchor" in w for w in res.claims[0].warnings)


def test_capability_anchor_persists_embedding(
    capability_bridge: ScgMemoryBridge, wiki_store
) -> None:
    """(2) The deposit persists a connector embedding for the note."""
    res = capability_bridge.write_insight(
        CONNECTOR_SLUG,
        "github issue field id is bound",
        source_keys=["wikidata#search_items"],
    )
    node_id = res.claims[0].node_id
    # The embedding is keyed by node_id under the connector slug; a brute-force
    # vector search over the whole pool must surface it.
    qvec = _FakeEmbedder().embed_query("github issue field id")
    found = wiki_store.memory_vector_search(CONNECTOR_SLUG, qvec, k=10)
    assert any(e.node_id == node_id for e in found)


def test_capability_anchor_round_trips_via_read_insights(
    capability_bridge: ScgMemoryBridge,
) -> None:
    """(3) read_insights returns the capability-anchored note (edge-gated read)."""
    capability_bridge.write_insight(
        CONNECTOR_SLUG,
        "github issue user rate limit",
        source_keys=["wikidata#execute_sparql"],
    )
    qvec = _FakeEmbedder().embed_query("github issue user rate limit")
    hits = capability_bridge.read_insights(CONNECTOR_SLUG, qvec, k=5)
    assert "github issue user rate limit" in [n.content for n in hits]


def test_capability_note_surfaces_in_scg_graph_view_memory_layer(
    capability_bridge: ScgMemoryBridge, wiki_store, capability_scg_store: JsonScgStore
) -> None:
    """(4) ScgGraphView includes the note + cross ANCHORS in the memory layer."""
    from mewbo_graph.scg.graph_view import ScgGraphView

    res = capability_bridge.write_insight(
        CONNECTOR_SLUG,
        "github issue rate limit field applies",
        source_keys=["wikidata#execute_sparql"],
    )
    note_id = res.claims[0].node_id

    view = ScgGraphView.for_scope(
        capability_scg_store, wiki_store, source_ids=["wikidata"]
    )
    # The note is in the memory layer (it had an in-scope live anchor).
    assert note_id in {n.node_id for n in view.memory_nodes}
    # A reconciled cross-layer ANCHORS edge ties the note to the capability node.
    cap_node_id = ScgNode.make_id("wikidata#execute_sparql", "capability")
    assert (note_id, cap_node_id) in view.cross_edges
    wire = view.to_wire()
    assert wire["stats"]["perLayer"]["memory"] >= 1


def test_read_anchored_insights_returns_score_and_anchors(
    bridge: ScgMemoryBridge,
) -> None:
    """The bias-feed read returns (note, score, anchors) with the live anchor."""
    bridge.write_insight(
        CONNECTOR_SLUG, "github Repo is queryable by id", source_keys=["github#Repo"]
    )
    qvec = _FakeEmbedder().embed_query("repo id github")
    rows = bridge.read_anchored_insights(CONNECTOR_SLUG, qvec, k=5)
    assert rows
    note, score, anchors = rows[0]
    assert note.content == "github Repo is queryable by id"
    assert anchors == ["github#Repo"]
    assert score > 0.0  # the query overlaps the note's vocabulary
