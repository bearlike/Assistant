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
from mewbo_graph.entities.types import Entity, EntityRelation
from mewbo_graph.wiki.graph import GraphParseResult, KnowledgeGraphView
from mewbo_graph.wiki.memory_types import MemoryEdge, MemoryNode, MemoryProvenance
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


# Tiny in-memory multiplex store. Defaults to empty entity + memory layers so
# the legacy AST-only tests are unaffected; the multiplex tests below pass the
# extra families explicitly.
class _FakeStore:
    def __init__(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        *,
        entities=None,
        entity_edges=None,
        memory=None,
        memory_edges=None,
    ) -> None:
        self._nodes = nodes
        self._edges = edges
        self._entities = entities or []
        self._entity_edges = entity_edges or []
        self._memory = memory or []
        self._memory_edges = memory_edges or []

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

    def query_entities(self, slug: str, *, filt=None):
        return list(self._entities)

    def list_entity_edges(self, slug: str, *, source_id=None):
        return list(self._entity_edges)

    def get_entity(self, slug: str, entity_id: str):
        return next((e for e in self._entities if e.id == entity_id), None)

    def query_memory(self, slug: str, *, filt=None):
        return list(self._memory)

    def list_memory_edges(
        self, slug: str, *, node_id=None, include_invalidated=False
    ):
        return [
            e
            for e in self._memory_edges
            if include_invalidated or e.invalid_at is None
        ]


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


# ── multiplex helpers ─────────────────────────────────────────────────────────


def _entity(name: str, typ: str = "concept", labels=None) -> Entity:
    return Entity(name=name, type=typ, labels=labels or [])


def _mem(content: str) -> MemoryNode:
    return MemoryNode(
        slug=SLUG,
        content=content,
        provenance=MemoryProvenance(
            author_agent="t", source="indexer", created_at="2026-01-01T00:00:00Z"
        ),
    )


def _mem_edge(src: str, tgt: str, typ: str) -> MemoryEdge:
    return MemoryEdge(
        slug=SLUG, source=src, target=tgt, type=typ, valid_at="2026-01-01T00:00:00Z"
    )


def _ce(src: str, tgt: str, typ: str = "CALLS", target_name: str | None = None):
    return GraphEdge(
        slug=SLUG, source=src, target=tgt, type=typ, target_name=target_name
    )


def _layers(wire) -> dict[str, set]:
    """Group the distinct ``layer`` tags across wire nodes and edges."""
    return {
        "node": {n["data"]["layer"] for n in wire["nodes"]},
        "edge": {e["data"]["layer"] for e in wire["edges"]},
    }


# ── CHANGE 1: multiplex exposure ──────────────────────────────────────────────


def test_multiplex_entity_and_memory_nodes_appear_with_layers() -> None:
    """Entity + memory nodes surface in to_wire with the correct ``layer`` tag."""
    ast = [_gn("ast1", "run", typ="Function", f="a.py")]
    ents = [_entity("Billing", labels=["domain"])]
    mems = [_mem("Billing batches invoices nightly.")]
    store = _FakeStore(ast, [], entities=ents, memory=mems)

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    by_id = {n["data"]["id"]: n["data"] for n in wire["nodes"]}

    assert by_id["ast1"]["layer"] == "ast"
    ent = by_id[ents[0].id]
    assert ent["layer"] == "entity"
    assert ent["kind"] == "Entity"
    assert ent["entityType"] == "concept"
    assert ent["labels"] == ["domain"]
    assert ent["label"] == "Billing"
    mem = by_id[mems[0].node_id]
    assert mem["layer"] == "memory"
    assert mem["kind"] == "Memory"
    assert mem["snippet"].startswith("Billing batches")
    assert wire["stats"]["perLayer"] == {"ast": 1, "entity": 1, "memory": 1}


