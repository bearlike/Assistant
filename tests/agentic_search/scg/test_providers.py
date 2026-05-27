"""Provider seam tests — OpenAPI / MCP-tool-list / LLM-fallback structure providers.

Each provider parses a source-type-specific :class:`SourceDescriptor` into a
normalized :class:`StructureGraph` of ``ScgNode`` / ``ScgEdge`` tuples. These
tests pin the contract the spec (Gitea #19 P3) fixes: deterministic source
keys, binding modes (required→``bound`` / optional→``optional``), the
HAS_ENTITY / HAS_FIELD / SUPPORTS_QUERY / PRODUCES edge fabric, and the
registry dispatch by ``source_type``. No network, no real LLM.
"""

from __future__ import annotations

import pytest
from mewbo_graph.scg.providers import (
    LlmStructureProvider,
    McpToolListStructureProvider,
    OpenApiStructureProvider,
    StructureProviderRegistry,
)
from mewbo_graph.scg.types import SourceDescriptor, StructureGraph

# ── fixtures ────────────────────────────────────────────────────────────────


def _openapi_descriptor() -> SourceDescriptor:
    """A small OpenAPI 3.1 doc: one schema + one operation with mixed params."""
    return SourceDescriptor(
        source_id="github",
        source_type="openapi",
        raw={
            "openapi": "3.1.0",
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


def _mcp_descriptor() -> SourceDescriptor:
    """An MCP server tool-list payload: one tool with input + output schema."""
    return SourceDescriptor(
        source_id="filesystem",
        source_type="mcp_tool_list",
        raw={
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file from disk.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "encoding": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "contents": {"type": "string"},
                            "size": {"type": "integer"},
                        },
                    },
                }
            ]
        },
    )


# ── OpenAPI provider ──────────────────────────────────────────────────────────


def test_openapi_emits_source_entity_and_capability_nodes():
    """source + entity_type (from schemas) + capability (from operations) nodes."""
    graph = OpenApiStructureProvider().build_structure(_openapi_descriptor())
    assert isinstance(graph, StructureGraph)

    by_kind: dict[str, list[str]] = {}
    for node in graph.nodes:
        by_kind.setdefault(node.kind, []).append(node.source_key)

    assert by_kind["source"] == ["github"]
    assert "github#Issue" in by_kind["entity_type"]
    assert "github#search_issues" in by_kind["capability"]
    # entity fields become field nodes.
    assert "github#Issue.id" in by_kind["field"]
    assert "github#Issue.title" in by_kind["field"]


def test_openapi_binding_modes_from_parameter_required():
    """required param => bound; optional param => optional; query => operators."""
    graph = OpenApiStructureProvider().build_structure(_openapi_descriptor())
    cap = next(n for n in graph.nodes if n.source_key == "github#search_issues")

    modes = {b.field_key: b.mode for b in cap.bindings}
    assert modes["github#search_issues.repo"] == "bound"
    assert modes["github#search_issues.state"] == "optional"

    # query params expose operators; path params do not.
    state = next(b for b in cap.bindings if b.field_key.endswith(".state"))
    assert state.operators  # non-empty for in:query
    repo = next(b for b in cap.bindings if b.field_key.endswith(".repo"))
    assert repo.operators == []


def test_openapi_edges_has_entity_has_field_supports_query():
    """source HAS_ENTITY entity; entity HAS_FIELD field; capability SUPPORTS_QUERY field."""
    graph = OpenApiStructureProvider().build_structure(_openapi_descriptor())
    edges = {(e.source, e.kind, e.target) for e in graph.edges}

    assert ("github", "HAS_ENTITY", "github#Issue") in edges
    assert ("github#Issue", "HAS_FIELD", "github#Issue.id") in edges
    assert (
        "github#search_issues",
        "SUPPORTS_QUERY",
        "github#search_issues.repo",
    ) in edges


def test_openapi_never_persists_secrets():
    """A bearer token in the raw doc is never copied into any node attribute."""
    desc = _openapi_descriptor()
    desc.raw["x-access-token"] = "ghp_SECRETTOKEN"  # type: ignore[index]
    graph = OpenApiStructureProvider().build_structure(desc)
    dumped = graph.model_dump_json()
    assert "ghp_SECRETTOKEN" not in dumped


# ── MCP tool-list provider ──────────────────────────────────────────────────


