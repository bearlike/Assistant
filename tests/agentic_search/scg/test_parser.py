"""Tests for ScgParser — the registry that maps sources into the persisted SCG.

Exercises the atomic class end-to-end against a seeded JSON store (no MongoDB)
with an injected FAKE embedder (never a real embedding API). The contract the
spec (Gitea #19) fixes:

* ``parse_source`` dispatches by ``source_type``, persists nodes/edges/recipes,
  and embeds every node into an ``ScgEmbedding`` keyed on ``node_id``;
* re-parsing the SAME source replaces its subgraph (no duplicates) — a clean
  re-map via ``store.delete_source`` first;
* ``link_sources`` runs the injected ``TypeAligner`` and persists RESOLVES_TO;
* ``compute_param_edges`` is the In-N-Out producer→consumer join: a capability
  that PRODUCES an output field gets a CONSUMES edge to another capability that
  binds an input field of the same name, carrying ``binds=(out_key, in_key)``.

No network, no real LLM, no real embedding backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.entity_resolution import TypeAligner
from mewbo_graph.scg.parser import ScgParser
from mewbo_graph.scg.providers import (
    McpToolListStructureProvider,
    OpenApiStructureProvider,
)
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import (
    CapabilityBinding,
    ScgNode,
    SourceDescriptor,
    StructureGraph,
)

# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A fresh JSON-backed SCG store under a throwaway temp dir."""
    return JsonScgStore(root_dir=tmp_path / "scg")


class _FakeEmbedder:
    """A deterministic fake embedder: a tiny vector derived from the text length.

    Mirrors the wiki ``Embedder`` surface the parser uses (``embed_nodes`` over
    ``(node_id, text)`` pairs) without any network. Records what it was asked to
    embed so a test can assert the parser embedded every node.
    """

    model = "fake-embed"

    def __init__(self) -> None:
        self.embedded: list[tuple[str, str]] = []

    def embed_nodes(
        self, items: list[tuple[str, str]], *, slug: str = ""
    ) -> list[object]:
        self.embedded.extend(items)
        # Return lightweight stand-ins carrying just what the parser maps.
        return [
            _FakeEmbeddingRow(node_id=nid, vector=[float(len(text)), 1.0])
            for nid, text in items
        ]


class _FakeEmbeddingRow:
    """A minimal stand-in for the wiki ``Embedding`` record."""

    def __init__(self, *, node_id: str, vector: list[float]) -> None:
        self.node_id = node_id
        self.vector = vector
        self.dim = len(vector)


def _openapi_descriptor() -> SourceDescriptor:
    """A small OpenAPI doc: one schema + one operation with mixed params."""
    return SourceDescriptor(
        source_id="github",
        source_type="openapi",
        raw={
            "openapi": "3.1.0",
            "info": {"title": "GitHub"},
            "components": {
                "schemas": {
                    "Issue": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "title": {"type": "string"},
                        },
                    }
                }
            },
            "paths": {
                "/repos/{owner}/{repo}/issues": {
                    "get": {
                        "operationId": "search_issues",
                        "summary": "List repository issues.",
                        "parameters": [
                            {
                                "name": "repo",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "state",
                                "in": "query",
                                "required": False,
                                "schema": {"type": "string"},
                            },
                        ],
                    }
                }
            },
        },
    )


def _producer_consumer_descriptor() -> SourceDescriptor:
    """An MCP server with a producer tool + a consumer tool that binds its output.

    ``find_user`` PRODUCES ``user_id``; ``get_orders`` binds an input ``user_id``
    — the In-N-Out join should wire a CONSUMES edge find_user → get_orders.
    """
    return SourceDescriptor(
        source_id="crm",
        source_type="mcp_tool_list",
        raw={
            "tools": [
                {
                    "name": "find_user",
                    "description": "Find a user by email.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"email": {"type": "string"}},
                        "required": ["email"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {"user_id": {"type": "string"}},
                    },
                },
                {
                    "name": "get_orders",
                    "description": "List a user's orders.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"user_id": {"type": "string"}},
                        "required": ["user_id"],
                    },
                },
            ]
        },
    )


def _parser(store: JsonScgStore, embedder: _FakeEmbedder) -> ScgParser:
    """A parser wired with the real default providers + the fake embedder."""
    return ScgParser(
        store=store,
        providers=[OpenApiStructureProvider(), McpToolListStructureProvider()],
        embedder=embedder,
    )


# ── parse_source: persists nodes / edges / embeddings ───────────────────────


def test_parse_source_persists_nodes_edges_and_embeddings(
    store: JsonScgStore,
) -> None:
    """parse_source dispatches, persists the subgraph, and embeds every node."""
    embedder = _FakeEmbedder()
    parser = _parser(store, embedder)

    graph = parser.parse_source(_openapi_descriptor())
    assert isinstance(graph, StructureGraph)

    # Nodes persisted (source + entity + fields + capability).
    nodes = store.query_nodes(source_id="github")
    keys = {n.source_key for n in nodes}
    assert "github" in keys
    assert "github#Issue" in keys
    assert "github#search_issues" in keys

    # Edges persisted.
    assert store.list_edges(source="github", kind="HAS_ENTITY")

    # The descriptor is persisted so a later re-map / link can find it.
    assert any(s.source_id == "github" for s in store.list_sources())

    # Every node got exactly one embedding keyed on node_id.
    embeddings = store.list_embeddings()
    emb_ids = {e.node_id for e in embeddings}
    assert emb_ids == {n.node_id for n in nodes}
    assert all(e.model == "fake-embed" for e in embeddings)
    # The fake embedder was actually invoked once per node.
    assert len(embedder.embedded) == len(nodes)