def test_entity_relates_edge_carries_verb_in_label_not_kind() -> None:
    """entity↔entity edge → layer=entity, kind=RELATES, verb in label."""
    a, b = _entity("Alice", "person"), _entity("Acme", "organization")
    rel = EntityRelation(source_id=a.id, target_id=b.id, type="works_at")
    store = _FakeStore([], [], entities=[a, b], entity_edges=[rel])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    edges = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "entity"]
    assert len(edges) == 1
    assert edges[0]["kind"] == "RELATES"
    assert edges[0]["label"] == "works_at"
    assert edges[0]["source"] == a.id
    assert edges[0]["target"] == b.id


def test_entity_to_ast_anchor_is_cross_layer() -> None:
    """entity→AST EntityRelation (type=ANCHORS) → layer=cross kind=ANCHORS."""
    ast = [_gn("astN", "Service", typ="Class", f="svc.py")]
    ent = _entity("ServiceConcept")
    anchor = EntityRelation(source_id=ent.id, target_id="astN", type="ANCHORS")
    store = _FakeStore(ast, [], entities=[ent], entity_edges=[anchor])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    cross = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "cross"]
    assert len(cross) == 1
    assert cross[0]["kind"] == "ANCHORS"
    assert cross[0]["source"] == ent.id
    assert cross[0]["target"] == "astN"


def test_memory_anchor_by_name_reconciles_to_ast_node_id() -> None:
    """A memory ANCHORS edge keyed by ``file#Name`` reconciles to the node_id."""
    # The AST node's entity_key is ``svc.py#Service`` (file#name).
    ast = [_gn("astZ", "Service", typ="Class", f="svc.py")]
    mem = _mem("Service owns the retry policy.")
    # ANCHORS target is the EntityKey, NOT the node_id — the view must resolve it.
    anchor = _mem_edge(mem.node_id, "svc.py#Service", "ANCHORS")
    store = _FakeStore(ast, [], memory=[mem], memory_edges=[anchor])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    cross = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "cross"]
    assert len(cross) == 1
    assert cross[0]["source"] == mem.node_id
    assert cross[0]["target"] == "astZ"  # reconciled from "svc.py#Service"


def test_memory_relates_edge_is_memory_layer() -> None:
    """note↔note RELATES → layer=memory kind=RELATES."""
    m1, m2 = _mem("Claim one."), _mem("Claim two.")
    rel = _mem_edge(m1.node_id, m2.node_id, "RELATES")
    store = _FakeStore([], [], memory=[m1, m2], memory_edges=[rel])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    edges = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "memory"]
    assert len(edges) == 1
    assert edges[0]["kind"] == "RELATES"


def test_unresolvable_memory_anchor_is_dropped_no_dangling_edge() -> None:
    """An anchor whose target resolves to nothing produces NO cross edge."""
    mem = _mem("Dangling note.")
    anchor = _mem_edge(mem.node_id, "ghost.py#Nope", "ANCHORS")
    store = _FakeStore([], [], memory=[mem], memory_edges=[anchor])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    assert [e for e in wire["edges"] if e["data"]["layer"] == "cross"] == []


def test_entity_relates_to_missing_entity_is_dropped() -> None:
    """An entity edge whose target entity is absent from the payload is dropped."""
    a = _entity("Alice", "person")
    rel = EntityRelation(source_id=a.id, target_id="deadbeef" * 5, type="knows")
    store = _FakeStore([], [], entities=[a], entity_edges=[rel])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    assert [e for e in wire["edges"] if e["data"]["layer"] == "entity"] == []


def test_invalidated_memory_anchor_excluded() -> None:
    """An invalidated ANCHORS edge (invalid_at set) is not emitted."""
    ast = [_gn("astQ", "Service", typ="Class", f="svc.py")]
    mem = _mem("Stale claim.")
    edge = MemoryEdge(
        slug=SLUG,
        source=mem.node_id,
        target="svc.py#Service",
        type="ANCHORS",
        valid_at="2026-01-01T00:00:00Z",
        invalid_at="2026-02-01T00:00:00Z",
    )
    store = _FakeStore(ast, [], memory=[mem], memory_edges=[edge])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    assert [e for e in wire["edges"] if e["data"]["layer"] == "cross"] == []


