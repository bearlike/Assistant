"""``_apply_resolver`` — merge of tree-sitter edges with faithful resolver edges.

Drives the merge helper directly with an INJECTED fake resolver, so no scip
binaries are required: the unit asserts the exact edge/node bookkeeping the
graph-build seam performs when the Python resolver supersedes tree-sitter's
name-matched edges, and that an unavailable resolver leaves the parse untouched.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mewbo_graph.plugins.wiki import build_graph as build_graph_mod
from mewbo_graph.plugins.wiki.build_graph import _apply_resolver
from mewbo_graph.wiki.graph import GraphParseResult
from mewbo_graph.wiki.resolve import ResolutionResult, ResolutionStats
from mewbo_graph.wiki.types import GraphEdge, GraphNode, make_graph_node

SLUG = "host/org/repo"


def _node(node_id: str, type_: str, name: str, file: str) -> GraphNode:
    return make_graph_node(
        slug=SLUG, node_id=node_id, type=type_, name=name, file=file, range=(0, 1)
    )


def _edge(source: str, target: str, type_: str, target_name: str | None = None) -> GraphEdge:
    return GraphEdge(
        slug=SLUG, source=source, target=target, type=type_, target_name=target_name
    )


def _fixture() -> GraphParseResult:
    """A two-file (one Python, one Go) parse with CONTAINS + synthetic edges."""
    nodes = [
        _node("pyfile", "File", "pkg/a.py", "pkg/a.py"),
        _node("pyfunc", "Function", "helper", "pkg/a.py"),
        _node("pycls", "Class", "Widget", "pkg/a.py"),
        _node("gofile", "File", "pkg/b.go", "pkg/b.go"),
        _node("gofunc", "Function", "GoHelper", "pkg/b.go"),
    ]
    edges = [
        # Structural — all languages — must always survive.
        _edge("pyfile", "pyfunc", "CONTAINS"),
        _edge("pyfile", "pycls", "CONTAINS"),
        _edge("gofile", "gofunc", "CONTAINS"),
        # Python synthetic name-matched edges — the resolver supersedes these.
        _edge("pyfile", "ext-os", "IMPORTS", target_name="os"),
        _edge("pyfile", "ext-h", "CALLS", target_name="other"),
        _edge("pycls", "ext-base", "EXTENDS", target_name="Base"),
        # Go synthetic CALLS — kept (no Go backend yet).
        _edge("gofile", "ext-go", "CALLS", target_name="GoHelper"),
    ]
    return GraphParseResult(nodes=nodes, edges=edges, skipped=["README.md"])


class _FakeResolver:
    """Returns a canned ``ResolutionResult`` regardless of inputs."""

    def __init__(self, result: ResolutionResult) -> None:
        self._result = result

    def resolve(self, repo_root: Path, nodes: object) -> ResolutionResult:
        return self._result


def _resolver_result() -> ResolutionResult:
    """One precise in-repo edge + one synthesized External node (available=True)."""
    precise = _edge("pyfunc", "pycls", "REFERENCES")  # real ids, target_name=None
    external = _node("ext-desc", "External", "requests.get", "")
    return ResolutionResult(
        edges=[precise],
        externals=[external],
        stats=ResolutionStats(indexed_roots=1),
    )


def test_apply_resolver_supersedes_python_edges_keeps_rest() -> None:
    parsed = _fixture()
    fake = _FakeResolver(_resolver_result())
    with patch.object(build_graph_mod, "_resolver_available", return_value=True), \
         patch.object(build_graph_mod, "_make_resolver", return_value=fake):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)

    edge_keys = {(e.source, e.target, e.type) for e in out.edges}

    # Python synthetic IMPORTS/CALLS/EXTENDS dropped.
    assert ("pyfile", "ext-os", "IMPORTS") not in edge_keys
    assert ("pyfile", "ext-h", "CALLS") not in edge_keys
    assert ("pycls", "ext-base", "EXTENDS") not in edge_keys
    # Go synthetic CALLS kept (non-Python source).
    assert ("gofile", "ext-go", "CALLS") in edge_keys
    # CONTAINS kept for every language.
    assert ("pyfile", "pyfunc", "CONTAINS") in edge_keys
    assert ("pyfile", "pycls", "CONTAINS") in edge_keys
    assert ("gofile", "gofunc", "CONTAINS") in edge_keys
    # Resolver's precise edge appended.
    assert ("pyfunc", "pycls", "REFERENCES") in edge_keys

    # External node added to the persisted node set.
    node_ids = {n.node_id for n in out.nodes}
    assert "ext-desc" in node_ids
    assert node_ids.issuperset({"pyfile", "pyfunc", "pycls", "gofile", "gofunc"})
    # skipped passes through untouched.
    assert out.skipped == ["README.md"]


def test_apply_resolver_no_binaries_is_identity() -> None:
    parsed = _fixture()
    with patch.object(build_graph_mod, "_resolver_available", return_value=False):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)
    # Resolver unavailable → the parse is returned untouched (byte-identical).
    assert out is parsed


def test_apply_resolver_unavailable_index_is_identity() -> None:
    parsed = _fixture()
    # available is False whenever no root was indexed (default stats).
    empty = ResolutionResult(edges=[], externals=[], stats=ResolutionStats())
    assert empty.available is False
    fake = _FakeResolver(empty)
    with patch.object(build_graph_mod, "_resolver_available", return_value=True), \
         patch.object(build_graph_mod, "_make_resolver", return_value=fake):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)
    assert out is parsed
