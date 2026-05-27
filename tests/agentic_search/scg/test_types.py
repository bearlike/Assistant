"""Round-trip + extra-forbid + deterministic-id tests for SCG types."""

from __future__ import annotations

import pytest
from mewbo_graph.scg.types import (
    CapabilityBinding,
    RouteRecipe,
    ScgEdge,
    ScgEmbedding,
    ScgNode,
    SourceDescriptor,
    StructureGraph,
)
from pydantic import ValidationError


def _roundtrip(model_cls, data: dict):
    """Validate, dump to a JSON dict, re-parse — assert equal; return the obj."""
    obj = model_cls.model_validate(data)
    dumped = obj.model_dump(mode="json")
    reparsed = model_cls.model_validate(dumped)
    assert obj == reparsed
    return obj


# ── extra="forbid" ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_cls, valid_data",
    [
        (CapabilityBinding, {"field_key": "github#repo.id", "mode": "bound"}),
        (
            ScgNode,
            {
                "source_key": "github#Repo",
                "kind": "entity_type",
                "source_id": "github",
                "name": "Repo",
            },
        ),
        (ScgEdge, {"source": "github#Repo", "target": "github#Repo.id", "kind": "HAS_FIELD"}),
        (RouteRecipe, {"source_key": "github#find_pr", "steps": ["github#search"]}),
        (ScgEmbedding, {"node_id": "abc", "vector": [0.1], "model": "m", "dim": 1}),
        (SourceDescriptor, {"source_id": "github", "source_type": "openapi", "raw": {}}),
        (StructureGraph, {}),
    ],
)
def test_extra_forbid(model_cls, valid_data):
    """Every SCG model rejects unknown fields."""
    model_cls.model_validate(valid_data)  # valid baseline
    with pytest.raises(ValidationError):
        model_cls.model_validate({**valid_data, "bogus_field": 1})


# ── deterministic node id ───────────────────────────────────────────────────


def test_make_id_is_deterministic_and_stable():
    """make_id is a pure function of (source_key, kind)."""
    a = ScgNode.make_id("github#Repo", "entity_type")
    b = ScgNode.make_id("github#Repo", "entity_type")
    assert a == b
    assert len(a) == 16
    # Different kind -> different id.
    assert ScgNode.make_id("github#Repo", "field") != a
    # Different source_key -> different id.
    assert ScgNode.make_id("gitlab#Repo", "entity_type") != a


def test_node_id_auto_derived_when_omitted():
    """A node with no node_id derives the deterministic id from (source_key, kind)."""
    node = ScgNode(
        source_key="github#Repo",
        kind="entity_type",
        source_id="github",
        name="Repo",
    )
    assert node.node_id == ScgNode.make_id("github#Repo", "entity_type")


def test_node_id_is_overwritten_to_canonical():
    """A supplied node_id is forced to the canonical deterministic value."""
    node = ScgNode(
        source_key="github#Repo",
        kind="entity_type",
        source_id="github",
        name="Repo",
        node_id="garbage",
    )
    assert node.node_id == ScgNode.make_id("github#Repo", "entity_type")


# ── round-trips ─────────────────────────────────────────────────────────────


def test_scg_node_roundtrip_with_bindings():
    """ScgNode (with nested CapabilityBinding) survives dump/validate."""
    node = _roundtrip(
        ScgNode,
        {
            "source_key": "github#search_issues",
            "kind": "capability",
            "source_id": "github",
            "name": "search_issues",
            "doc": "Search issues.",
            "example_queries": ["open PRs"],
            "bindings": [
                {"field_key": "github#repo", "mode": "bound", "operators": ["eq"]}
            ],
            "auth_scope": "repo:read",
        },
    )
    assert node.bindings[0].mode == "bound"
    assert node.auth_scope == "repo:read"


def test_scg_edge_roundtrip_with_binds_tuple():
    """ScgEdge round-trips, including the optional binds 2-tuple."""
    edge = _roundtrip(
        ScgEdge,
        {
            "source": "github#search_issues",
            "target": "github#Issue",
            "kind": "PRODUCES",
            "weight": 0.8,
            "binds": ["github#repo", "github#Issue.repo"],
            "method": "type_align",
            "evidence": ["schema match"],
        },
    )
    assert edge.binds == ("github#repo", "github#Issue.repo")
    assert edge.method == "type_align"


def test_route_recipe_roundtrip():
    """RouteRecipe round-trips."""
    recipe = _roundtrip(
        RouteRecipe,
        {
            "source_key": "github#find_issue_pr",
            "steps": ["github#search_issues", "github#get_pr"],
            "cost_estimate": 2.0,
        },
    )
    assert recipe.steps == ["github#search_issues", "github#get_pr"]


def test_source_descriptor_roundtrip():
    """SourceDescriptor carries an opaque raw dict and round-trips."""
    desc = _roundtrip(
        SourceDescriptor,
        {
            "source_id": "github",
            "source_type": "openapi",
            "raw": {"openapi": "3.1.0", "paths": {}},
            "schema_version": "3.1.0",
        },
    )
    assert desc.raw["openapi"] == "3.1.0"


# ── StructureGraph ──────────────────────────────────────────────────────────


def _node(name: str) -> ScgNode:
    return ScgNode(
        source_key=f"github#{name}",
        kind="entity_type",
        source_id="github",
        name=name,
    )


def test_structure_graph_roundtrip():
    """StructureGraph survives dump/validate with nested models."""
    g = StructureGraph(
        nodes=[_node("A")],
        edges=[ScgEdge(source="github#A", target="github#B", kind="HAS_FIELD")],
        recipes=[RouteRecipe(source_key="github#r", steps=["github#A"])],
    )
    again = StructureGraph.model_validate(g.model_dump(mode="json"))
    assert again == g
