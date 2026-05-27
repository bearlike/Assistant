"""ScgMemoryBridge — the learned-layer flywheel over the SCG.

The SCG *structure* (schemas + pathways) is search-owned; the *learned* layer is
**shared with #13's memory substrate** — there is ZERO re-implementation of the
atomic-note / anchor / dedup machinery here. This module is a thin seam that:

* lets the #13 :class:`~mewbo_graph.wiki.memory.InsightIngestor` resolve connector
  anchors against the SCG instead of the code graph
  (:class:`ScgAnchorResolver`), and
* deposits / retrieves connector insights under ``corpus="connector"``
  (:class:`ScgMemoryBridge`).

Why the resolver is correctness-critical: ``memory_vector_search`` defaults to
``exclude_invalidated=True``, which only returns notes that have a **live
``ANCHORS`` edge**. The ingestor creates that edge only for anchors its
``StructureProvider`` can resolve. The default ``CodeStructureProvider`` resolves
``file#Name`` code keys — it can never resolve a connector ``source_key``, so a
connector insight would be written but then silently dropped on read.
``ScgAnchorResolver`` resolves ``source_key`` → :class:`ScgNode`, so the edge is
created and the insight surfaces.

Retrieval goes straight through the store's ``memory_vector_search`` ANN seam
(NOT ``MultiplexExpander``): the expander's code-graph neighbour expansion
no-ops for connectors — they have no tree-sitter CALLS/IMPORTS edges to walk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mewbo_graph.wiki.memory import InsightIngestor
from mewbo_graph.wiki.memory_types import EntityKey, MemoryFilter, MemoryKind, MemoryNode

if TYPE_CHECKING:  # type-only imports keep this module import-light
    from mewbo_graph.wiki.memory import IngestResult
    from mewbo_graph.wiki.store import WikiStoreBase

    from .store import ScgStore
    from .types import ScgNode

# A stable, synthetic slug namespacing every connector insight. Connectors are
# not repositories, so they share one namespace rather than a per-repo slug.
CONNECTOR_SLUG = "__connector__"
# The corpus tag isolating connector insights from the code/docs corpora.
CONNECTOR_CORPUS = "connector"


class ScgAnchorResolver:
    """``StructureProvider`` backed by the SCG store (``source_key`` → node).

    Implements the #13 ``StructureProvider`` Protocol (``resolve`` /
    ``resolve_many`` / ``entity_key_of``) so the shared ``InsightIngestor`` can
    resolve connector ``source_key`` anchors instead of dropping them.
    Stateless beyond the injected store — a re-map mutates the
    graph, so caching would go stale (mirrors ``CodeStructureProvider``).

    The multiplex ``entity_key`` for a connector is simply its ``source_key``
    (``<source_id>#<Qualified.Name>``); SCG nodes already carry no byte offsets,
    so anchors survive a re-index.
    """

    def __init__(self, store: ScgStore) -> None:
        """Compose over an SCG store (dependency-injected)."""
        self._store = store

    def resolve(self, slug: str, entity_key: EntityKey) -> ScgNode | None:
        """Return the SCG node addressed by ``entity_key`` (a ``source_key``)."""
        return self._store.get_node(self._node_id_for(entity_key))

    def resolve_many(
        self, slug: str, entity_keys: list[EntityKey]
    ) -> dict[EntityKey, ScgNode]:
        """Resolve a batch by ``source_key``; misses are omitted from the result."""
        out: dict[EntityKey, ScgNode] = {}
        for key in entity_keys:
            if key in out:
                continue
            node = self.resolve(slug, key)
            if node is not None:
                out[key] = node
        return out

    def entity_key_of(self, slug: str, node_id: str) -> EntityKey | None:
        """Return the ``source_key`` (== ``entity_key``) for an SCG ``node_id``."""
        node = self._store.get_node(node_id)
        return node.source_key if node is not None else None

    @staticmethod
    def _node_id_for(source_key: EntityKey) -> str:
        """Derive the canonical SCG node id for an entity-type ``source_key``.

        Anchors point at ``entity_type`` nodes (the queryable surface a learned
        insight hangs off), matching ``ScgNode.make_id(source_key, "entity_type")``.
        """
        from .types import ScgNode as _ScgNode

        return _ScgNode.make_id(source_key, "entity_type")


class ScgMemoryBridge:
    """Deposit / retrieve connector insights over #13's memory substrate.

    The learned-layer flywheel for Agentic Search: data-location wins, failure
    constraints, resolved bindings and learned edge weights are written as
    atomic connector notes and read back to bias traversal. All atomic-note /
    dedup / anchor work is the shared ``InsightIngestor`` — this class only
    pins ``corpus="connector"`` and swaps in the SCG-backed anchor resolver.
    """

    def __init__(
        self,
        *,
        wiki_store: WikiStoreBase,
        embedder: object,
        llm: object | None = None,
    ) -> None:
        """Wire collaborators (all injected); ``llm`` is opt-in (dedup tier-3)."""
        self._store = wiki_store
        self._embedder = embedder
        self._llm = llm
        # The resolver is overridable so a caller can point it at a specific
        # SCG store; default constructs against the process-wide singleton lazily.
        self._resolver: ScgAnchorResolver | None = None

    @property
    def resolver(self) -> ScgAnchorResolver:
        """The anchor resolver; lazily bound to the process-wide SCG store."""
        if self._resolver is None:
            from .store import get_scg_store

            self._resolver = ScgAnchorResolver(get_scg_store())
        return self._resolver

    @resolver.setter
    def resolver(self, resolver: ScgAnchorResolver) -> None:
        """Override the anchor resolver (e.g. to target a specific SCG store)."""
        self._resolver = resolver

    def write_insight(
        self,
        slug: str,
        content: str,
        *,
        source_keys: list[str],
        kind: MemoryKind = "propositional",
        labels: list[str] | None = None,
    ) -> IngestResult:
        """Deposit one connector insight anchored to ``source_keys``.

        Routes through the shared ``InsightIngestor`` with ``corpus="connector"``
        and the SCG-backed anchor resolver, so resolvable anchors create the live
        ``ANCHORS`` edge that retrieval requires. The resolver is passed at
        construction (``provider=``) — connector source_keys resolve instead of
        being dropped, with no post-construction mutation of the ingestor.
        """
        ingestor = InsightIngestor.from_store(
            self._store,
            embedder=self._embedder,
            llm=self._llm,
            provider=self.resolver,
        )
        return ingestor.ingest(
            slug,
            content,
            anchors=list(source_keys),
            corpus=CONNECTOR_CORPUS,
            kind=kind,
            labels=labels,
        )

    def read_insights(
        self, slug: str, query_vec: list[float], *, k: int = 10
    ) -> list[MemoryNode]:
        """Return the top-``k`` connector insights for ``query_vec``.

        Reads through the store's ``memory_vector_search`` ANN seam filtered to
        ``corpus="connector"`` (NOT ``MultiplexExpander`` — its code-graph
        neighbour expansion no-ops for connectors). Embeddings are resolved back
        to their nodes in rank order; the corpus filter already excludes other
        corpora, so the node lookup never returns a non-connector note.
        """
        filt = MemoryFilter(corpus=CONNECTOR_CORPUS)
        out: list[MemoryNode] = []
        for emb in self._store.memory_vector_search(slug, query_vec, k, filt=filt):
            node = self._store.get_memory_node(slug, emb.node_id)
            if node is not None:
                out.append(node)
        return out


__all__ = [
    "CONNECTOR_SLUG",
    "CONNECTOR_CORPUS",
    "ScgAnchorResolver",
    "ScgMemoryBridge",
]
