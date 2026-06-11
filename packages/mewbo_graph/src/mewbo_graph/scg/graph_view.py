"""ScgGraphView — the SCG multiplex assembler (#76, the search-side mirror).

``docs/features-search.md``: the SCG "is a tenant of the same three-layer
multiplex graph that powers the Agentic Wiki", holding **schema**, **entity**,
and **memory** layers in one store, queryable together. The wiki side already
has its assembler (``KnowledgeGraphView``); this is its search-side twin — the
ONE place the three SCG-tenant layers are unified for a source-id scope, with
cross-layer ``ANCHORS`` reconciled to real node ids.

Layers, per ``docs/features-search.md`` (the "shared multiplex graph" table):

* **schema** — the SCG structure for the scoped sources: ``capability`` /
  ``entity_type`` / ``field`` nodes + their edges (``HAS_ENTITY`` / ``HAS_FIELD``
  / ``SUPPORTS_QUERY`` / ``PRODUCES`` / ``CONSUMES`` / ``RESOLVES_TO``).
* **memory** — the learned connector notes (``corpus="connector"``) anchored to
  the schema layer's capability / entity-type nodes, the reachability facts a
  search run deposits.
* **entity** — the abstract-entity layer IF present (``mewbo_graph.entities``),
  best-effort: connectors don't yet mint abstract entities, so this is normally
  empty, but the assembler surfaces them when a future enrich path populates it.

Wire shape (``to_wire``) — self-contained, NO Flask / app import (an API route
will wrap it for ``GET /api/agentic_search/workspaces/<id>/graph``, #79):

    {
      "scope": [<source_id>, ...],
      "nodes": [{"data": {"id", "label", "kind", "layer", ...}}, ...],
      "edges": [{"data": {"id", "source", "target", "kind", "layer", "label?"}}],
      "stats": {"perLayer": {"schema", "memory", "entity"}, "totalNodes",
                "totalEdges"},
    }

Each node/edge carries a ``layer`` tag (``schema`` | ``memory`` | ``entity`` |
``cross``); a ``cross``-layer edge is a reconciled memory/entity → schema
``ANCHORS`` whose endpoints are BOTH real nodes in the payload (no dangling
edges). Construction is exclusively via :meth:`for_scope` so the invariant
"every emitted edge endpoint is a real node in the payload" stays enforced in
one place. Immutable once built — safe to share / cache.

Security invariant (spec §6): the schema layer carries only redacted descriptors
— ``auth_scope`` is dropped from the wire (never a token / credential / record
value); the memory layer is propositional reachability facts only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mewbo_graph.wiki.memory_types import MemoryFilter

from .memory_bridge import CONNECTOR_CORPUS, CONNECTOR_SLUG, ScgAnchorResolver
from .types import ScgEdge, ScgNode

if TYPE_CHECKING:
    from mewbo_graph.wiki.memory_types import MemoryEdge, MemoryNode
    from mewbo_graph.wiki.store import WikiStoreBase

    from .store import ScgStore

# Memory-node label truncation for the FE (mirrors KnowledgeGraphView's caps).
_MEMORY_LABEL_CHARS = 60
_MEMORY_SNIPPET_CHARS = 200


@dataclass(frozen=True, slots=True)
class ScgGraphView:
    """Source-scoped projection of the SCG multiplex (schema + memory + entity).

    Built via :meth:`for_scope` over a set of source ids (a workspace's enabled
    sources). Holds the three layers as typed tuples plus the reconciled
    cross-layer ANCHORS as ``(memory_node_id, schema_node_id)`` pairs.
    """

    scope: tuple[str, ...]
    # schema layer — SCG structure nodes/edges for the scoped sources.
    schema_nodes: tuple[ScgNode, ...]
    schema_edges: tuple[ScgEdge, ...]
    # memory layer — connector notes anchored into the scoped schema.
    memory_nodes: tuple[MemoryNode, ...]
    memory_edges: tuple[MemoryEdge, ...]  # note↔note RELATES only
    # cross-layer ANCHORS: memory node_id → schema node_id (both in payload).
    cross_edges: tuple[tuple[str, str], ...]

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def for_scope(
        cls,
        scg_store: ScgStore,
        wiki_store: WikiStoreBase,
        source_ids: list[str],
    ) -> ScgGraphView:
        """Assemble the multiplex for *source_ids* (a workspace's sources).

        Schema: every SCG node for the scoped sources + every edge whose BOTH
        endpoints stay in scope (an out-of-scope ``RESOLVES_TO`` partner is
        dropped — no dangling endpoint). Memory: the connector notes anchored to
        an in-scope schema node, reconciled via :class:`ScgAnchorResolver` (the
        same ``source_key``→node resolver the deposit path uses), so a note
        anchored only to out-of-scope sources is dropped. Empty ``source_ids``
        yields an empty view (never the whole catalog — mirrors ``ScgScope``).
        """
        scope = sorted(set(source_ids))

        # ── schema layer ────────────────────────────────────────────────
        schema_nodes = [
            n for sid in scope for n in scg_store.query_nodes(source_id=sid)
        ]
        node_keys = {n.source_key for n in schema_nodes}
        # An edge is in-scope iff BOTH endpoints are scoped schema nodes — a
        # cross-source RESOLVES_TO to an out-of-scope partner is dropped (no
        # dangling endpoint), the SCG analogue of the wiki view's orphan hygiene.
        schema_edges = [
            e
            for e in scg_store.list_edges()
            if e.source in node_keys and e.target in node_keys
        ]

        # ── memory layer ────────────────────────────────────────────────
        memory_nodes, memory_edges, cross_edges = cls._memory_layer(
            scg_store, wiki_store, node_keys
        )

        return cls(
            scope=tuple(scope),
            schema_nodes=tuple(schema_nodes),
            schema_edges=tuple(schema_edges),
            memory_nodes=tuple(memory_nodes),
            memory_edges=tuple(memory_edges),
            cross_edges=tuple(cross_edges),
        )

    @staticmethod
    def _memory_layer(
        scg_store: ScgStore,
        wiki_store: WikiStoreBase,
        node_keys: set[str],
    ) -> tuple[list[MemoryNode], list[MemoryEdge], list[tuple[str, str]]]:
        """Connector notes + their note↔note RELATES + reconciled cross ANCHORS.

        A connector note is kept iff at least one of its live ANCHORS targets
        resolves (via :class:`ScgAnchorResolver`) to an in-scope schema node —
        so the memory layer never surfaces a note that hangs entirely off
        out-of-scope sources. ``RELATES`` edges are kept only between two kept
        notes; each kept ANCHORS becomes a ``(note_id, schema_node_id)`` cross
        edge whose schema endpoint is guaranteed to be a real node in the payload.
        """
        try:
            all_notes = wiki_store.query_memory(
                CONNECTOR_SLUG, filt=MemoryFilter(corpus=CONNECTOR_CORPUS)
            )
        except Exception:  # noqa: BLE001 — a graph-only / empty memory store
            return [], [], []

        resolver = ScgAnchorResolver(scg_store)
        kept_notes: dict[str, MemoryNode] = {}
        cross_edges: list[tuple[str, str]] = []
        for note in all_notes:
            anchored = False
            for edge in wiki_store.list_memory_edges(
                CONNECTOR_SLUG, node_id=note.node_id
            ):
                if edge.type != "ANCHORS":
                    continue
                scg_node = resolver.resolve(CONNECTOR_SLUG, edge.target)
                # In scope iff the anchor's source_key is a node in this payload.
                if scg_node is None or scg_node.source_key not in node_keys:
                    continue
                cross_edges.append((note.node_id, scg_node.node_id))
                anchored = True
            if anchored:
                kept_notes[note.node_id] = note

        # note↔note RELATES, only between two kept notes (no dangling endpoint).
        memory_edges = [
            e
            for note_id in kept_notes
            for e in wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=note_id)
            if e.type == "RELATES" and e.target in kept_notes
        ]
        return list(kept_notes.values()), memory_edges, cross_edges

    # ── Derived state ───────────────────────────────────────────────────

    @property
    def kinds(self) -> dict[str, int]:
        """Per-kind schema-node histogram (capability/entity_type/field/…)."""
        return dict(Counter(n.kind for n in self.schema_nodes))

    # ── Serialisation ───────────────────────────────────────────────────

    def to_wire(self) -> dict[str, Any]:
        """Layer-tagged ``{scope, nodes, edges, stats}`` — no Flask, no app dep.

        Self-contained so an API route can wrap it verbatim (#79). Every node /
        edge is ``{data: {...}}`` with a ``layer`` tag; ``auth_scope`` is dropped
        (redaction invariant). The schema endpoints of cross edges are the SCG
        ``node_id`` — already a real schema node in ``nodes`` — so the FE never
        sees a dangling edge.
        """
        nodes = [self._schema_node_to_wire(n) for n in self.schema_nodes] + [
            self._memory_node_to_wire(m) for m in self.memory_nodes
        ]
        edges = (
            [self._schema_edge_to_wire(e) for e in self.schema_edges]
            + [self._memory_edge_to_wire(e) for e in self.memory_edges]
            + [self._cross_edge_to_wire(s, t) for s, t in self.cross_edges]
        )
        return {
            "scope": list(self.scope),
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "totalNodes": len(self.schema_nodes) + len(self.memory_nodes),
                "totalEdges": (
                    len(self.schema_edges)
                    + len(self.memory_edges)
                    + len(self.cross_edges)
                ),
                "kinds": self.kinds,
                "perLayer": {
                    "schema": len(self.schema_nodes),
                    "memory": len(self.memory_nodes),
                    # entity layer is best-effort + currently unpopulated for
                    # connectors — surfaced as 0 so the FE Record map stays
                    # exhaustive (a future enrich path fills it without a FE change).
                    "entity": 0,
                },
            },
        }

    # ── Static helpers (per-record formatters) ──────────────────────────

    @staticmethod
    def _schema_node_to_wire(n: ScgNode) -> dict[str, Any]:
        # auth_scope is REDACTED off the wire (never echo even the descriptor).
        return {
            "data": {
                "id": n.node_id,
                "label": n.name,
                "kind": n.kind,
                "layer": "schema",
                "sourceId": n.source_id,
                "sourceKey": n.source_key,
                "doc": n.doc,
            },
        }

    @staticmethod
    def _memory_node_to_wire(m: MemoryNode) -> dict[str, Any]:
        content = m.content.strip()
        label = content[:_MEMORY_LABEL_CHARS]
        if len(content) > _MEMORY_LABEL_CHARS:
            label += "…"
        return {
            "data": {
                "id": m.node_id,
                "label": label,
                "kind": "Memory",
                "layer": "memory",
                "snippet": content[:_MEMORY_SNIPPET_CHARS],
                "labels": list(m.labels),
            },
        }

    @staticmethod
    def _schema_edge_to_wire(e: ScgEdge) -> dict[str, Any]:
        # Endpoints are source_keys (the SCG edge addressing); the FE joins them
        # to schema nodes by the node's ``sourceKey`` data field.
        return {
            "data": {
                "id": f"{e.source}__{e.kind}__{e.target}",
                "source": e.source,
                "target": e.target,
                "kind": e.kind,
                "layer": "schema",
                "weight": e.weight,
            },
        }

    @staticmethod
    def _memory_edge_to_wire(e: MemoryEdge) -> dict[str, Any]:
        return {
            "data": {
                "id": f"{e.source}__RELATES__{e.target}",
                "source": e.source,
                "target": e.target,
                "kind": "RELATES",
                "layer": "memory",
            },
        }

    @staticmethod
    def _cross_edge_to_wire(source: str, target: str) -> dict[str, Any]:
        # source = memory node_id, target = schema node_id (both real in payload).
        return {
            "data": {
                "id": f"{source}__ANCHORS__{target}",
                "source": source,
                "target": target,
                "kind": "ANCHORS",
                "layer": "cross",
            },
        }


__all__ = ["ScgGraphView"]
