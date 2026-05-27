"""StructureProvider — the code↔multiplex-key bridge.

``entity_key`` (``path/to/file.py#Qualified.Name``, no byte offsets) is the
shared identity that joins the memory and docs layers to the code graph.
This module owns the *only* derivation of an ``entity_key`` from a
``GraphNode`` and the resolution back to a live node.

``StructureProvider`` is a ``Protocol`` so the structural layer is pluggable
(corpus-agnostic seam — code today; PDF sections / DB schemas later). v1 ships
exactly one implementation, ``CodeStructureProvider``, which composes over the
wiki store. Keep it stateless: a refresh mutates the graph, so a cached map
would go stale.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .memory_types import EntityKey
from .types import GraphNode

if TYPE_CHECKING:
    from .store import WikiStoreBase


def entity_key_for_node(node: GraphNode) -> EntityKey:
    """Derive the multiplex ``entity_key`` for a code node.

    File nodes key on their bare path; every other symbol keys on
    ``file#name``. ``name`` is the tree-sitter symbol name — class→method
    qualification lands with the graph's scoping work, so two same-named
    methods in one file currently collapse to one key (an accepted v1
    over-approximation: a false anchor is wasted work, never data loss).
    """
    if node.type == "File":
        return node.file
    return f"{node.file}#{node.name}"


@runtime_checkable
class StructureProvider(Protocol):
    """Resolves between ``entity_key`` and the underlying structural unit."""

    def resolve(self, slug: str, entity_key: EntityKey) -> GraphNode | None:
        """Return the node addressed by *entity_key*, or None if absent."""
        ...

    def resolve_many(
        self, slug: str, entity_keys: list[EntityKey]
    ) -> dict[EntityKey, GraphNode]:
        """Resolve a batch in one pass; misses are omitted from the result."""
        ...

    def entity_key_of(self, slug: str, node_id: str) -> EntityKey | None:
        """Return the ``entity_key`` for a code ``node_id``, or None."""
        ...


class CodeStructureProvider:
    """``StructureProvider`` over the tree-sitter code graph (v1)."""

    def __init__(self, store: WikiStoreBase) -> None:
        """Compose over a wiki store (dependency-injected)."""
        self._store = store

    def resolve(self, slug: str, entity_key: EntityKey) -> GraphNode | None:
        """Return the node addressed by *entity_key*, or None if absent."""
        for node in self._store.query_graph(slug):
            if entity_key_for_node(node) == entity_key:
                return node
        return None

    def resolve_many(
        self, slug: str, entity_keys: list[EntityKey]
    ) -> dict[EntityKey, GraphNode]:
        """Resolve a batch in one graph pass; misses are omitted."""
        wanted = set(entity_keys)
        out: dict[EntityKey, GraphNode] = {}
        if not wanted:
            return out
        for node in self._store.query_graph(slug):
            key = entity_key_for_node(node)
            if key in wanted and key not in out:
                out[key] = node
                if len(out) == len(wanted):
                    break
        return out

    def entity_key_of(self, slug: str, node_id: str) -> EntityKey | None:
        """Return the ``entity_key`` for a code ``node_id``, or None."""
        for node in self._store.query_graph(slug):
            if node.node_id == node_id:
                return entity_key_for_node(node)
        return None


__all__ = ["StructureProvider", "CodeStructureProvider", "entity_key_for_node"]
