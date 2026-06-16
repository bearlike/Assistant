"""Persistence for the Source Capability Graph (SCG) — JSON or MongoDB.

The SCG *structure* store is **search-owned and deliberately SEPARATE from the
run store** (`agentic_search_runs`): a re-map of a source rewrites graph nodes
without touching any in-flight run. It mirrors the project/wiki/run dual-backend
pattern: an abstract base + a filesystem driver + a Mongo driver + a
config-driven factory + a process-wide singleton.

Five entity families, each in its own storage namespace:

* **nodes** — :class:`ScgNode`, keyed on the derived ``node_id``.
* **edges** — :class:`ScgEdge`, keyed on the ``(source, target, kind)`` triple.
* **recipes** — :class:`RouteRecipe`, keyed on ``source_key``.
* **embeddings** — :class:`ScgEmbedding`, keyed on ``node_id``.
* **sources** — :class:`SourceDescriptor`, keyed on ``source_id``.

JSON layout under ``<cache_dir>/agentic_search/scg/`` — one file per collection
holding a ``{key: doc}`` map (small graphs; whole-file rewrite under a lock)::

    nodes.json
    edges.json
    recipes.json
    embeddings.json
    sources.json

Mongo collections: ``agentic_search_scg_nodes``, ``agentic_search_scg_edges``,
``agentic_search_scg_recipes``, ``agentic_search_scg_embeddings``,
``agentic_search_scg_sources``.

Per-source mappings are GLOBAL and content-addressed (``node_id =
sha1(source_key|kind)[:16]``) — the SCG is "a tenant of the same three-layer
multiplex graph that powers the Agentic Wiki" and the layers cross-pollinate
without explicit wiring (``docs/features-search.md``). #75 therefore does NOT
hard-partition this store by workspace; a workspace is a **scoped VIEW** over the
shared graph — see :mod:`mewbo_graph.scg.scope` (the source-id allowlist
:class:`ScgRouter` honours at query time) — so a re-map in one workspace stays a
cheap idempotent upsert that *every* workspace mapping that source benefits from.

Security invariant (spec §6): SCG nodes carry only a *redacted* ``auth_scope``
descriptor — this store never sees or persists a token/credential.
"""

from __future__ import annotations

import abc
import json
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value

from mewbo_graph.wiki.embedder import Embedder

from .types import (
    RouteRecipe,
    ScgEdge,
    ScgEmbedding,
    ScgNode,
    SourceDescriptor,
    SourceKey,
)

if TYPE_CHECKING:
    # Real pymongo types for the Mongo seam — type-only so a runtime without
    # pymongo (the JSON default) never imports it. Keeps the driver precisely
    # typed without ``Any``.
    from pymongo import MongoClient
    from pymongo.collection import Collection

    _MongoDoc = dict[str, object]
    _Collection = Collection[_MongoDoc]
    _MongoClient = MongoClient[_MongoDoc]

logging = get_logger(name="api.agentic_search.scg.store")

# A predicate over one stored JSON doc — used by the scoped-delete helper.
_DocPredicate = Callable[[dict[str, object]], bool]


def _edge_key(edge: ScgEdge) -> str:
    """The natural upsert key for an edge — the ``(source, target, kind)`` triple."""
    return f"{edge.source}\x1f{edge.target}\x1f{edge.kind}"


# A query_nodes filter triple — the cache key.
_NodeKey = tuple[str | None, str | None, str | None]