def test_parse_source_embed_text_includes_name_doc_and_examples(
    store: JsonScgStore,
) -> None:
    """The embedded text blends the node name + doc (+ example queries)."""
    embedder = _FakeEmbedder()
    _parser(store, embedder).parse_source(_openapi_descriptor())

    cap_node = next(
        n for n in store.query_nodes(source_id="github")
        if n.source_key == "github#search_issues"
    )
    text = dict(embedder.embedded)[cap_node.node_id]
    assert "search_issues" in text
    assert "List repository issues." in text  # the operation doc


# ── re-parse replaces (no dupes) ────────────────────────────────────────────


def test_reparse_same_source_replaces_no_duplicates(store: JsonScgStore) -> None:
    """Re-parsing the same source wipes its old subgraph first (clean re-map)."""
    embedder = _FakeEmbedder()
    parser = _parser(store, embedder)

    parser.parse_source(_openapi_descriptor())
    first_nodes = len(store.query_nodes(source_id="github"))
    first_edges = len(store.list_edges(source="github"))
    first_emb = len(store.list_embeddings())

    # Re-map the identical descriptor.
    parser.parse_source(_openapi_descriptor())

    assert len(store.query_nodes(source_id="github")) == first_nodes
    assert len(store.list_edges(source="github")) == first_edges
    assert len(store.list_embeddings()) == first_emb


def test_reparse_drops_removed_capability(store: JsonScgStore) -> None:
    """A capability removed from the descriptor is gone after a re-map."""
    embedder = _FakeEmbedder()
    parser = _parser(store, embedder)

    parser.parse_source(_producer_consumer_descriptor())
    assert store.query_nodes(source_id="crm", name_contains="get_orders")

    # Re-map with only one tool.
    shrunk = SourceDescriptor(
        source_id="crm",
        source_type="mcp_tool_list",
        raw={"tools": [{"name": "find_user", "inputSchema": {}}]},
    )
    parser.parse_source(shrunk)
    assert not store.query_nodes(source_id="crm", name_contains="get_orders")


# ── compute_param_edges: producer → consumer join ───────────────────────────


def test_compute_param_edges_links_producer_to_consumer(
    store: JsonScgStore,
) -> None:
    """PRODUCES output field matched to a consuming op's input => CONSUMES edge."""
    embedder = _FakeEmbedder()
    parser = _parser(store, embedder)
    parser.parse_source(_producer_consumer_descriptor())

    edges = parser.compute_param_edges()
    consumes = [e for e in store.list_edges(kind="CONSUMES")]

    assert any(
        e.source == "crm#find_user" and e.target == "crm#get_orders"
        for e in consumes
    )
    edge = next(
        e for e in consumes
        if e.source == "crm#find_user" and e.target == "crm#get_orders"
    )
    assert edge.binds == ("crm#find_user.user_id", "crm#get_orders.user_id")
    assert edge.method in {"type_align", "key", "embedding", "llm"}
    # The returned list is the persisted edge set.
    assert any(e.kind == "CONSUMES" for e in edges)


def test_compute_param_edges_no_match_emits_nothing(store: JsonScgStore) -> None:
    """No output field name matches any other op's input => no CONSUMES edges."""
    embedder = _FakeEmbedder()
    parser = _parser(store, embedder)
    parser.parse_source(_openapi_descriptor())  # single op, no shared producer

    edges = parser.compute_param_edges()
    assert edges == []
    assert store.list_edges(kind="CONSUMES") == []


# ── link_sources: RESOLVES_TO via TypeAligner ───────────────────────────────


def _issue_entity(source_id: str) -> ScgNode:
    """An alignable ``entity_type`` node whose field overlap drives the aligner."""
    fields = ["id", "title", "status", "assignee"]
    return ScgNode(
        source_key=f"{source_id}#Issue",
        kind="entity_type",
        source_id=source_id,
        name="Issue",
        bindings=[
            CapabilityBinding(field_key=f"{source_id}#Issue.{f}", mode="optional")
            for f in fields
        ],
    )


def test_link_sources_persists_resolves_to_edges(store: JsonScgStore) -> None:
    """link_sources runs the aligner and persists RESOLVES_TO hypothesis edges."""
    embedder = _FakeEmbedder()
    parser = ScgParser(
        store=store,
        providers=[OpenApiStructureProvider()],
        embedder=embedder,
        aligner=TypeAligner(store=store),
    )

    # Seed two cross-source, near-identical entity types (the aligner reads the
    # bindings' field names for its overlap heuristic).
    store.upsert_nodes([_issue_entity("jira"), _issue_entity("linear")])

    edges = parser.link_sources(["jira", "linear"])
    resolves = store.list_edges(kind="RESOLVES_TO")
    assert len(resolves) == 1
    assert resolves[0].method == "type_align"
    assert edges and edges[0].kind == "RESOLVES_TO"


def test_link_sources_without_aligner_is_noop(store: JsonScgStore) -> None:
    """No aligner injected => link_sources returns [] without raising."""
    embedder = _FakeEmbedder()
    parser = _parser(store, embedder)
    assert parser.link_sources(["github"]) == []