def test_mcp_emits_capability_per_tool_with_tool_source_key():
    """One capability node per tool, keyed <source_id>#<tool_name>."""
    graph = McpToolListStructureProvider().build_structure(_mcp_descriptor())
    caps = [n for n in graph.nodes if n.kind == "capability"]
    assert [c.source_key for c in caps] == ["filesystem#read_file"]
    assert caps[0].name == "read_file"
    assert caps[0].doc == "Read a file from disk."


def test_mcp_input_schema_required_drives_binding_mode():
    """required input => bound; non-required => optional."""
    graph = McpToolListStructureProvider().build_structure(_mcp_descriptor())
    cap = next(n for n in graph.nodes if n.source_key == "filesystem#read_file")
    modes = {b.field_key: b.mode for b in cap.bindings}
    assert modes["filesystem#read_file.path"] == "bound"
    assert modes["filesystem#read_file.encoding"] == "optional"


def test_mcp_output_schema_emits_produces_edges():
    """Output-schema fields become PRODUCES edges from the capability."""
    graph = McpToolListStructureProvider().build_structure(_mcp_descriptor())
    produces = {
        e.target for e in graph.edges
        if e.kind == "PRODUCES" and e.source == "filesystem#read_file"
    }
    assert "filesystem#read_file.contents" in produces
    assert "filesystem#read_file.size" in produces


def test_mcp_empty_tool_list_yields_only_source_node():
    """A toolless server still yields its source node, no capabilities."""
    desc = SourceDescriptor(
        source_id="empty", source_type="mcp_tool_list", raw={"tools": []}
    )
    graph = McpToolListStructureProvider().build_structure(desc)
    assert [n.kind for n in graph.nodes] == ["source"]


# ── LLM fallback provider ─────────────────────────────────────────────────────


def test_llm_provider_raises_without_injected_callable():
    """Default DI is None — using the provider with no llm raises, never silently no-ops."""
    desc = SourceDescriptor(
        source_id="legacy",
        source_type="text",
        raw={"description": "A legacy CRM with no schema."},
    )
    with pytest.raises(RuntimeError):
        LlmStructureProvider().build_structure(desc)


def test_llm_provider_uses_injected_fake_for_coarse_capability():
    """An injected fake llm produces one coarse capability node (no real LLM)."""
    calls: list[str] = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return "search_crm"

    desc = SourceDescriptor(
        source_id="legacy",
        source_type="text",
        raw={"description": "A legacy CRM with no schema."},
    )
    graph = LlmStructureProvider(llm=fake_llm).build_structure(desc)

    assert calls  # the fake was actually invoked
    kinds = {n.kind for n in graph.nodes}
    assert "source" in kinds
    assert "capability" in kinds
    cap = next(n for n in graph.nodes if n.kind == "capability")
    assert cap.source_key == "legacy#search_crm"
    # source -> capability is wired so the router can reach it.
    edges = {(e.source, e.kind, e.target) for e in graph.edges}
    assert ("legacy", "HAS_ENTITY", "legacy#search_crm") in edges


# ── registry / dispatch ───────────────────────────────────────────────────────


def test_registry_dispatches_by_source_type():
    """The registry resolves a provider by descriptor.source_type."""
    registry = StructureProviderRegistry.with_defaults()
    assert isinstance(
        registry.for_type("openapi"), OpenApiStructureProvider
    )
    assert isinstance(
        registry.for_type("mcp_tool_list"), McpToolListStructureProvider
    )


def test_registry_build_routes_to_correct_provider():
    """build() picks the provider matching the descriptor and parses it."""
    registry = StructureProviderRegistry.with_defaults()
    graph = registry.build(_mcp_descriptor())
    assert any(n.source_key == "filesystem#read_file" for n in graph.nodes)


def test_registry_unknown_source_type_raises():
    """An unregistered source_type raises KeyError, not a silent empty graph."""
    registry = StructureProviderRegistry.with_defaults()
    with pytest.raises(KeyError):
        registry.build(
            SourceDescriptor(source_id="x", source_type="graphql", raw={})
        )


def test_registry_register_adds_a_provider():
    """A new source type = one register() call, zero core edits (RML mandate)."""
    registry = StructureProviderRegistry.with_defaults()
    provider = LlmStructureProvider(llm=lambda _p: "noop")
    registry.register(provider)
    assert registry.for_type("text") is provider
