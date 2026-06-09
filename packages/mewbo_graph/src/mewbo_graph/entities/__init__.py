"""Abstract-entity layer over the shared multiplex (Gitea Issue #35).

Entities are UML-actor-like nouns (person/project/product/organization/concept/
student/team + open extension) minted by an LLM-driven agent into the SAME
``mewbo_graph`` store that holds code symbols, connector schemas, and atomic
notes. Identity is deterministic (``sha1(normalized_name|type)``) so every write
is an idempotent upsert; resolution runs through the one generalized
``ResolutionLadder`` shared with insight dedup.
"""
from __future__ import annotations

from .anchor import EntityAnchorResolver, entity_key_for
from .minter import EntityMinter
from .resolver import EntityResolver, LadderDecision, ResolutionLadder, fuzzy_ratio
from .types import (
    SEED_ENTITY_TYPES,
    Entity,
    EntityEmbedding,
    EntityFilter,
    EntityMention,
    EntityRecommendation,
    EntityRelation,
    EntityStatus,
    RecommendationAction,
    normalize_entity_name,
)

__all__ = [
    "SEED_ENTITY_TYPES",
    "Entity",
    "EntityAnchorResolver",
    "EntityEmbedding",
    "EntityFilter",
    "EntityMention",
    "EntityMinter",
    "EntityRecommendation",
    "EntityRelation",
    "EntityResolver",
    "EntityStatus",
    "LadderDecision",
    "RecommendationAction",
    "ResolutionLadder",
    "entity_key_for",
    "fuzzy_ratio",
    "normalize_entity_name",
]
