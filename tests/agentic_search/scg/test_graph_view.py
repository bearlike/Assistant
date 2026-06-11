"""Tests for ScgGraphView — the SCG multiplex assembler (#76).

The search-side mirror of the wiki ``KnowledgeGraphView``: it unifies the three
SCG-tenant layers (schema + memory + entity) for a source-id scope and reconciles
cross-layer ANCHORS to real node ids. These tests prove:

* assembly of all layers for a scope (schema nodes/edges + connector memory
  notes + their note↔note RELATES);
* cross-layer ANCHORS reconciliation (memory note → its anchored schema node,
  endpoints both real in the payload — no dangling edge);
* scope hygiene (a node / note / edge whose source is out of scope is dropped);
* the wire shape is self-contained + layer-tagged + redacts ``auth_scope``.

Both stores run for real in a tmp dir (JSON, no Mongo); the ONLY stub is the
deterministic embedder (the vector I/O seam). No LLM, no app/Flask import.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.graph_view import ScgGraphView
from mewbo_graph.scg.memory_bridge import (
    CONNECTOR_SLUG,
    ScgAnchorResolver,
    ScgMemoryBridge,
)
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import ScgEdge, ScgNode


class _FakeEmbedder:
    """Token-presence embedder — offline, deterministic (the only stubbed seam)."""

    model = "fake-embed"
    _VOCAB = ["repo", "issue", "id", "field", "github", "slack"]

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [float(low.count(t)) for t in self._VOCAB]

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list:
        from mewbo_graph.wiki.types import Embedding

        return [
            Embedding(slug=slug, node_id=nid, vector=self._vec(text),
                      model=self.model, dim=len(self._VOCAB))
            for nid, text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture()
def scg_store(tmp_path: Path) -> JsonScgStore:
    """A two-source SCG: github (Repo entity + search capability) + slack (out)."""
    store = JsonScgStore(root_dir=tmp_path / "scg")
    store.upsert_nodes([
        ScgNode(source_key="github#Repo", kind="entity_type",
                source_id="github", name="Repo", doc="A repository.",
                auth_scope="oauth:repo"),
        ScgNode(source_key="github#search", kind="capability",
                source_id="github", name="search"),
        ScgNode(source_key="slack#Channel", kind="entity_type",
                source_id="slack", name="Channel"),
    ])
    store.upsert_edges([
        ScgEdge(source="github#search", target="github#Repo", kind="PRODUCES"),
        # A cross-source RESOLVES_TO to slack (out of a github-only scope).
        ScgEdge(source="github#Repo", target="slack#Channel", kind="RESOLVES_TO"),
    ])
    return store


@pytest.fixture()
def bridge(wiki_store, scg_store: JsonScgStore) -> ScgMemoryBridge:
    """A real memory bridge anchored to the SCG store (no LLM)."""
    b = ScgMemoryBridge(wiki_store=wiki_store, embedder=_FakeEmbedder(), llm=None)
    b.resolver = ScgAnchorResolver(scg_store)
    return b


# ── assembly ────────────────────────────────────────────────────────────────


def test_assembles_schema_layer_for_scope(
    scg_store: JsonScgStore, wiki_store
) -> None:
    """A github-only scope surfaces github schema nodes, drops slack + its edge."""
    view = ScgGraphView.for_scope(scg_store, wiki_store, ["github"])
    keys = {n.source_key for n in view.schema_nodes}
    assert keys == {"github#Repo", "github#search"}
    # The PRODUCES edge (both endpoints in scope) is kept; the RESOLVES_TO to
    # slack (out-of-scope endpoint) is dropped — no dangling edge.
    edge_kinds = {e.kind for e in view.schema_edges}
    assert edge_kinds == {"PRODUCES"}


def test_assembles_memory_layer_and_cross_anchors(
    scg_store: JsonScgStore, wiki_store, bridge: ScgMemoryBridge
) -> None:
    """A connector note anchored to github#Repo joins the view as a cross edge."""
    res = bridge.write_insight(
        CONNECTOR_SLUG, "github Repo is queryable by id",
        source_keys=["github#Repo"],
    )
    note_id = res.claims[0].node_id

    view = ScgGraphView.for_scope(scg_store, wiki_store, ["github"])

    assert [n.node_id for n in view.memory_nodes] == [note_id]
    # The cross edge points memory note → the github#Repo SCHEMA node id (a real
    # node in the payload), not the raw source_key. The view resolves the anchor
    # through ScgAnchorResolver, which is kind-agnostic — github#Repo is the
    # entity_type node this fixture seeds.
    repo_node = ScgAnchorResolver(scg_store).resolve(CONNECTOR_SLUG, "github#Repo")
    assert repo_node is not None
    assert (note_id, repo_node.node_id) in view.cross_edges


