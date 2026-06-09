"""Pydantic models for the abstract-entity layer.

Mirrors the memory-node identity idiom (``MemoryNode.compute_node_id``): the id
is *derived* and overwrites any supplied value, so two surface variants of the
same name+type collapse to one node — the deterministic-upsert convergence
guarantee. ``type`` is a SOFT attribute: a free-form string with a documented
seed vocabulary, stored as a property, NEVER an enum or a graph label.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Conventions match ``memory_types.py``: extra-forbid + populate_by_name.
_CFG = ConfigDict(extra="forbid", populate_by_name=True)

# Seed vocabulary — guidance for the agent, NOT a closed enum. ``type`` accepts
# any string; this list is what the AgentDef prompt suggests and what tests
# exercise. Open extension is the whole point (soft type).
SEED_ENTITY_TYPES: tuple[str, ...] = (
    "person",
    "project",
    "product",
    "organization",
    "concept",
    "student",
    "team",
)

EntityStatus = Literal["active", "needs_review"]
RecommendationAction = Literal["merge", "distinct", "retype", "create"]

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_entity_name(name: str) -> str:
    """Trim → lower → strip punctuation → collapse whitespace.

    The single normalization used for both the deterministic id and the
    fuzzy-match key, so the two can never disagree.
    """
    lowered = _PUNCT_RE.sub("", name.strip().lower())
    return _WS_RE.sub(" ", lowered).strip()


class EntityMention(BaseModel):
    """One provenance record for an entity — per-mention, so merges are reversible."""

    model_config = _CFG

    source: str
    insight_id: str | None = None
    ts: str
    surface_name: str


class Entity(BaseModel):
    """An abstract entity reified as a multiplex node.

    ``id`` is always ``sha1(normalized_name + "|" + type)`` — derived, never
    user-set. ``type`` is a soft free-form string (seed vocab + open extension).
    """

    model_config = _CFG

    # Always derived (= sha1(normalized_name|type)); any supplied value is ignored.
    id: str = ""
    name: str
    normalized_name: str = ""
    type: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    embedding: list[float] | None = None
    mentions: list[EntityMention] = Field(default_factory=list)
    status: EntityStatus = "active"
    labels: list[str] = Field(default_factory=list)

    @staticmethod
    def compute_id(normalized_name: str, type: str) -> str:
        """Deterministic id over ``(normalized_name, type)``."""
        return hashlib.sha1(f"{normalized_name}|{type}".encode()).hexdigest()

    @model_validator(mode="after")
    def _derive_identity(self) -> Entity:
        """Force ``normalized_name`` + ``id`` to their derived values."""
        norm = normalize_entity_name(self.name)
        if self.normalized_name != norm:
            self.normalized_name = norm
        derived = self.compute_id(norm, self.type)
        if self.id != derived:
            self.id = derived
        return self


class EntityRelation(BaseModel):
    """A typed, directed relationship between two entities (e.g. owns/works_on)."""

    model_config = _CFG

    # Always derived (= sha1(source_id|type|target_id)); a supplied value is ignored.
    id: str = ""
    source_id: str
    target_id: str
    type: str
    description: str = ""
    mentions: list[EntityMention] = Field(default_factory=list)

    @staticmethod
    def compute_id(source_id: str, type: str, target_id: str) -> str:
        """Deterministic id over ``(source_id, type, target_id)``."""
        return hashlib.sha1(f"{source_id}|{type}|{target_id}".encode()).hexdigest()

    @model_validator(mode="after")
    def _derive_id(self) -> EntityRelation:
        derived = self.compute_id(self.source_id, self.type, self.target_id)
        if self.id != derived:
            self.id = derived
        return self


class EntityRecommendation(BaseModel):
    """An agent-proposed resolution prior, consumed by ``EntityResolver`` next pass.

    ``subjects`` are canonical ``<normalized_name>|<type>`` keys (or entity ids).
    A ``merge`` prior on a pair forces a merge (and lowers the effective
    threshold), a ``distinct`` prior forbids it, a ``retype``/``create`` prior is
    surfaced to the caller.
    """

    model_config = _CFG

    action: RecommendationAction
    subjects: list[str]
    type: str | None = None
    rationale: str = ""


class EntityEmbedding(BaseModel):
    """Dense embedding vector for an entity (mirrors ``MemoryEmbedding``)."""

    model_config = _CFG

    slug: str
    entity_id: str
    vector: list[float]
    model: str
    dim: int


class EntityFilter(BaseModel):
    """Optional facets applied to entity retrieval (all default to no-op)."""

    model_config = _CFG

    type: str | None = None
    status: EntityStatus | None = None
    labels: list[str] | None = None

    def matches(self, entity: Entity) -> bool:
        """Return True if *entity* satisfies the facets."""
        if self.type is not None and entity.type != self.type:
            return False
        if self.status is not None and entity.status != self.status:
            return False
        if self.labels and not set(self.labels).issubset(set(entity.labels)):
            return False
        return True


__all__ = [
    "SEED_ENTITY_TYPES",
    "Entity",
    "EntityEmbedding",
    "EntityFilter",
    "EntityMention",
    "EntityRecommendation",
    "EntityRelation",
    "EntityStatus",
    "RecommendationAction",
    "normalize_entity_name",
]
