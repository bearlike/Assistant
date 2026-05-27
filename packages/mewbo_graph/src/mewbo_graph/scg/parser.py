"""ScgParser — the registry that maps sources into the persisted SCG (spec §6).

This is the parser's *control* layer: the per-type
:class:`~mewbo_graph.scg.providers.base.SourceStructureProvider`s do
the descriptor→graph parsing; ``ScgParser`` owns persistence, embedding, and the
cross-capability / cross-source joins that no single provider can see:

* :meth:`parse_source` — dispatch one descriptor to the provider for its
  ``source_type``, **clean re-map** (``store.delete_source`` first so a re-index
  never accumulates stale/duplicate nodes), persist nodes/edges/recipes + the
  descriptor, then embed every node and upsert an :class:`ScgEmbedding`.
* :meth:`link_sources` — run the injected :class:`TypeAligner` to deposit
  ``RESOLVES_TO`` hypothesis edges across sources (no-op without an aligner).
* :meth:`compute_param_edges` — the In-N-Out (``2509.01560``) producer→consumer
  join: match a capability's ``PRODUCES`` output field to another capability's
  input binding by field name and emit a ``CONSUMES`` edge carrying
  ``binds=(out_key, in_key)`` so traversal can chain ops into qualified paths.

The embedder is the wiki :class:`~mewbo_graph.wiki.embedder.Embedder` (constructed
via ``make_embedder()`` by default; tests inject a fake). Embedding is
best-effort — a missing/failed embedding backend degrades to a structure-only
SCG, never a hard failure (mirrors the wiki's BM25-fallback stance).

Security invariant (spec §6): nodes carry only redacted descriptors; this class
copies no token/credential/data — it persists exactly what the providers emit.
"""

from __future__ import annotations

from typing import Protocol

from mewbo_core.common import get_logger

from mewbo_graph.wiki.embedder import make_embedder

from .entity_resolution import TypeAligner
from .providers.base import SourceStructureProvider, StructureProviderRegistry
from .store import ScgStore
from .types import (
    ScgEdge,
    ScgEmbedding,
    ScgNode,
    SourceDescriptor,
    SourceKey,
    StructureGraph,
    field_leaf,
)

logging = get_logger(name="api.agentic_search.scg.parser")


class _NodeEmbedder(Protocol):
    """The single embedder method the parser needs (wiki ``Embedder`` satisfies it).

    A node embedder maps ``(node_id, text)`` pairs to records exposing
    ``node_id`` / ``vector`` / ``dim`` (the wiki :class:`Embedding`). ``model``
    is read off the embedder so the parser stays decoupled from the record type.
    """

    model: str

    def embed_nodes(
        self, items: list[tuple[str, str]], *, slug: str = ""
    ) -> list[_EmbeddingRow]:
        """Return one embedding record per ``(node_id, text)`` pair, in order."""
        ...


class _EmbeddingRow(Protocol):
    """The fields the parser reads off an embedder's returned record."""

    node_id: str
    vector: list[float]
    dim: int