def test_out_of_scope_note_is_dropped(
    scg_store: JsonScgStore, wiki_store, bridge: ScgMemoryBridge
) -> None:
    """A note anchored only to slack does NOT surface in a github-only view."""
    # Give slack an entity_type node so the anchor resolves (then scope it out).
    scg_store.upsert_nodes([ScgNode(
        source_key="slack#Channel", kind="entity_type",
        source_id="slack", name="Channel",
    )])
    bridge.write_insight(
        CONNECTOR_SLUG, "slack Channel is queryable by name",
        source_keys=["slack#Channel"],
    )
    view = ScgGraphView.for_scope(scg_store, wiki_store, ["github"])
    assert view.memory_nodes == ()
    assert view.cross_edges == ()


def test_empty_scope_is_empty_view(scg_store: JsonScgStore, wiki_store) -> None:
    """An empty source list yields an empty view (never the whole catalog)."""
    view = ScgGraphView.for_scope(scg_store, wiki_store, [])
    assert view.schema_nodes == ()
    assert view.schema_edges == ()
    assert view.memory_nodes == ()


# ── wire shape ──────────────────────────────────────────────────────────────


def test_to_wire_is_layer_tagged_and_redacts_auth(
    scg_store: JsonScgStore, wiki_store, bridge: ScgMemoryBridge
) -> None:
    """Every node/edge carries a layer tag; auth_scope never reaches the wire."""
    bridge.write_insight(
        CONNECTOR_SLUG, "github Repo is queryable by id",
        source_keys=["github#Repo"],
    )
    wire = ScgGraphView.for_scope(scg_store, wiki_store, ["github"]).to_wire()

    assert wire["scope"] == ["github"]
    layers = {n["data"]["layer"] for n in wire["nodes"]}
    assert layers == {"schema", "memory"}
    edge_layers = {e["data"]["layer"] for e in wire["edges"]}
    assert "cross" in edge_layers  # the reconciled ANCHORS
    # auth_scope is redacted — the descriptor string never appears anywhere.
    assert "oauth:repo" not in repr(wire)
    # Self-contained stats with per-layer breakdown.
    assert set(wire["stats"]["perLayer"]) == {"schema", "memory", "entity"}
    assert wire["stats"]["totalNodes"] == len(wire["nodes"])


def test_to_wire_cross_edge_endpoints_are_real_nodes(
    scg_store: JsonScgStore, wiki_store, bridge: ScgMemoryBridge
) -> None:
    """A cross edge's endpoints are both real node ids in the payload (no dangling)."""
    bridge.write_insight(
        CONNECTOR_SLUG, "github Repo is queryable by id",
        source_keys=["github#Repo"],
    )
    wire = ScgGraphView.for_scope(scg_store, wiki_store, ["github"]).to_wire()
    node_ids = {n["data"]["id"] for n in wire["nodes"]}
    for edge in wire["edges"]:
        if edge["data"]["layer"] == "cross":
            assert edge["data"]["source"] in node_ids
            assert edge["data"]["target"] in node_ids
