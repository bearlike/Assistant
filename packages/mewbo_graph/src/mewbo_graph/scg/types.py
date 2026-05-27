"""Typed contracts for the Source Capability Graph (SCG) — spec §6.

The SCG indexes *reachability* — the schemas and qualified pathways a source
exposes, **never the data behind them**. These models are the shared surface
the structure providers, parser, router, and traversal engine all build
*against*; everything else in the ``scg`` package references them.

Conventions mirror :mod:`mewbo_api.agentic_search.schemas` and the wiki types:

* Every model subclasses :class:`_Wire` (``extra="forbid"``,
  ``populate_by_name=True``) so unknown keys are rejected at the boundary.
* ``node_id`` is a deterministic ``sha1(source_key|kind)[:16]`` derived by
  :meth:`ScgNode.make_id` and *overwritten* on every validate — that derivation
  is the single source of node identity (mirrors the wiki graph's
  ``_stable_id`` and the memory layer's content-addressed node ids).

Security invariant (spec §6): SCG nodes carry only a *redacted* ``auth_scope``
descriptor string — **never** persist tokens, credentials, or any secret.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Identity + vocabulary aliases ───────────────────────────────────────────

# ``<source_id>#<Qualified.Name>``. Flat MCP tool lists map to
# ``<source_id>#<tool_name>``. This is the stable anchor the learned
# memory layer (#13) hangs connector insights off.
SourceKey = str


def field_leaf(field_key: SourceKey) -> str:
    """Return the trailing ``.``-segment of a ``<cap>.<name>`` field key (lower).

    The one canonical home for the "trailing field-name segment, lower-cased"
    idiom shared by the parser's field indexing and the type aligner's
    field-overlap heuristic (DRY).
    """
    return field_key.rsplit(".", 1)[-1].lower()

NodeKind = Literal["source", "entity_type", "field", "capability", "route_recipe"]
EdgeKind = Literal[
    "HAS_ENTITY", "HAS_FIELD", "SUPPORTS_QUERY", "PRODUCES", "CONSUMES", "RESOLVES_TO"
]
# How a field may be supplied to a capability: ``free`` (free-text),
# ``bound`` (must be supplied — access-pattern limit), ``optional``.
BindMode = Literal["free", "bound", "optional"]
# Provenance of an edge — how the parser asserted it.
EdgeMethod = Literal["key", "embedding", "llm", "type_align"]


class _Wire(BaseModel):
    """Base for SCG models: forbid unknown keys, allow snake_case aliases."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ── Capability binding ──────────────────────────────────────────────────────


class CapabilityBinding(_Wire):
    """One field a capability binds, with its access mode + allowed operators.

    Binding patterns keep traversal honest: a capability "queryable by
    ``service_id``, not free-text" emits ``mode="bound"`` so the router only
    proposes *executable* plans (Florescu/Vassalos SIGMOD'99).
    """

    field_key: SourceKey
    mode: BindMode
    operators: list[str] = Field(default_factory=list)


# ── Node ────────────────────────────────────────────────────────────────────


class ScgNode(_Wire):
    """A node in the Source Capability Graph.

    ``node_id`` is *always* the canonical ``sha1(source_key|kind)[:16]`` — any
    supplied value is overwritten on validate so identity stays content-addressed
    and stable across re-indexes.
    """

    source_key: SourceKey
    node_id: str = ""
    kind: NodeKind
    source_id: str
    name: str
    doc: str = ""
    example_queries: list[str] = Field(default_factory=list)
    bindings: list[CapabilityBinding] = Field(default_factory=list)
    # Redacted auth descriptor ONLY — never a token/credential (spec §6).
    auth_scope: str | None = None

    @staticmethod
    def make_id(source_key: SourceKey, kind: NodeKind) -> str:
        """Deterministic node id over ``(source_key, kind)`` — sha1[:16]."""
        return hashlib.sha1(f"{source_key}|{kind}".encode()).hexdigest()[:16]

    @model_validator(mode="after")
    def _derive_node_id(self) -> ScgNode:
        """Force ``node_id`` to the canonical derivation (overwrites any input)."""
        canonical = self.make_id(self.source_key, self.kind)
        if self.node_id != canonical:
            object.__setattr__(self, "node_id", canonical)
        return self


# ── Edge ────────────────────────────────────────────────────────────────────


class ScgEdge(_Wire):
    """A directed, weighted, provenanced edge between two ``SourceKey`` nodes.

    ``binds`` records the (source-field, target-field) pair an edge aligns on;
    ``method`` is the parser's evidence kind. ``valid_at`` / ``invalid_at``
    carry the invalidate-don't-delete validity window (Graphiti) so learned
    edges can be retired without losing provenance.
    """

    source: SourceKey
    target: SourceKey
    kind: EdgeKind
    weight: float = 1.0
    binds: tuple[SourceKey, SourceKey] | None = None
    method: EdgeMethod | None = None
    evidence: list[str] = Field(default_factory=list)
    valid_at: str | None = None
    invalid_at: str | None = None


# ── Route recipe ────────────────────────────────────────────────────────────


class RouteRecipe(_Wire):
    """A precomputed qualified path (ordered ``SourceKey`` steps) over the SCG."""

    source_key: SourceKey
    steps: list[SourceKey]
    cost_estimate: float = 0.0


# ── Embedding ───────────────────────────────────────────────────────────────


class ScgEmbedding(_Wire):
    """A dense embedding vector for an SCG node (parallels the wiki Embedding)."""

    node_id: str
    vector: list[float]
    model: str
    dim: int


# ── Source descriptor (provider INPUT) ──────────────────────────────────────


class SourceDescriptor(_Wire):
    """The raw, source-type-specific descriptor a structure provider parses.

    ``raw`` is the opaque provider payload (OpenAPI doc, MCP tool list, GraphQL
    SDL, SQL schema…). Carries no secrets — auth lives in the connector config,
    not here.
    """

    source_id: str
    source_type: str
    raw: dict[str, object]
    schema_version: str | None = None


# ── Structure graph (provider OUTPUT) ───────────────────────────────────────


class StructureGraph(_Wire):
    """The normalized provider output: nodes + edges + recipes for one source.

    A provider returns one source's subgraph; ``ScgParser.parse_source`` upserts
    it into the persisted whole-catalog SCG directly (one source at a time).
    """

    nodes: list[ScgNode] = Field(default_factory=list)
    edges: list[ScgEdge] = Field(default_factory=list)
    recipes: list[RouteRecipe] = Field(default_factory=list)


__all__ = [
    "SourceKey",
    "field_leaf",
    "NodeKind",
    "EdgeKind",
    "BindMode",
    "EdgeMethod",
    "CapabilityBinding",
    "ScgNode",
    "ScgEdge",
    "RouteRecipe",
    "ScgEmbedding",
    "SourceDescriptor",
    "StructureGraph",
]