# ── CHANGE 2: AST connectivity ────────────────────────────────────────────────


def test_cross_file_call_connects_two_in_repo_nodes() -> None:
    """A CALLS edge whose target_name resolves to a real node connects them."""
    caller = _gn("file_a", "a.py", typ="File", f="a.py")
    callee = _gn("fn_run", "run_engine", typ="Function", f="b.py")
    # File a.py CALLS ``run_engine`` (defined in b.py) — synthetic target id.
    edge = _ce("file_a", "synthetic_ext", typ="CALLS", target_name="run_engine")
    store = _FakeStore([caller, callee], [edge])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    ast_edges = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "ast"]
    assert len(ast_edges) == 1
    # Re-pointed at the REAL in-repo node, not the synthetic external id.
    assert ast_edges[0]["source"] == "file_a"
    assert ast_edges[0]["target"] == "fn_run"
    assert ast_edges[0]["kind"] == "CALLS"
    # No External node was synthesized — the symbol was in-repo.
    assert all(n["data"]["kind"] != "External" for n in wire["nodes"])


def test_cross_file_import_connects_in_repo_module() -> None:
    """An IMPORTS edge resolving to an in-repo node connects, no External node."""
    f = _gn("file_main", "main.py", typ="File", f="main.py")
    mod = _gn("mod_core", "core", typ="File", f="core.py")
    edge = _ce("file_main", "synthetic", typ="IMPORTS", target_name="core")
    store = _FakeStore([f, mod], [edge])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    ast_edges = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "ast"]
    assert ast_edges[0]["target"] == "mod_core"


def test_shared_external_converges_on_one_named_node() -> None:
    """Multiple references to the same out-of-repo symbol share ONE External node."""
    fa = _gn("file_a", "a.py", typ="File", f="a.py")
    fb = _gn("file_b", "b.py", typ="File", f="b.py")
    # Both files import ``os`` (not an in-repo node) → one External node.
    e1 = _ce("file_a", "syn1", typ="IMPORTS", target_name="os")
    e2 = _ce("file_b", "syn2", typ="IMPORTS", target_name="os")
    store = _FakeStore([fa, fb], [e1, e2])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    externals = [n["data"] for n in wire["nodes"] if n["data"]["kind"] == "External"]
    assert len(externals) == 1
    assert externals[0]["label"] == "os"
    assert externals[0]["layer"] == "ast"
    # Both edges survive and point at the single External node.
    ext_id = externals[0]["id"]
    ast_edges = [e["data"] for e in wire["edges"] if e["data"]["layer"] == "ast"]
    assert {e["source"] for e in ast_edges} == {"file_a", "file_b"}
    assert all(e["target"] == ext_id for e in ast_edges)


def test_node_limit_prunes_only_ast_layer() -> None:
    """node_limit degree-prunes AST nodes; entity + memory stay fully included."""
    # 3 AST nodes (hub has degree 2), 2 entities, 2 memory notes.
    ast = [_gn("hub", "h"), _gn("a", "a"), _gn("b", "c")]
    ast_edges = [_ge("hub", "a"), _ge("hub", "b")]
    ents = [_entity("E1"), _entity("E2")]
    mems = [_mem("note one"), _mem("note two")]
    store = _FakeStore(
        ast, ast_edges, entities=ents, memory=mems
    )

    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=2)
    wire = view.to_wire()

    ast_nodes = [n for n in wire["nodes"] if n["data"]["layer"] == "ast"]
    ent_nodes = [n for n in wire["nodes"] if n["data"]["layer"] == "entity"]
    mem_nodes = [n for n in wire["nodes"] if n["data"]["layer"] == "memory"]
    assert len(ast_nodes) == 2  # capped
    assert len(ent_nodes) == 2  # untouched
    assert len(mem_nodes) == 2  # untouched
    assert wire["stats"]["truncated"] is True
    assert wire["stats"]["perLayer"] == {"ast": 2, "entity": 2, "memory": 2}
    # The hub (highest degree) survived the cut.
    assert "hub" in {n["data"]["id"] for n in ast_nodes}


