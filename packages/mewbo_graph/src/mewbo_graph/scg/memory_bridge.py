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

from typing import TYPE_CHECKING, Literal

from mewbo_graph.wiki.embedder import Embedder
from mewbo_graph.wiki.memory import InsightIngestor
from mewbo_graph.wiki.memory_types import EntityKey, MemoryFilter, MemoryKind, MemoryNode

from .types import NodeKind, SourceKey

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

# ── Insight polarity (the memory-bias signal, #76) ──────────────────────────
#
# A connector insight is either *positive* evidence ("this pathway produced
# results") or a *dead end* ("this pathway returned nothing for that question").
# `docs/features-search.md`: routing biases "toward pathways that have produced
# results and away from dead ends already discovered." Polarity rides the
# existing `MemoryNode.labels` (a model-writable, round-tripping field) as a
# reserved `scg:<polarity>` label — NO new persisted field, NO schema change.
Polarity = Literal["positive", "dead_end"]
_POLARITY_PREFIX = "scg:"
# The default when a deposit records no explicit polarity — a plain reachability
# fact ("Issue is queryable by id") is positive evidence about a pathway.
_DEFAULT_POLARITY: Polarity = "positive"


def polarity_label(polarity: Polarity) -> str:
    """The reserved label encoding *polarity* (the one canonical mapping)."""
    return f"{_POLARITY_PREFIX}{polarity}"


def polarity_of(node: MemoryNode) -> Polarity:
    """Read a note's polarity off its labels; default ``positive`` if unlabelled.

    A dead-end label damps routing; anything else (including an untagged legacy
    note) reads as positive evidence — the conservative default, so an existing
    corpus keeps biasing toward known-good pathways without a backfill.
    """
    if polarity_label("dead_end") in node.labels:
        return "dead_end"
    return "positive"


# The SCG node kinds a learned connector insight may anchor to, in resolution
# priority. A ``source_key`` is content-addressed PER KIND (``node_id =
# sha1(source_key|kind)``), so a kind-agnostic resolve must probe each anchorable
# kind. ``capability`` is first because an MCP-tool-list source — the dominant
# connector shape — maps every tool to a ``capability`` node (no entity layer);
# ``entity_type`` covers the OpenAPI/GraphQL sources that expose a typed entity
# surface. ``field`` / ``source`` / ``route_recipe`` are never anchor targets (a
# note hangs off a queryable capability/entity, not a leaf field or the bare
# source), so they are deliberately excluded.
_ANCHORABLE_KINDS: tuple[NodeKind, ...] = ("capability", "entity_type")


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

    A ``source_key`` is resolved KIND-AGNOSTICALLY across ``_ANCHORABLE_KINDS``
    because ``node_id`` is content-addressed per ``(source_key, kind)``: an MCP
    tool-list source maps each tool to a ``capability`` node while an OpenAPI
    source exposes ``entity_type`` nodes, so a fixed-kind probe drops every
    anchor of the other shape — which is exactly the bug that left connector
    insights edge-less (and thus invisible to ``memory_vector_search``).
    """

    def __init__(self, store: ScgStore) -> None:
        """Compose over an SCG store (dependency-injected)."""
        self._store = store

    def resolve(self, slug: str, entity_key: EntityKey) -> ScgNode | None:
        """Return the SCG node addressed by ``entity_key`` (a ``source_key``).

        Probes each anchorable kind in priority order (``capability`` first, the
        MCP-tool-list shape; then ``entity_type``, the OpenAPI shape) and returns
        the first live node — so a connector ``source_key`` resolves regardless
        of which structure the source's provider emitted.
        """
        return self._lookup(entity_key)

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

    def _lookup(self, source_key: EntityKey) -> ScgNode | None:
        """Return the live SCG node for ``source_key`` across anchorable kinds.

        ``node_id`` is content-addressed per ``(source_key, kind)``, so this
        probes ``_ANCHORABLE_KINDS`` in priority order and returns the first
        node that exists — a ``capability`` (MCP tool list) OR ``entity_type``
        (OpenAPI) source resolves identically. ``None`` only when no anchorable
        node carries this ``source_key`` (a genuinely unresolvable anchor).
        """
        from .types import ScgNode as _ScgNode

        for kind in _ANCHORABLE_KINDS:
            node = self._store.get_node(_ScgNode.make_id(source_key, kind))
            if node is not None:
                return node
        return None


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
        polarity: Polarity = _DEFAULT_POLARITY,
        workspace: str | None = None,
    ) -> IngestResult:
        """Deposit one connector insight anchored to ``source_keys``.

        Routes through the shared ``InsightIngestor`` with ``corpus="connector"``
        and the SCG-backed anchor resolver, so resolvable anchors create the live
        ``ANCHORS`` edge that retrieval requires. The resolver is passed at
        construction (``provider=``) — connector source_keys resolve instead of
        being dropped, with no post-construction mutation of the ingestor.

        ``polarity`` records whether the fact is positive evidence or a dead end;
        it rides a reserved ``scg:<polarity>`` label so memory-aware routing can
        boost / damp the anchored capability (#76). ``workspace`` (if given —
        ambient from :class:`ScgScope` at the call site) rides a reserved
        ``ws:<id>`` label for **attribution only**, NEVER a partition: the shared
        graph cross-pollinates, so a cross-workspace read still surfaces the note.
        """
        tags = list(labels or [])
        tags.append(polarity_label(polarity))
        if workspace:
            tags.append(f"ws:{workspace}")
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
            labels=tags,
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

    def read_anchored_insights(
        self, slug: str, query_vec: list[float], *, k: int = 10
    ) -> list[tuple[MemoryNode, float, list[SourceKey]]]:
        """Top-``k`` connector insights, each with its cosine score + live anchors.

        The retrieval surface memory-aware routing consumes (#76): a note is
        useless to routing without knowing WHICH capability ``source_key`` it
        hangs off, so this returns ``(note, score, anchored_source_keys)`` in one
        pass — the live ``ANCHORS`` edge targets per note are read off the store's
        edge index (``list_memory_edges`` already excludes invalidated edges).
        The score is the same brute-force cosine the router uses, so the bias is
        commensurate with the seed similarity it blends into.
        """
        filt = MemoryFilter(corpus=CONNECTOR_CORPUS)
        out: list[tuple[MemoryNode, float, list[SourceKey]]] = []
        for emb in self._store.memory_vector_search(slug, query_vec, k, filt=filt):
            node = self._store.get_memory_node(slug, emb.node_id)
            if node is None:
                continue
            anchors = [
                e.target
                for e in self._store.list_memory_edges(slug, node_id=node.node_id)
                if e.type == "ANCHORS"
            ]
            out.append((node, Embedder.cosine(query_vec, emb.vector), anchors))
        return out


__all__ = [
    "CONNECTOR_SLUG",
    "CONNECTOR_CORPUS",
    "Polarity",
    "polarity_label",
    "polarity_of",
    "ScgAnchorResolver",
    "ScgMemoryBridge",
]
