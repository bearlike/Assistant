"""EntityAnchorResolver — entities as anchorable multiplex units.

Implements the wiki ``AnchorResolver`` Protocol (``resolve`` / ``resolve_many`` /
``entity_key_of``) over ``entity:<id>`` keys, so memory notes/insights can ANCHOR
to an entity (the ``InsightIngestor`` only creates the live ANCHORS edge for
anchors a resolver can resolve — ``ScgAnchorResolver`` is the precedent). This is
the seam that ties code symbols, connector schemas, notes, and entities into ONE
graph. Stateless beyond the injected store (a re-map would stale a cache).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .types import Entity

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase

_ENTITY_PREFIX = "entity:"


def entity_key_for(entity: Entity) -> str:
    """The multiplex anchor key for an entity: ``entity:<id>``."""
    return f"{_ENTITY_PREFIX}{entity.id}"


class EntityAnchorResolver:
    """``AnchorResolver`` backed by the entity store (``entity:<id>`` → Entity)."""

    def __init__(self, store: WikiStoreBase) -> None:
        """Compose over an entity-capable store (dependency-injected)."""
        self._store = store

    def resolve(self, slug: str, entity_key: str) -> Entity | None:
        """Return the entity addressed by ``entity:<id>``, or None."""
        if not entity_key.startswith(_ENTITY_PREFIX):
            return None
        return self._store.get_entity(slug, entity_key[len(_ENTITY_PREFIX) :])

    def resolve_many(self, slug: str, entity_keys: list[str]) -> dict[str, Entity]:
        """Resolve a batch by key; misses (and wrong-prefix keys) are omitted."""
        out: dict[str, Entity] = {}
        for key in entity_keys:
            if key in out:
                continue
            node = self.resolve(slug, key)
            if node is not None:
                out[key] = node
        return out

    def entity_key_of(self, slug: str, node_id: str) -> str | None:
        """Return ``entity:<id>`` for an entity id, or None if absent."""
        node = self._store.get_entity(slug, node_id)
        return entity_key_for(node) if node is not None else None


__all__ = ["EntityAnchorResolver", "entity_key_for"]