def test_truncated_stays_true_when_cap_and_externals_coexist() -> None:
    """A node cap + synthesized External nodes must NOT mask truncation.

    Regression: ``truncated`` once compared ``node_count`` (real AST + External)
    against ``total_nodes``; with enough externals the sum exceeded the total and
    falsely read un-truncated. It must compare REAL kept AST nodes only.
    """
    # 5 real AST nodes; the hub imports 3 distinct out-of-repo modules.
    hub = _gn("hub", "main.py", typ="File", f="main.py")
    others = [_gn(f"n{i}", f"sym{i}", typ="Function", f="main.py") for i in range(4)]
    # hub CONTAINS the 4 others (gives hub degree 4 so it survives the cap).
    contains = [_ge("hub", o.node_id, "CONTAINS") for o in others]
    # hub imports 3 externals → 3 External nodes synthesized in-view.
    ext_edges = [
        _ce("hub", f"syn{m}", typ="IMPORTS", target_name=m)
        for m in ("os", "sys", "json")
    ]
    store = _FakeStore([hub, *others], contains + ext_edges)

    view = KnowledgeGraphView.for_slug(store, SLUG, node_limit=3)
    wire = view.to_wire()

    externals = [n for n in wire["nodes"] if n["data"]["kind"] == "External"]
    real_ast = [
        n
        for n in wire["nodes"]
        if n["data"]["layer"] == "ast" and n["data"]["kind"] != "External"
    ]
    assert len(real_ast) == 3  # genuinely capped (5 → 3)
    assert len(externals) >= 1  # at least one external survived alongside the hub
    # node_count (real + external) exceeds total — yet truncated must stay True.
    assert wire["stats"]["nodeCount"] > wire["stats"]["totalNodes"] - len(externals)
    assert wire["stats"]["truncated"] is True


def test_total_edges_excludes_dropped_orphan_edges() -> None:
    """``totalEdges`` ("M") must reflect emitted edges, not raw stored edges.

    An orphan structural edge (``target_name=None`` with a missing endpoint) is
    dropped by resolution and must not inflate the honest edge total.
    """
    a = _gn("a", "a.py", typ="File", f="a.py")
    b = _gn("b", "b.py", typ="File", f="b.py")
    real = _ge("a", "b", "CONTAINS")  # both endpoints present → kept
    orphan = _ge("a", "ghost", "CONTAINS")  # target missing → dropped
    store = _FakeStore([a, b], [real, orphan])

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    ast_edges = [e for e in wire["edges"] if e["data"]["layer"] == "ast"]
    assert len(ast_edges) == 1  # orphan gone from the payload
    # totalEdges counts only the emitted edge, NOT the dropped orphan.
    assert wire["stats"]["totalEdges"] == 1


def test_all_three_layers_in_one_payload() -> None:
    """End-to-end: ast + entity + memory nodes AND a cross edge coexist."""
    ast = [_gn("astC", "Service", typ="Class", f="svc.py")]
    ent = _entity("ServiceDomain")
    anchor = EntityRelation(source_id=ent.id, target_id="astC", type="ANCHORS")
    mem = _mem("Service retries thrice.")
    mem_anchor = _mem_edge(mem.node_id, "svc.py#Service", "ANCHORS")
    store = _FakeStore(
        ast,
        [],
        entities=[ent],
        entity_edges=[anchor],
        memory=[mem],
        memory_edges=[mem_anchor],
    )

    wire = KnowledgeGraphView.for_slug(store, SLUG).to_wire()
    layers = _layers(wire)
    assert layers["node"] == {"ast", "entity", "memory"}
    assert "cross" in layers["edge"]
    # Two cross ANCHORS edges: entity→ast and memory→ast.
    cross = [e for e in wire["edges"] if e["data"]["layer"] == "cross"]
    assert len(cross) == 2