class ScgParser:
    """Maps mapped sources into the persisted Source Capability Graph.

    Dependency-injected with the :class:`ScgStore` to persist into, the list of
    :class:`SourceStructureProvider`s to dispatch over (built into an internal
    :class:`StructureProviderRegistry`), a node embedder (the wiki
    :class:`Embedder` by default), and an optional :class:`TypeAligner` for the
    cross-source ``RESOLVES_TO`` pass. Holds no per-source state — every method
    operates over the injected store, so one parser instance maps a whole
    catalog.
    """

    def __init__(
        self,
        *,
        store: ScgStore,
        providers: list[SourceStructureProvider],
        embedder: _NodeEmbedder | None = None,
        aligner: TypeAligner | None = None,
    ) -> None:
        """Bind the store, providers (→ registry), embedder, and aligner."""
        self._store = store
        self._registry = StructureProviderRegistry(providers)
        self._embedder = embedder if embedder is not None else make_embedder()
        self._aligner = aligner

    # -- map one source -----------------------------------------------------

    def parse_source(self, descriptor: SourceDescriptor) -> StructureGraph:
        """Map one source into the SCG and return its parsed structure graph.

        Clean re-map: every prior node/edge/recipe/embedding for this source is
        deleted first, so re-indexing replaces rather than accumulates. The
        descriptor is persisted so later ``link_sources`` / re-maps can find it.
        """
        graph = self._registry.build(descriptor)

        self._store.delete_source(descriptor.source_id)
        self._store.upsert_nodes(graph.nodes)
        self._store.upsert_edges(graph.edges)
        self._store.upsert_recipes(graph.recipes)
        self._store.upsert_source(descriptor)
        self._embed_nodes(graph.nodes)
        return graph

    # -- cross-source RESOLVES_TO ------------------------------------------

    def link_sources(self, source_ids: list[str]) -> list[ScgEdge]:
        """Run the injected aligner across *source_ids*, persisting RESOLVES_TO.

        Returns the emitted edges (already upserted by the aligner). Without an
        aligner injected this is a deterministic no-op (``[]``), never a raise.
        """
        if self._aligner is None:
            return []
        return self._aligner.align(source_ids)

    # -- In-N-Out producer → consumer --------------------------------------

    def compute_param_edges(self) -> list[ScgEdge]:
        """Wire ``CONSUMES`` edges from producing ops to consuming ops by field.

        The In-N-Out join (``2509.01560``): a capability's ``PRODUCES`` output
        field (``<cap>.<name>``) matched to *another* capability's input binding
        of the same field *name* yields a ``CONSUMES`` edge
        ``producer → consumer`` carrying ``binds=(out_key, in_key)`` — the seam
        the router chains into qualified multi-hop paths. Deterministic;
        self-edges are skipped. Returns (and persists) the edges emitted.
        """
        produced = self._produced_fields()
        consumed = self._consumed_fields()
        edges: list[ScgEdge] = []
        seen: set[tuple[SourceKey, SourceKey, SourceKey, SourceKey]] = set()
        for name, producers in produced.items():
            for in_cap, in_key in consumed.get(name, []):
                for out_cap, out_key in producers:
                    if out_cap == in_cap:
                        continue  # an op never consumes its own output
                    dedup = (out_cap, in_cap, out_key, in_key)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    edges.append(
                        ScgEdge(
                            source=out_cap,
                            target=in_cap,
                            kind="CONSUMES",
                            binds=(out_key, in_key),
                            method="type_align",
                            evidence=[f"field_name={name}"],
                        )
                    )
        if edges:
            self._store.upsert_edges(edges)
        return edges

    # -- embedding ----------------------------------------------------------

    def _embed_nodes(self, nodes: list[ScgNode]) -> None:
        """Embed *nodes* and upsert one :class:`ScgEmbedding` each (best-effort).

        Failure is non-fatal: a missing/erroring embedding backend leaves a
        structure-only SCG (mirrors the wiki's BM25 fallback). The embed text
        blends the node name, doc, and example queries — the retrievable surface.
        """
        if not nodes:
            return
        items = [(n.node_id, self._embed_text(n)) for n in nodes]
        try:
            rows = self._embedder.embed_nodes(items)
        except Exception as exc:  # noqa: BLE001 — embedding is best-effort
            logging.warning("SCG node embedding skipped: %s", exc)
            return
        model = getattr(self._embedder, "model", "")
        self._store.upsert_embeddings(
            [
                ScgEmbedding(
                    node_id=row.node_id,
                    vector=list(row.vector),
                    model=model,
                    dim=row.dim,
                )
                for row in rows
            ]
        )

    @staticmethod
    def _embed_text(node: ScgNode) -> str:
        """The retrievable text for a node: name + doc + example queries."""
        parts = [node.name]
        if node.doc:
            parts.append(node.doc)
        parts.extend(node.example_queries)
        return "\n".join(parts)

    # -- field indexing (In-N-Out helpers) ---------------------------------

    def _produced_fields(self) -> dict[str, list[tuple[SourceKey, SourceKey]]]:
        """``field_name -> [(capability_key, produced_field_key)]`` from PRODUCES."""
        out: dict[str, list[tuple[SourceKey, SourceKey]]] = {}
        for edge in self._store.list_edges(kind="PRODUCES"):
            name = field_leaf(edge.target)
            out.setdefault(name, []).append((edge.source, edge.target))
        return out

    def _consumed_fields(self) -> dict[str, list[tuple[SourceKey, SourceKey]]]:
        """``field_name -> [(capability_key, input_field_key)]`` from bindings."""
        out: dict[str, list[tuple[SourceKey, SourceKey]]] = {}
        for cap in self._store.query_nodes(kind="capability"):
            for binding in cap.bindings:
                name = field_leaf(binding.field_key)
                out.setdefault(name, []).append((cap.source_key, binding.field_key))
        return out


__all__ = ["ScgParser"]
