"""Pydantic models for the multiplex memory layer.

These overlay an evolving memory + docs graph on the existing tree-sitter
code graph (``types.py``). Three node families share one identity namespace
(``EntityKey``):

* **Code entities** — the existing ``GraphNode``/``GraphEdge`` (untouched).
* **Memory notes** — ``MemoryNode`` (ultra-small atomic claims) reified as
  nodes that ``ANCHORS`` to code entities and ``RELATES`` to siblings.
* **Doc pages** — ``DocPageNote`` (one generated wiki page = one node)
  anchored to the code it documents.

Conventions match ``types.py``: ``model_config = ConfigDict(extra="forbid",
populate_by_name=True)`` and snake_case attributes. ``MemoryNode.node_id`` is
*derived* — ``sha1(slug | content.strip().lower())[:16]`` — so two notes with
the same normalized claim collapse to the same id (the exact-dup dedup tier).
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Shared config ───────────────────────────────────────────────────────────

_CFG = ConfigDict(extra="forbid", populate_by_name=True)

# The multiplex join key: ``path/to/file.py#Qualified.Name`` (NO byte offsets).
# A bare ``path`` (no ``#``) addresses a File-level entity.
EntityKey = str

MemoryKind = Literal["propositional", "prescriptive"]
MemorySource = Literal["indexer", "qa", "on_demand"]
MemoryEdgeType = Literal["ANCHORS", "RELATES"]
DocEdgeType = Literal["ANCHORS", "COVERS_MODULE", "CROSS_REFS"]
DocPageType = Literal["concept", "module", "subsystem", "api_reference", "how_to"]
DocGenerationPolicy = Literal["keep", "edit", "regenerate", "create_new"]

# A memory note is one atomic claim. Cap keeps notes ANN-tight and multi-hop
# friendly (over-long notes decontextualize — Molecular Facts, EMNLP'24).
MAX_INSIGHT_CHARS = 200


# ── Provenance ──────────────────────────────────────────────────────────────


class MemoryProvenance(BaseModel):
    """Who/when/how a memory note was created — citable audit trail."""

    model_config = _CFG

    author_agent: str
    source: MemorySource
    session_id: str | None = None
    created_at: str
    updated_at: str | None = None


# ── Memory node / edge / embedding ──────────────────────────────────────────


class MemoryNode(BaseModel):
    """An atomic memory claim reified as a multiplex graph node.

    ``node_id`` is always derived from ``(slug, content)`` — any supplied
    value is overwritten. This makes identical normalized claims share one
    id, which is exactly the exact-match tier of the dedup ladder.
    """

    model_config = _CFG

    slug: str
    node_id: str = ""
    content: str = Field(max_length=MAX_INSIGHT_CHARS)
    kind: MemoryKind = "propositional"
    labels: list[str] = Field(default_factory=list)
    corpus: str = "code"
    provenance: MemoryProvenance
    # ISO timestamp of the last incremental-refresh anchor check (idempotency
    # guard in ``MemoryReconciler``); ``None`` until first reconciled.
    anchor_checked_at: str | None = None

    @staticmethod
    def compute_node_id(slug: str, content: str) -> str:
        """Deterministic id over ``(slug, normalized content)``."""
        h = hashlib.sha1(f"{slug}|{content.strip().lower()}".encode())
        return h.hexdigest()[:16]

    @model_validator(mode="after")
    def _derive_node_id(self) -> MemoryNode:
        """Force ``node_id`` to the derived value (single source of truth)."""
        derived = self.compute_node_id(self.slug, self.content)
        if self.node_id != derived:
            self.node_id = derived
        return self


class MemoryEdge(BaseModel):
    """Directed multiplex edge.

    ``ANCHORS``: memory ``node_id`` → code ``EntityKey`` (the de-facto
    hyperedge fan-out). ``RELATES``: memory ``node_id`` → memory ``node_id``.
    Validity is a single nullable axis — ``invalid_at=None`` means live;
    setting it invalidates the edge (Graphiti invalidate-don't-delete).
    """

    model_config = _CFG

    slug: str
    source: str
    target: str
    type: MemoryEdgeType
    weight: float = 1.0
    valid_at: str
    invalid_at: str | None = None


class MemoryEmbedding(BaseModel):
    """Dense embedding vector for a memory node (mirrors ``Embedding``)."""

    model_config = _CFG

    slug: str
    node_id: str
    vector: list[float]
    model: str
    dim: int


# ── Documentation-page node ─────────────────────────────────────────────────


class DocPageNote(BaseModel):
    """A generated wiki page as a first-class multiplex node.

    Anchored to code via ``anchor_keys`` (resolved from the page's
    frontmatter ``relevantSources``). The incremental refresh propagates
    change impact onto these to decide keep/edit/regenerate/create.
    """

    model_config = _CFG

    slug: str
    page_id: str
    title: str
    content_hash: str
    page_type: DocPageType
    anchor_keys: list[EntityKey] = Field(default_factory=list)
    staleness_score: float = 0.0
    staleness_reason: str = "clean"
    generation_policy: DocGenerationPolicy = "keep"
    last_indexed_commit: str | None = None


# ── Incremental-refresh manifest ────────────────────────────────────────────


class FileManifest(BaseModel):
    """Per-``(slug, path)`` content-hash + entity index for scoped retraction.

    The incremental refresh diffs the stored ``content_hash`` against the
    working tree and, for dirty files, retracts exactly the listed
    ``entity_keys`` instead of rebuilding the whole graph.
    """

    model_config = _CFG

    slug: str
    path: str
    content_hash: str
    last_indexed_commit: str | None = None
    entity_keys: list[EntityKey] = Field(default_factory=list)


# ── Retrieval filter ────────────────────────────────────────────────────────


class MemoryFilter(BaseModel):
    """Optional facets applied to memory retrieval (all default to no-op)."""

    model_config = _CFG

    corpus: str | None = None
    source: MemorySource | None = None
    kind: MemoryKind | None = None
    labels: list[str] | None = None
    valid_at: str | None = None
    exclude_invalidated: bool = True

    def matches_node(self, node: MemoryNode) -> bool:
        """Return True if *node* satisfies the node-level facets.

        Edge-level validity (``valid_at`` / ``exclude_invalidated``) is
        applied separately by the store/retriever against the edge set.
        """
        if self.corpus is not None and node.corpus != self.corpus:
            return False
        if self.source is not None and node.provenance.source != self.source:
            return False
        if self.kind is not None and node.kind != self.kind:
            return False
        if self.labels and not set(self.labels).issubset(set(node.labels)):
            return False
        return True


__all__ = [
    "EntityKey",
    "MemoryKind",
    "MemorySource",
    "MemoryEdgeType",
    "DocEdgeType",
    "DocPageType",
    "DocGenerationPolicy",
    "MAX_INSIGHT_CHARS",
    "MemoryProvenance",
    "MemoryNode",
    "MemoryEdge",
    "MemoryEmbedding",
    "DocPageNote",
    "FileManifest",
    "MemoryFilter",
]