class _NodeQueryCache:
    """Process-local memo of ``query_nodes`` results, keyed by the filter triple.

    Both ``GET /sources`` (a per-source capability lookup per configured server)
    and ``GET /workspaces/<id>/graph`` (a ``query_nodes(source_id=…)`` per scoped
    source) re-scan the node collection once per source on every request; on the
    JSON backend each scan reloads and re-validates the whole ``nodes.json``.
    Memoizing by ``(source_id, kind, name_contains)`` turns those repeat reads
    into O(1) hits.

    Correctness: every node write (:meth:`ScgStore.upsert_nodes` /
    :meth:`ScgStore.delete_source`) calls :meth:`clear`, so a same-process read
    never observes a stale graph. A short TTL bounds cross-worker staleness on
    the Mongo backend — another worker's map job can't reach our :meth:`clear`,
    so its writes self-heal within :data:`_TTL_S`, the same eventual-consistency
    contract the console's 60s ``staleTime`` already assumes. Returned lists are
    copied so a caller mutating the result never corrupts the cached entry.
    """

    _TTL_S = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[_NodeKey, tuple[float, list[ScgNode]]] = {}

    def get(self, key: _NodeKey) -> list[ScgNode] | None:
        """Return a cached (copied) result for *key*, or None on miss/expiry."""
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            stamped, nodes = hit
            if time.monotonic() - stamped > self._TTL_S:
                del self._entries[key]
                return None
            return list(nodes)

    def put(self, key: _NodeKey, nodes: list[ScgNode]) -> None:
        """Memoize *nodes* (copied) for *key* with a fresh timestamp."""
        with self._lock:
            self._entries[key] = (time.monotonic(), list(nodes))

    def clear(self) -> None:
        """Drop every memoized result (called on any node-collection write)."""
        with self._lock:
            self._entries.clear()


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ScgStore(abc.ABC):
    """Abstract base for SCG structure-persistence backends."""

    def __init__(self) -> None:
        """Initialise the shared node-query cache (drivers must ``super().__init__``)."""
        self._node_cache = _NodeQueryCache()

    def _invalidate_nodes(self) -> None:
        """Drop cached ``query_nodes`` results after a node-collection write."""
        self._node_cache.clear()

    # -- Writes -------------------------------------------------------------

    @abc.abstractmethod
    def upsert_nodes(self, nodes: list[ScgNode]) -> None:
        """Upsert nodes, keyed on ``node_id``."""

    @abc.abstractmethod
    def upsert_edges(self, edges: list[ScgEdge]) -> None:
        """Upsert edges, keyed on the ``(source, target, kind)`` triple."""

    @abc.abstractmethod
    def upsert_recipes(self, recipes: list[RouteRecipe]) -> None:
        """Upsert route recipes, keyed on ``source_key``."""

    @abc.abstractmethod
    def upsert_embeddings(self, embeddings: list[ScgEmbedding]) -> None:
        """Upsert embeddings, keyed on ``node_id``."""

    @abc.abstractmethod
    def upsert_source(self, descriptor: SourceDescriptor) -> None:
        """Upsert a source descriptor, keyed on ``source_id``."""

    # -- Reads --------------------------------------------------------------

    @abc.abstractmethod
    def get_node(self, node_id: str) -> ScgNode | None:
        """Return one node by id, or None if absent."""

    def query_nodes(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        name_contains: str | None = None,
    ) -> list[ScgNode]:
        """Return nodes matching every supplied filter (AND-composed).

        Memoized by the filter triple (see :class:`_NodeQueryCache`); node writes
        invalidate the cache. Backends implement the raw scan in
        :meth:`_query_nodes_uncached`.
        """
        key = (source_id, kind, name_contains)
        cached = self._node_cache.get(key)
        if cached is not None:
            return cached
        nodes = self._query_nodes_uncached(
            source_id=source_id, kind=kind, name_contains=name_contains
        )
        self._node_cache.put(key, nodes)
        return nodes

    @abc.abstractmethod
    def _query_nodes_uncached(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        name_contains: str | None = None,
    ) -> list[ScgNode]:
        """Scan the node collection for the filter, with no caching."""

    @abc.abstractmethod
    def list_edges(
        self, *, source: SourceKey | None = None, kind: str | None = None
    ) -> list[ScgEdge]:
        """Return edges matching every supplied filter (AND-composed)."""

    @abc.abstractmethod
    def neighbors(self, source_key: SourceKey) -> list[ScgEdge]:
        """Return the outgoing edges whose ``source`` is *source_key*."""

    @abc.abstractmethod
    def list_recipes(self, *, source_id: str | None = None) -> list[RouteRecipe]:
        """Return route recipes, optionally scoped to one *source_id*."""

    @abc.abstractmethod
    def list_embeddings(self) -> list[ScgEmbedding]:
        """Return all embeddings."""

    @abc.abstractmethod
    def list_sources(self) -> list[SourceDescriptor]:
        """Return all source descriptors."""

    # -- Vector search + scoped delete -------------------------------------

    def vector_search(self, qvec: list[float], k: int) -> list[tuple[str, float]]:
        """Return ``(node_id, cosine_score)`` for the top-*k* embeddings.

        Brute-force cosine over every stored vector — the documented scale seam
        (mirrors the wiki ``vector_search``): an ANN index lands behind this
        signature without changing callers.
        """
        embeddings = self.list_embeddings()
        if not embeddings:
            return []
        scored = [
            (e.node_id, Embedder.cosine(qvec, e.vector)) for e in embeddings
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    @abc.abstractmethod
    def delete_source(self, source_id: str) -> int:
        """Delete every node/edge/recipe/embedding/source for *source_id*.

        Scoped wipe for a clean re-map; returns the total document count removed.
        """


# ---------------------------------------------------------------------------
# JSON / filesystem driver
# ---------------------------------------------------------------------------


class JsonScgStore(ScgStore):
    """Filesystem-backed SCG store under ``<cache_dir>/agentic_search/scg/``.

    Each collection is one JSON file holding a ``{natural_key: doc}`` map. All
    mutations take ``_lock`` and rewrite the whole file — SCGs are small enough
    that this is simpler and safer than partial writes (single-instance use;
    the Mongo driver is the multi-worker path).
    """

    _NODES = "nodes"
    _EDGES = "edges"
    _RECIPES = "recipes"
    _EMBEDDINGS = "embeddings"
    _SOURCES = "sources"

    def __init__(self, root_dir: str | Path | None = None) -> None:
        """Initialise + create the directory tree."""
        super().__init__()
        if root_dir is None:
            home = get_config_value("runtime", "cache_dir", default="") or ".mewbo"
            root_dir = Path(home) / "agentic_search" / "scg"
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    def _path(self, collection: str) -> Path:
        return self.root_dir / f"{collection}.json"

    def _load(self, collection: str) -> dict[str, dict[str, object]]:
        path = self._path(collection)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logging.warning("Skipping malformed SCG collection at %s", path)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, collection: str, data: dict[str, dict[str, object]]) -> None:
        self._path(collection).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def _upsert(
        self, collection: str, items: list[tuple[str, dict[str, object]]]
    ) -> None:
        if not items:
            return
        with self._lock:
            data = self._load(collection)
            for key, doc in items:
                data[key] = doc
            self._save(collection, data)

    # -- Writes -------------------------------------------------------------

    def upsert_nodes(self, nodes: list[ScgNode]) -> None:
        """Upsert nodes, keyed on ``node_id``."""
        self._upsert(
            self._NODES, [(n.node_id, n.model_dump(mode="json")) for n in nodes]
        )
        self._invalidate_nodes()

    def upsert_edges(self, edges: list[ScgEdge]) -> None:
        """Upsert edges, keyed on the ``(source, target, kind)`` triple."""
        self._upsert(
            self._EDGES, [(_edge_key(e), e.model_dump(mode="json")) for e in edges]
        )

    def upsert_recipes(self, recipes: list[RouteRecipe]) -> None:
        """Upsert route recipes, keyed on ``source_key``."""
        self._upsert(
            self._RECIPES,
            [(r.source_key, r.model_dump(mode="json")) for r in recipes],
        )

    def upsert_embeddings(self, embeddings: list[ScgEmbedding]) -> None:
        """Upsert embeddings, keyed on ``node_id``."""
        self._upsert(
            self._EMBEDDINGS,
            [(e.node_id, e.model_dump(mode="json")) for e in embeddings],
        )

    def upsert_source(self, descriptor: SourceDescriptor) -> None:
        """Upsert a source descriptor, keyed on ``source_id``."""
        self._upsert(
            self._SOURCES,
            [(descriptor.source_id, descriptor.model_dump(mode="json"))],
        )

    # -- Reads --------------------------------------------------------------

    def get_node(self, node_id: str) -> ScgNode | None:
        """Return one node by id, or None if absent."""
        with self._lock:
            doc = self._load(self._NODES).get(node_id)
        return ScgNode.model_validate(doc) if doc is not None else None

    def _query_nodes_uncached(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        name_contains: str | None = None,
    ) -> list[ScgNode]:
        """Scan ``nodes.json`` for the filter (AND-composed); no caching."""
        with self._lock:
            docs = list(self._load(self._NODES).values())
        needle = name_contains.lower() if name_contains else None
        out: list[ScgNode] = []
        for doc in docs:
            node = ScgNode.model_validate(doc)
            if source_id is not None and node.source_id != source_id:
                continue
            if kind is not None and node.kind != kind:
                continue
            if needle is not None and needle not in node.name.lower():
                continue
            out.append(node)
        return out

    def list_edges(
        self, *, source: SourceKey | None = None, kind: str | None = None
    ) -> list[ScgEdge]:
        """Return edges matching every supplied filter (AND-composed)."""
        with self._lock:
            docs = list(self._load(self._EDGES).values())
        out: list[ScgEdge] = []
        for doc in docs:
            edge = ScgEdge.model_validate(doc)
            if source is not None and edge.source != source:
                continue
            if kind is not None and edge.kind != kind:
                continue
            out.append(edge)
        return out

    def neighbors(self, source_key: SourceKey) -> list[ScgEdge]:
        """Return the outgoing edges whose ``source`` is *source_key*."""
        return self.list_edges(source=source_key)

    def list_recipes(self, *, source_id: str | None = None) -> list[RouteRecipe]:
        """Return route recipes, optionally scoped to one *source_id*."""
        with self._lock:
            docs = list(self._load(self._RECIPES).values())
        prefix = f"{source_id}#" if source_id is not None else None
        out: list[RouteRecipe] = []
        for doc in docs:
            recipe = RouteRecipe.model_validate(doc)
            if prefix is not None and not recipe.source_key.startswith(prefix):
                continue
            out.append(recipe)
        return out

    def list_embeddings(self) -> list[ScgEmbedding]:
        """Return all embeddings."""
        with self._lock:
            docs = list(self._load(self._EMBEDDINGS).values())
        return [ScgEmbedding.model_validate(d) for d in docs]

    def list_sources(self) -> list[SourceDescriptor]:
        """Return all source descriptors."""
        with self._lock:
            docs = list(self._load(self._SOURCES).values())
        return [SourceDescriptor.model_validate(d) for d in docs]

    # -- Scoped delete ------------------------------------------------------

    def delete_source(self, source_id: str) -> int:
        """Delete every entity for *source_id*; return the count removed.

        NOTE — non-atomic: the scoped delete is a sequence of per-collection file
        rewrites under one lock, so a mid-sequence crash can leave dangling edges
        pointing at an already-removed node. Acceptable for the single-instance
        dev (JSON) path; the multi-worker path is the Mongo backend. No
        transaction is layered on here (YAGNI for JSON v1).
        """
        prefix = f"{source_id}#"
        removed = 0
        with self._lock:
            # Nodes whose node_id we'll need to evict from embeddings too.
            nodes = self._load(self._NODES)
            evicted_ids = {
                nid
                for nid, doc in nodes.items()
                if doc.get("source_id") == source_id
            }
            kept_nodes = {
                nid: doc for nid, doc in nodes.items() if nid not in evicted_ids
            }
            removed += len(nodes) - len(kept_nodes)
            self._save(self._NODES, kept_nodes)

            removed += self._delete_where(
                self._EDGES,
                lambda d: str(d.get("source", "")).startswith(prefix)
                or str(d.get("target", "")).startswith(prefix),
            )
            removed += self._delete_where(
                self._RECIPES,
                lambda d: str(d.get("source_key", "")).startswith(prefix),
            )
            removed += self._delete_where(
                self._EMBEDDINGS,
                lambda d: d.get("node_id") in evicted_ids,
            )
            removed += self._delete_where(
                self._SOURCES,
                lambda d: d.get("source_id") == source_id,
            )
        self._invalidate_nodes()
        return removed

    def _delete_where(self, collection: str, predicate: _DocPredicate) -> int:
        """Drop docs matching *predicate* from *collection*; return count removed.

        Caller already holds ``_lock``.
        """
        data = self._load(collection)
        kept = {k: v for k, v in data.items() if not predicate(v)}
        removed = len(data) - len(kept)
        if removed:
            self._save(collection, kept)
        return removed


