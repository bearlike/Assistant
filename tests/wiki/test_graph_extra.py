"""Extra coverage for mewbo_graph.wiki.graph.

Covers the uncovered branches:
- GraphIndex.__init__() ImportError path (lines 80-81): when tree_sitter
  extras are absent, a clean ImportError with a helpful message is raised.
- GraphParseResult.__add__(): merge operator concatenates nodes/edges/skipped.
- KnowledgeGraphView.for_slug(): full load path without node_limit, with
  node_limit below total count (degree-rank cap), and with node_limit at or
  above total count (no-op cap).
- KnowledgeGraphView.node_count / edge_count / kinds properties.
- KnowledgeGraphView.to_wire(): wire-shape structure, stats fields,
  ``truncated`` flag.
- KnowledgeGraphView._node_to_wire / _edge_to_wire: key names and values.
- Empty graph (no nodes/edges): for_slug still returns a valid view.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from mewbo_graph.wiki.graph import GraphParseResult, KnowledgeGraphView
from mewbo_graph.wiki.types import GraphEdge, GraphNode

# ── helpers ────────────────────────────────────────────────────────────────────

SLUG = "org/repo"


def _gn(nid: str, name: str, typ: str = "Function", f: str = "a.py") -> GraphNode:
    return GraphNode(
        slug=SLUG,
        node_id=nid,
        type=typ,
        name=name,
        file=f,
        range=(0, 100),
        docstring=f"Doc for {name}",
    )


def _ge(src: str, tgt: str, typ: str = "CALLS") -> GraphEdge:
    return GraphEdge(slug=SLUG, source=src, target=tgt, type=typ)


# Tiny in-memory store (just needs query_graph + list_edges).
class _FakeStore:
    def __init__(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        self._nodes = nodes
        self._edges = edges

    def query_graph(
        self,
        slug: str,
        *,
        node_type=None,
        name_match=None,
        neighbors_of=None,
    ) -> list[GraphNode]:
        return list(self._nodes)

    def list_edges(self, slug: str) -> list[GraphEdge]:
        return list(self._edges)


# ── GraphIndex.__init__() ImportError path ────────────────────────────────────


def test_graph_index_init_raises_import_error_when_extras_absent() -> None:
    """GraphIndex.__init__ raises ImportError with a diagnostic message when
    tree_sitter is not importable (the wiki extras guard)."""
    import builtins

    real_import = builtins.__import__

    def _block_tree_sitter(name, *args, **kwargs):
        if "tree_sitter" in name:
            raise ImportError("Fake: tree_sitter not installed")
        return real_import(name, *args, **kwargs)

    # Reload graph module with the import blocked.
    with patch("builtins.__import__", side_effect=_block_tree_sitter):
        # Unload cached module so __init__ runs again.
        import sys

        mod_name = "mewbo_graph.wiki.graph"
        old_mod = sys.modules.pop(mod_name, None)
        try:
            from mewbo_graph.wiki import graph as g_mod

            with pytest.raises(ImportError, match="wiki.*extras"):
                g_mod.GraphIndex()
        finally:
            # Restore the original module.
            if old_mod is not None:
                sys.modules[mod_name] = old_mod


# ── GraphParseResult.__add__ ──────────────────────────────────────────────────


def test_graph_parse_result_add_concatenates() -> None:
    """GraphParseResult + GraphParseResult merges all three fields."""
    r1 = GraphParseResult(
        nodes=[_gn("n1", "foo")],
        edges=[_ge("n1", "n2")],
        skipped=["skip1.txt"],
    )
    r2 = GraphParseResult(
        nodes=[_gn("n2", "bar")],
        edges=[_ge("n2", "n3")],
        skipped=["skip2.txt"],
    )
    merged = r1 + r2
    assert len(merged.nodes) == 2
    assert len(merged.edges) == 2
    assert merged.skipped == ["skip1.txt", "skip2.txt"]


def test_graph_parse_result_add_empty_right() -> None:
    r1 = GraphParseResult(nodes=[_gn("n1", "foo")], edges=[], skipped=[])
    empty = GraphParseResult(nodes=[], edges=[], skipped=[])
    merged = r1 + empty
    assert len(merged.nodes) == 1
    assert merged.edges == []


def test_graph_parse_result_add_identity_left() -> None:
    empty = GraphParseResult(nodes=[], edges=[], skipped=[])
    r2 = GraphParseResult(nodes=[_gn("n1", "foo")], edges=[], skipped=["s.txt"])
    merged = empty + r2
    assert len(merged.nodes) == 1
    assert merged.skipped == ["s.txt"]


# ── KnowledgeGraphView.for_slug: basic load ──────────────────────────────────


def test_for_slug_loads_all_nodes_and_edges() -> None:
    """for_slug without a node_limit returns every node and edge from the store."""
    nodes = [_gn("n1", "foo"), _gn("n2", "bar"), _gn("n3", "baz")]
    edges = [_ge("n1", "n2"), _ge("n2", "n3")]
    store = _FakeStore(nodes, edges)

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.slug == SLUG
    assert view.node_count == 3
    assert view.edge_count == 2
    assert view.total_nodes == 3
    assert view.total_edges == 2


def test_for_slug_empty_graph() -> None:
    """for_slug on an empty graph returns a valid zero-count view."""
    store = _FakeStore([], [])
    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.node_count == 0
    assert view.edge_count == 0
    assert view.total_nodes == 0
    assert view.total_edges == 0


# ── KnowledgeGraphView.for_slug: node_limit capping ─────────────────────────


def test_for_slug_node_limit_above_total_is_noop() -> None:
    """node_limit >= total_nodes keeps all nodes unchanged."""
    nodes = [_gn("n1", "foo"), _gn("n2", "bar")]
    store = _FakeStore(nodes, [])
    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=10)
    assert view.node_count == 2
    assert view.total_nodes == 2


def test_for_slug_node_limit_caps_by_degree() -> None:
    """node_limit < total_nodes keeps the highest-degree nodes."""
    # n1 connects to n2 and n3 (degree 2); n4 has degree 1; n2, n3 degree 1 each.
    nodes = [_gn("n1", "hub"), _gn("n2", "a"), _gn("n3", "b"), _gn("n4", "c")]
    edges = [_ge("n1", "n2"), _ge("n1", "n3"), _ge("n4", "n2")]
    store = _FakeStore(nodes, edges)

    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=2)

    # Highest-degree nodes are n1 (2) and n2 (2) or n3 (1) — n1 must be present.
    assert view.node_count == 2
    assert view.total_nodes == 4
    node_names = {n.name for n in view.nodes}
    assert "hub" in node_names


def test_for_slug_node_limit_drops_orphan_edges() -> None:
    """Edges whose endpoints are both NOT in the surviving node set are dropped."""
    nodes = [_gn("n1", "a"), _gn("n2", "b"), _gn("n3", "c")]
    # n3→n2 edge will be dropped if n3 is capped out.
    edges = [_ge("n1", "n2"), _ge("n3", "n2")]
    store = _FakeStore(nodes, edges)

    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=2)
    assert view.node_count == 2
    # Only the edge between surviving nodes should appear.
    for edge in view.edges:
        node_ids = {n.node_id for n in view.nodes}
        assert edge.source in node_ids
        assert edge.target in node_ids


# ── KnowledgeGraphView: derived properties ───────────────────────────────────


def test_kinds_histogram() -> None:
    """kinds property returns a per-type count dict."""
    nodes = [
        _gn("n1", "foo", typ="Function"),
        _gn("n2", "Bar", typ="Class"),
        _gn("n3", "baz", typ="Function"),
    ]
    store = _FakeStore(nodes, [])
    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.kinds == {"Function": 2, "Class": 1}


def test_node_count_and_edge_count_match_sequence_lengths() -> None:
    nodes = [_gn("n1", "a"), _gn("n2", "b")]
    edges = [_ge("n1", "n2")]
    store = _FakeStore(nodes, edges)
    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.node_count == len(view.nodes)
    assert view.edge_count == len(view.edges)


# ── KnowledgeGraphView.to_wire ────────────────────────────────────────────────


def test_to_wire_shape() -> None:
    """to_wire() emits the expected top-level keys with correct types."""
    nodes = [_gn("n1", "foo"), _gn("n2", "bar")]
    edges = [_ge("n1", "n2", "CONTAINS")]
    store = _FakeStore(nodes, edges)
    view = KnowledgeGraphView.for_slug(store, SLUG)

    wire = view.to_wire()

    assert wire["slug"] == SLUG
    assert isinstance(wire["nodes"], list)
    assert isinstance(wire["edges"], list)
    assert isinstance(wire["stats"], dict)
    assert len(wire["nodes"]) == 2
    assert len(wire["edges"]) == 1


def test_to_wire_stats_fields() -> None:
    """to_wire stats carry nodeCount, edgeCount, kinds, totalNodes, totalEdges, truncated."""
    nodes = [_gn("n1", "foo", typ="Function"), _gn("n2", "bar", typ="Class")]
    edges = [_ge("n1", "n2")]
    store = _FakeStore(nodes, edges)
    view = KnowledgeGraphView.for_slug(store, SLUG)

    stats = view.to_wire()["stats"]

    assert stats["nodeCount"] == 2
    assert stats["edgeCount"] == 1
    assert stats["kinds"] == {"Function": 1, "Class": 1}
    assert stats["totalNodes"] == 2
    assert stats["totalEdges"] == 1
    assert stats["truncated"] is False


def test_to_wire_truncated_true_when_capped() -> None:
    """truncated is True when node_limit removes at least one node."""
    nodes = [_gn("n1", "a"), _gn("n2", "b"), _gn("n3", "c")]
    edges = [_ge("n1", "n2"), _ge("n1", "n3")]
    store = _FakeStore(nodes, edges)
    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=2)

    stats = view.to_wire()["stats"]
    assert stats["truncated"] is True
    assert stats["totalNodes"] == 3
    assert stats["nodeCount"] == 2


def test_to_wire_node_shape() -> None:
    """Each node in the wire output has the expected Cytoscape data keys."""
    nodes = [_gn("n1", "MyFunc", typ="Function", f="src/main.py")]
    store = _FakeStore(nodes, [])
    view = KnowledgeGraphView.for_slug(store, SLUG)

    wire_node = view.to_wire()["nodes"][0]
    data = wire_node["data"]

    assert data["id"] == "n1"
    assert data["label"] == "MyFunc"
    assert data["kind"] == "Function"
    assert data["file"] == "src/main.py"
    assert isinstance(data["range"], list)
    assert isinstance(data["docstring"], str)


def test_to_wire_edge_shape() -> None:
    """Each edge in the wire output has the expected Cytoscape data keys."""
    nodes = [_gn("n1", "a"), _gn("n2", "b")]
    edges = [_ge("n1", "n2", "CALLS")]
    store = _FakeStore(nodes, edges)
    view = KnowledgeGraphView.for_slug(store, SLUG)

    wire_edge = view.to_wire()["edges"][0]
    data = wire_edge["data"]

    assert data["source"] == "n1"
    assert data["target"] == "n2"
    assert data["kind"] == "CALLS"
    assert data["id"] == "n1__CALLS__n2"


def test_to_wire_node_docstring_none_becomes_empty_string() -> None:
    """A node with docstring=None serialises as an empty string in the wire shape."""
    node = GraphNode(
        slug=SLUG,
        node_id="nx",
        type="File",
        name="a.py",
        file="a.py",
        range=(0, 1),
        docstring=None,
    )
    store = _FakeStore([node], [])
    view = KnowledgeGraphView.for_slug(store, SLUG)

    wire_node = view.to_wire()["nodes"][0]
    assert wire_node["data"]["docstring"] == ""


# ── Degree-tie breaking is stable ─────────────────────────────────────────────


def test_for_slug_degree_tie_breaks_on_node_id() -> None:
    """When two nodes share the same degree, the one with the lexicographically
    lower node_id survives (stable, deterministic for caching)."""
    # Both n1 and n2 have degree 1 (one edge each). With node_limit=1, whichever
    # has the lower node_id alphabetically wins.
    nodes = [_gn("aaa", "first"), _gn("zzz", "last")]
    edges = [_ge("aaa", "zzz")]
    store = _FakeStore(nodes, edges)

    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=1)

    assert view.node_count == 1
    # Both have degree 1 (one endpoint each). Lower node_id = "aaa".
    # (sorted by (-degree, node_id) → "aaa" sorts before "zzz" by node_id.)
    assert view.nodes[0].node_id == "aaa"
