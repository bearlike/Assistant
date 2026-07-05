"""Schema v2 ``CodeGraph`` validation tests (Gitea #188).

Drives the discriminated-union node models + whole-graph ``CodeGraph`` validator
from the CALLER site: legacy-shape rehydration (no ``subkind``/``attributes``),
the four rejection paths (duplicate id, dangling non-synthetic endpoint,
non-namespaced attribute key, endpoint-rule violation), and a real fixture-repo
``build_graph_core`` round-trip that assembles + validates before persisting.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    ClassNode,
    CodeGraph,
    FileNode,
    FunctionNode,
    GraphEdge,
    GraphNodeAdapter,
    IndexingJob,
    make_graph_node,
)
from pydantic import ValidationError

SLUG = "example.com/o/r"
FIXTURE = Path(__file__).parent / "fixtures" / "tiny_python_repo"

# A node dict in the exact shape older graphs persisted — no ``subkind`` /
# ``attributes`` keys at all.
_LEGACY_NODE = {
    "slug": SLUG,
    "node_id": "n1",
    "type": "File",
    "name": "a.py",
    "file": "a.py",
    "range": [0, 50],
}


# ── (a) legacy compatibility ────────────────────────────────────────────────


def test_legacy_node_dict_validates_through_union():
    """A persisted node lacking subkind/attributes rehydrates to the defaults."""
    node = GraphNodeAdapter.validate_python(_LEGACY_NODE)
    assert isinstance(node, FileNode)
    assert node.subkind is None
    assert node.attributes == {}
    # JSON path (the store persists JSONL lines) round-trips identically.
    assert GraphNodeAdapter.validate_json(json.dumps(_LEGACY_NODE)) == node


def test_legacy_graph_validates_through_codegraph():
    """A whole graph of legacy-shape nodes + edges validates unchanged."""
    f = GraphNodeAdapter.validate_python(_LEGACY_NODE)
    fn = make_graph_node(
        slug=SLUG, node_id="fn", type="Function", name="foo", file="a.py", range=(1, 9)
    )
    cg = CodeGraph(
        nodes=[f, fn],
        edges=[GraphEdge(slug=SLUG, source="n1", target="fn", type="CONTAINS")],
    )
    assert cg.schema_version == "1"
    assert {type(n).__name__ for n in cg.nodes} == {"FileNode", "FunctionNode"}


# ── (b) duplicate node_id ───────────────────────────────────────────────────


def test_codegraph_rejects_duplicate_node_id():
    f1 = FileNode(slug=SLUG, node_id="dup", name="a.py", file="a.py", range=(0, 1))
    f2 = FileNode(slug=SLUG, node_id="dup", name="b.py", file="b.py", range=(0, 1))
    with pytest.raises(ValidationError, match="duplicate node_id"):
        CodeGraph(nodes=[f1, f2], edges=[])


# ── (c) dangling non-synthetic edge endpoint ────────────────────────────────


def test_codegraph_rejects_dangling_nonsynthetic_endpoint():
    """A CONTAINS edge carries no target_name → its target MUST resolve."""
    f = FileNode(slug=SLUG, node_id="F", name="a.py", file="a.py", range=(0, 1))
    with pytest.raises(ValidationError, match="does not resolve"):
        CodeGraph(
            nodes=[f],
            edges=[GraphEdge(slug=SLUG, source="F", target="ghost", type="CONTAINS")],
        )


def test_codegraph_allows_synthetic_external_target():
    """A cross-file edge carrying target_name may point out-of-repo (legal)."""
    f = FileNode(slug=SLUG, node_id="F", name="a.py", file="a.py", range=(0, 1))
    cg = CodeGraph(
        nodes=[f],
        edges=[
            GraphEdge(
                slug=SLUG, source="F", target="ext", type="IMPORTS", target_name="os"
            )
        ],
    )
    assert len(cg.edges) == 1


def test_codegraph_allows_dangling_source_when_target_name_set():
    """The ``target_name`` exemption is BOTH-ENDS, not just the target.

    This is the reality behind the documented, deliberately-unfixed EXTENDS
    dangling-source bug (packages/mewbo_graph/CLAUDE.md → "Known trap,
    deliberately NOT fixed"): the tree-sitter EXTENDS block computes its edge
    SOURCE id from the subclass NAME token's ``start_byte``, not the class
    DEF node's, so it matches no persisted node. If ``CodeGraph`` required
    the source to resolve whenever the target didn't, every such edge would
    fail validation for every language that hasn't been superseded by the
    scip resolver — so the exemption has to cover the source too.
    """
    f = FileNode(slug=SLUG, node_id="F", name="a.py", file="a.py", range=(0, 1))
    cg = CodeGraph(
        nodes=[f],
        edges=[
            GraphEdge(
                slug=SLUG,
                source="ghost-source",
                target="F",
                type="EXTENDS",
                target_name="Base",
            )
        ],
    )
    assert len(cg.edges) == 1


# ── (d) non-namespaced attribute keys ───────────────────────────────────────


def test_node_rejects_non_namespaced_attribute_key():
    with pytest.raises(ValidationError, match="namespaced"):
        FileNode(
            slug=SLUG,
            node_id="n",
            name="a",
            file="a",
            range=(0, 1),
            attributes={"visibility": "public"},
        )


def test_edge_rejects_non_namespaced_attribute_key():
    with pytest.raises(ValidationError, match="namespaced"):
        GraphEdge(
            slug=SLUG, source="a", target="b", type="CALLS", attributes={"count": 3}
        )


def test_namespaced_attributes_and_subkind_accepted():
    n = FileNode(
        slug=SLUG,
        node_id="n",
        name="a",
        file="a",
        range=(0, 1),
        subkind="companion",
        attributes={"kotlin.visibility": "public", "scip.symbol": "x"},
    )
    assert n.subkind == "companion"
    assert n.attributes["kotlin.visibility"] == "public"


# ── (e) endpoint-rule violation ─────────────────────────────────────────────


def test_codegraph_rejects_endpoint_rule_violation():
    """CONTAINS rooted at a Function source is illegal (CPG endpoint rule)."""
    fn = FunctionNode(slug=SLUG, node_id="fn", name="foo", file="a.py", range=(0, 1))
    cls = ClassNode(slug=SLUG, node_id="c", name="Bar", file="a.py", range=(1, 9))
    with pytest.raises(ValidationError, match="illegal source kind"):
        CodeGraph(
            nodes=[fn, cls],
            edges=[GraphEdge(slug=SLUG, source="fn", target="c", type="CONTAINS")],
        )


# ── (f) build_graph_core validates + persists a real fixture graph ──────────


def test_build_graph_core_validates_and_persists_fixture(tmp_path, monkeypatch):
    """The ingest path assembles + validates a CodeGraph, then persists it."""
    from mewbo_graph.plugins.wiki import build_graph as bg

    # Skip embeddings (no litellm / network in the unit boundary).
    monkeypatch.setattr(bg, "_embeddings_enabled", lambda: False)

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(
        IndexingJob(
            jobId="j1",
            slug=SLUG,
            status="queued",
            scannedCount=0,
            totalCount=0,
            currentFile=None,
        )
    )
    ctx = SimpleNamespace(slug=SLUG, clone_dir=FIXTURE, store=store, job_id="j1")

    result = bg.build_graph_core(ctx)

    assert result["nodeCount"] > 0
    nodes = store.query_graph(SLUG)
    # Persisted nodes rehydrate through the union (per-kind subclasses) and carry
    # the new schema-v2 fields at their defaults.
    assert nodes
    assert any(n.type == "File" for n in nodes)
    assert all(hasattr(n, "attributes") and hasattr(n, "subkind") for n in nodes)