# ---------------------------------------------------------------------------
# MongoDB driver
# ---------------------------------------------------------------------------


class MongoScgStore(ScgStore):
    """MongoDB-backed SCG store.

    Collections (one per family): ``agentic_search_scg_nodes`` (``node_id`` PK),
    ``agentic_search_scg_edges`` (``(source, target, kind)`` PK),
    ``agentic_search_scg_recipes`` (``source_key`` PK),
    ``agentic_search_scg_embeddings`` (``node_id`` PK),
    ``agentic_search_scg_sources`` (``source_id`` PK).
    """

    NODES = "agentic_search_scg_nodes"
    EDGES = "agentic_search_scg_edges"
    RECIPES = "agentic_search_scg_recipes"
    EMBEDDINGS = "agentic_search_scg_embeddings"
    SOURCES = "agentic_search_scg_sources"

    def __init__(
        self,
        *,
        client: _MongoClient | None = None,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Connect + ensure unique indexes for each upsert key."""
        super().__init__()
        if client is None:
            from pymongo import MongoClient

            _uri = uri or get_config_value(
                "storage", "mongodb", "uri", default="mongodb://localhost:27017"
            )
            client = MongoClient(_uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
        if database is None:
            database = get_config_value(
                "storage", "mongodb", "database", default="mewbo"
            )
        self._client = client
        self._db = client[database]
        self._ensure_indexes()

    def _col(self, name: str) -> _Collection:
        return self._db[name]

    def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING

        self._col(self.NODES).create_index(
            [("node_id", ASCENDING)], name="ix_scg_node_id", unique=True, background=True
        )
        self._col(self.NODES).create_index(
            [("source_id", ASCENDING)], name="ix_scg_node_source", background=True
        )
        self._col(self.EDGES).create_index(
            [("source", ASCENDING), ("target", ASCENDING), ("kind", ASCENDING)],
            name="ix_scg_edge_triple",
            unique=True,
            background=True,
        )
        self._col(self.RECIPES).create_index(
            [("source_key", ASCENDING)],
            name="ix_scg_recipe_key",
            unique=True,
            background=True,
        )
        self._col(self.EMBEDDINGS).create_index(
            [("node_id", ASCENDING)],
            name="ix_scg_emb_node",
            unique=True,
            background=True,
        )
        self._col(self.SOURCES).create_index(
            [("source_id", ASCENDING)],
            name="ix_scg_source_id",
            unique=True,
            background=True,
        )

    # -- Writes -------------------------------------------------------------

    def upsert_nodes(self, nodes: list[ScgNode]) -> None:
        """Upsert nodes, keyed on ``node_id``."""
        for n in nodes:
            self._col(self.NODES).replace_one(
                {"node_id": n.node_id}, n.model_dump(mode="json"), upsert=True
            )
        self._invalidate_nodes()

    def upsert_edges(self, edges: list[ScgEdge]) -> None:
        """Upsert edges, keyed on the ``(source, target, kind)`` triple."""
        for e in edges:
            self._col(self.EDGES).replace_one(
                {"source": e.source, "target": e.target, "kind": e.kind},
                e.model_dump(mode="json"),
                upsert=True,
            )

    def upsert_recipes(self, recipes: list[RouteRecipe]) -> None:
        """Upsert route recipes, keyed on ``source_key``."""
        for r in recipes:
            self._col(self.RECIPES).replace_one(
                {"source_key": r.source_key}, r.model_dump(mode="json"), upsert=True
            )

    def upsert_embeddings(self, embeddings: list[ScgEmbedding]) -> None:
        """Upsert embeddings, keyed on ``node_id``."""
        for emb in embeddings:
            self._col(self.EMBEDDINGS).replace_one(
                {"node_id": emb.node_id}, emb.model_dump(mode="json"), upsert=True
            )

    def upsert_source(self, descriptor: SourceDescriptor) -> None:
        """Upsert a source descriptor, keyed on ``source_id``."""
        self._col(self.SOURCES).replace_one(
            {"source_id": descriptor.source_id},
            descriptor.model_dump(mode="json"),
            upsert=True,
        )

    # -- Reads --------------------------------------------------------------

    def get_node(self, node_id: str) -> ScgNode | None:
        """Return one node by id, or None if absent."""
        doc = self._col(self.NODES).find_one({"node_id": node_id}, {"_id": 0})
        return ScgNode.model_validate(doc) if doc else None

    def _query_nodes_uncached(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        name_contains: str | None = None,
    ) -> list[ScgNode]:
        """Query the nodes collection for the filter (AND-composed); no caching."""
        query: dict[str, object] = {}
        if source_id is not None:
            query["source_id"] = source_id
        if kind is not None:
            query["kind"] = kind
        if name_contains is not None:
            # Case-insensitive substring (literal — escape regex metachars).
            query["name"] = {"$regex": re.escape(name_contains), "$options": "i"}
        cursor = self._col(self.NODES).find(query, {"_id": 0})
        return [ScgNode.model_validate(d) for d in cursor]

    def list_edges(
        self, *, source: SourceKey | None = None, kind: str | None = None
    ) -> list[ScgEdge]:
        """Return edges matching every supplied filter (AND-composed)."""
        query: dict[str, object] = {}
        if source is not None:
            query["source"] = source
        if kind is not None:
            query["kind"] = kind
        cursor = self._col(self.EDGES).find(query, {"_id": 0})
        return [ScgEdge.model_validate(d) for d in cursor]

    def neighbors(self, source_key: SourceKey) -> list[ScgEdge]:
        """Return the outgoing edges whose ``source`` is *source_key*."""
        return self.list_edges(source=source_key)

    def list_recipes(self, *, source_id: str | None = None) -> list[RouteRecipe]:
        """Return route recipes, optionally scoped to one *source_id*."""
        query: dict[str, object] = {}
        if source_id is not None:
            query["source_key"] = {"$regex": f"^{re.escape(source_id)}#"}
        cursor = self._col(self.RECIPES).find(query, {"_id": 0})
        return [RouteRecipe.model_validate(d) for d in cursor]

    def list_embeddings(self) -> list[ScgEmbedding]:
        """Return all embeddings."""
        cursor = self._col(self.EMBEDDINGS).find({}, {"_id": 0})
        return [ScgEmbedding.model_validate(d) for d in cursor]

    def list_sources(self) -> list[SourceDescriptor]:
        """Return all source descriptors."""
        cursor = self._col(self.SOURCES).find({}, {"_id": 0})
        return [SourceDescriptor.model_validate(d) for d in cursor]

    # -- Scoped delete ------------------------------------------------------

    def delete_source(self, source_id: str) -> int:
        """Delete every entity for *source_id*; return the count removed."""
        prefix = f"{source_id}#"
        starts = {"$regex": f"^{re.escape(prefix)}"}
        evicted_ids = [
            d["node_id"]
            for d in self._col(self.NODES).find({"source_id": source_id}, {"node_id": 1})
        ]
        removed = 0
        removed += self._col(self.NODES).delete_many(
            {"source_id": source_id}
        ).deleted_count
        removed += self._col(self.EDGES).delete_many(
            {"$or": [{"source": starts}, {"target": starts}]}
        ).deleted_count
        removed += self._col(self.RECIPES).delete_many(
            {"source_key": starts}
        ).deleted_count
        if evicted_ids:
            removed += self._col(self.EMBEDDINGS).delete_many(
                {"node_id": {"$in": evicted_ids}}
            ).deleted_count
        removed += self._col(self.SOURCES).delete_many(
            {"source_id": source_id}
        ).deleted_count
        self._invalidate_nodes()
        return removed


# ---------------------------------------------------------------------------
# Factory + module singleton
# ---------------------------------------------------------------------------


def create_scg_store() -> ScgStore:
    """Return the configured SCG store driver (``storage.driver``; default JSON)."""
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        return MongoScgStore()
    return JsonScgStore()


_store_singleton: ScgStore | None = None
_singleton_lock = threading.Lock()


def get_scg_store() -> ScgStore:
    """Return the process-wide SCG store, creating it on first use."""
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is None:
            _store_singleton = create_scg_store()
        return _store_singleton


def set_scg_store(store: ScgStore | None) -> None:
    """Override the process-wide SCG store (used by tests)."""
    global _store_singleton
    with _singleton_lock:
        _store_singleton = store


def reset_for_tests() -> None:
    """Swap in a fresh, empty JSON store under a throwaway temp dir.

    Keeps unit tests isolated from real data while still exercising the JSON
    backend end-to-end (mirrors the run store's ``reset_for_tests``).
    """
    tmp = Path(tempfile.mkdtemp(prefix="mewbo-scg-"))
    set_scg_store(JsonScgStore(root_dir=tmp))


__all__ = [
    "ScgStore",
    "JsonScgStore",
    "MongoScgStore",
    "create_scg_store",
    "get_scg_store",
    "set_scg_store",
    "reset_for_tests",
]
