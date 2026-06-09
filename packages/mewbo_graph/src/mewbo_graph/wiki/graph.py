"""Wiki code-graph module.

Two atomic classes share the same domain (the code knowledge graph) at
different phases:

* ``GraphIndex`` runs at indexing time — tree-sitter-driven AST extraction
  yielding flat ``GraphNode`` + ``GraphEdge`` lists that the store persists.
* ``KnowledgeGraphView`` runs at view time — loads a slug's persisted nodes
  + edges, computes lightweight stats, and serialises a Cytoscape-friendly
  wire shape for the ``/v1/wiki/projects/<slug>/graph`` endpoint.

Keeping both in one module avoids splitting the same domain across files;
each class owns its own state and behaviour over that state.
"""
from __future__ import annotations

import hashlib
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from .types import GraphEdge, GraphNode

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_graph.entities.types import Entity, EntityRelation

    from .memory_types import MemoryEdge, MemoryNode
    from .store import WikiStoreBase

_log = get_logger(name="api.wiki.graph")


# extension → language map
_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
}

@dataclass(frozen=True)
class GraphParseResult:
    """Output of a single-file or repo parse."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    skipped: list[str]  # files whose extension isn't supported

    def __add__(self, other: GraphParseResult) -> GraphParseResult:
        """Merge two results by concatenating their node, edge, and skipped lists."""
        return GraphParseResult(
            nodes=self.nodes + other.nodes,
            edges=self.edges + other.edges,
            skipped=self.skipped + other.skipped,
        )


class GraphIndex:
    """AST-graph extractor.

    Constructed once per wiki indexing job. Caches loaded languages and
    compiled queries so per-file parse is a hot-loop friendly call.
    """

    def __init__(self) -> None:
        """Initialise caches and verify that the wiki extras are installed."""
        self._lang_cache: dict[str, object] = {}   # name → tree_sitter.Language
        self._query_cache: dict[str, object] = {}  # name → tree_sitter.Query
        self._queries_dir = Path(__file__).parent / "graph_queries"
        # Defensive import so missing extras give a clean error.
        try:
            import ctypes  # noqa: F401

            import tree_sitter  # noqa: F401
            import tree_sitter_language_pack  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "GraphIndex requires the 'wiki' extras: install with "
                "`uv sync --extra wiki` or `pip install mewbo-api[wiki]`."
            ) from exc

    def parse_file(
        self, slug: str, file_path: Path, *, repo_root: Path
    ) -> GraphParseResult:
        """Parse a single file. Returns empty result for unsupported extensions."""
        ext = file_path.suffix.lower()
        lang_name = _LANG_BY_EXT.get(ext)
        if lang_name is None:
            return GraphParseResult(
                nodes=[],
                edges=[],
                skipped=[str(file_path.relative_to(repo_root))],
            )

        from tree_sitter import Parser

        source = file_path.read_bytes()
        lang = self._lang_for(lang_name)
        tree = Parser(lang).parse(source)
        query = self._query_for(lang_name, lang)
        from tree_sitter import QueryCursor

        cursor = QueryCursor(query)
        captures: dict[str, list] = cursor.captures(tree.root_node)

        rel = str(file_path.relative_to(repo_root))
        return _extract(slug, rel, source, captures)

    def parse_repo(
        self,
        slug: str,
        repo_root: Path,
        files: list[Path],
    ) -> GraphParseResult:
        """Parse every file. Files outside the supported set go to ``skipped``."""
        result = GraphParseResult(nodes=[], edges=[], skipped=[])
        for fp in files:
            result += self.parse_file(slug, fp, repo_root=repo_root)
        return result

    # ── helpers ───────────────────────────────────────────────────────────────

    def _lang_for(self, lang_name: str) -> Any:
        """Return a cached ``tree_sitter.Language`` for *lang_name*."""
        if lang_name not in self._lang_cache:
            self._lang_cache[lang_name] = _load_ts_language(lang_name)
        return self._lang_cache[lang_name]

    def _query_for(self, lang_name: str, lang: Any) -> Any:
        """Return a cached compiled ``tree_sitter.Query`` for *lang_name*."""
        if lang_name not in self._query_cache:
            from tree_sitter import Query

            scm = (self._queries_dir / f"{lang_name}.scm").read_text(encoding="utf-8")
            self._query_cache[lang_name] = Query(lang, scm)
        return self._query_cache[lang_name]


# Parser shared libraries are fetched on demand from a GitHub release the first
# time a language is seen on a cold cache. GitHub's release-asset CDN
# occasionally returns a transient 5xx (a 504 was observed in production), and a
# single un-retried download abort would fail an entire multi-minute indexing
# run. Bounded exponential backoff turns that blip into a short wait; a genuine
# repeated failure still surfaces (the last exception is re-raised). The
# deployed image also pre-warms this cache at build time (docker/Dockerfile.api)
# so a healthy container never reaches the network here at all.
_PARSER_DOWNLOAD_ATTEMPTS = 3
_PARSER_DOWNLOAD_BASE_DELAY = 2.0  # seconds (first backoff)
_PARSER_DOWNLOAD_MAX_DELAY = 30.0  # seconds (backoff cap)


def _download_with_retry(
    download: Callable[[list[str]], object],
    lang_name: str,
    *,
    attempts: int = _PARSER_DOWNLOAD_ATTEMPTS,
    base_delay: float = _PARSER_DOWNLOAD_BASE_DELAY,
    max_delay: float = _PARSER_DOWNLOAD_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Download *lang_name*'s parser, retrying transient failures with backoff.

    Calls ``download([lang_name])`` up to *attempts* times, sleeping a capped
    exponential delay between tries. Re-raises the final exception if every
    attempt fails so a real (non-transient) error is never swallowed. The
    download surface raises version-specific opaque network/IO exceptions, so we
    retry on any exception rather than match fragile, version-coupled classes.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            download([lang_name])
            return
        except Exception as exc:  # noqa: BLE001 — opaque network/IO across lib versions
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            _log.warning(
                "tree-sitter parser download for {} failed "
                "(attempt {}/{}): {} — retrying in {:.1f}s",
                lang_name,
                attempt,
                attempts,
                exc,
                delay,
            )
            sleep(delay)
    assert last_exc is not None  # the loop only exits here after a failure
    raise last_exc


def _load_ts_language(lang_name: str):
    """Load a ``tree_sitter.Language`` via the language-pack cache directory.

    ``tree_sitter_language_pack`` stores per-language shared libraries in its
    cache directory (``~/.cache/tree-sitter-language-pack/<version>/libs/``).
    We load the relevant ``.so`` via ``ctypes`` and hand the C function pointer
    to ``tree_sitter.Language``.
    """
    import ctypes

    import tree_sitter_language_pack as tlp
    from tree_sitter import Language

    cache = Path(tlp.cache_dir())
    func_name = f"tree_sitter_{lang_name}"
    lib_path = cache / f"libtree_sitter_{lang_name}.so"

    # Ensure the library is downloaded before trying to load it. Resilient to a
    # transient upstream 5xx so a network blip doesn't abort the indexing run.
    if not lib_path.exists():
        _log.info("Downloading tree-sitter language library for {}", lang_name)
        _download_with_retry(tlp.download, lang_name)

    lib = ctypes.cdll.LoadLibrary(str(lib_path))
    fn = getattr(lib, func_name)
    fn.restype = ctypes.c_void_p
    ptr = fn()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return Language(ptr)


def _stable_id(slug: str, kind: str, name: str, file: str, byte_start: int) -> str:
    """Deterministic node id over (slug, kind, name, file, byte_start)."""
    h = hashlib.sha1(
        f"{slug}|{kind}|{name}|{file}|{byte_start}".encode()
    ).hexdigest()
    return h[:16]


def _extract(
    slug: str,
    rel_path: str,
    source: bytes,
    captures: dict[str, list],
) -> GraphParseResult:
    """Translate tree-sitter captures into GraphNode + GraphEdge.

    ``captures`` is a dict of ``{capture_name: [Node, ...]}`` as returned by
    ``QueryCursor.captures()`` in tree-sitter 0.25+. Nodes within each list
    are in document (byte) order, which means zipping parallel capture names
    (e.g. ``class.def`` with ``class.name``) is safe for non-overlapping
    patterns that produce exactly one sibling capture per match.
    """
    # File node — always emitted; contains all nodes inside the file.
    file_node = GraphNode(
        slug=slug,
        node_id=_stable_id(slug, "File", rel_path, rel_path, 0),
        type="File",
        name=rel_path,
        file=rel_path,
        range=(0, len(source)),
        docstring=None,
    )
    nodes: list[GraphNode] = [file_node]
    edges: list[GraphEdge] = []

    # Classes
    cls_defs = captures.get("class.def", [])
    cls_names = captures.get("class.name", [])
    for cls_def_node, cls_name_node in zip(cls_defs, cls_names):
        name = cls_name_node.text.decode()
        nid = _stable_id(slug, "Class", name, rel_path, cls_def_node.start_byte)
        nodes.append(
            GraphNode(
                slug=slug,
                node_id=nid,
                type="Class",
                name=name,
                file=rel_path,
                range=(cls_def_node.start_byte, cls_def_node.end_byte),
                docstring=_extract_docstring(cls_def_node),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Interfaces (TypeScript, Go, Rust — trait/interface → Interface node)
    for if_def_node, if_name_node in zip(
        captures.get("interface.def", []), captures.get("interface.name", [])
    ):
        name = if_name_node.text.decode()
        nid = _stable_id(slug, "Interface", name, rel_path, if_def_node.start_byte)
        nodes.append(
            GraphNode(
                slug=slug,
                node_id=nid,
                type="Interface",
                name=name,
                file=rel_path,
                range=(if_def_node.start_byte, if_def_node.end_byte),
                docstring=None,
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Top-level functions
    fn_defs = captures.get("function.def", [])
    fn_names = captures.get("function.name", [])
    for fn_def_node, fn_name_node in zip(fn_defs, fn_names):
        name = fn_name_node.text.decode()
        nid = _stable_id(slug, "Function", name, rel_path, fn_def_node.start_byte)
        nodes.append(
            GraphNode(
                slug=slug,
                node_id=nid,
                type="Function",
                name=name,
                file=rel_path,
                range=(fn_def_node.start_byte, fn_def_node.end_byte),
                docstring=_extract_docstring(fn_def_node),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Methods
    m_defs = captures.get("method.def", [])
    m_names = captures.get("method.name", [])
    for m_def_node, m_name_node in zip(m_defs, m_names):
        name = m_name_node.text.decode()
        nid = _stable_id(slug, "Method", name, rel_path, m_def_node.start_byte)
        nodes.append(
            GraphNode(
                slug=slug,
                node_id=nid,
                type="Method",
                name=name,
                file=rel_path,
                range=(m_def_node.start_byte, m_def_node.end_byte),
                docstring=_extract_docstring(m_def_node),
            )
        )
        # Method CONTAINS edge: the class that contains this method.
        # We attach it to the file node as a CONTAINS edge (class→method scoping
        # ships in Task 3.2 with cross-language support).
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Cross-file edges (IMPORTS/CALLS/EXTENDS) target a symbol *by name*. Their
    # real node_id is unknown at single-file parse time (the definition may live
    # in another file parsed later), so we keep a synthetic ``<external>`` target
    # id AND carry the raw ``target_name``. ``KnowledgeGraphView.for_slug`` then
    # resolves each name against the whole-repo node set: a hit re-points the
    # edge at the real node (connecting File-clusters through shared symbols), a
    # miss converges on one named ``External`` view-node. Keeping the synthetic
    # id here means the persisted ``wiki_graph_nodes`` table stays real-in-repo
    # symbols only.

    # Imports — IMPORTS edges from file to the imported module name.
    import_nodes = captures.get("import.module", []) + captures.get(
        "import.from_module", []
    )
    for mod_node in import_nodes:
        target_name = mod_node.text.decode()
        edges.append(
            GraphEdge(
                slug=slug,
                source=file_node.node_id,
                target=_stable_id(slug, "Module", target_name, "<external>", 0),
                type="IMPORTS",
                target_name=target_name,
            )
        )

    # Calls — CALLS edges from the file to the called name. Exact scope
    # resolution is out of v1's scope.
    for call_name_node in captures.get("call.name", []):
        callee = call_name_node.text.decode()
        edges.append(
            GraphEdge(
                slug=slug,
                source=file_node.node_id,
                target=_stable_id(slug, "Function", callee, "<external>", 0),
                type="CALLS",
                target_name=callee,
            )
        )

    # EXTENDS edges from subclass → superclass.
    subs = captures.get("subclass.name", [])
    sups = captures.get("superclass.name", [])
    for sub_node, sup_node in zip(subs, sups):
        sub_name = sub_node.text.decode()
        sup_name = sup_node.text.decode()
        sub_id = _stable_id(slug, "Class", sub_name, rel_path, sub_node.start_byte)
        edges.append(
            GraphEdge(
                slug=slug,
                source=sub_id,
                target=_stable_id(slug, "Class", sup_name, "<external>", 0),
                type="EXTENDS",
                target_name=sup_name,
            )
        )

    return GraphParseResult(nodes=nodes, edges=edges, skipped=[])


_MEMORY_LABEL_CHARS = 60
_MEMORY_SNIPPET_CHARS = 120


@dataclass(frozen=True, slots=True)
class KnowledgeGraphView:
    """Slug-scoped projection of the persisted MULTIPLEX graph for the viewer.

    Three node layers share one viewer payload: the tree-sitter ``ast`` layer
    (File/Class/Function/… + synthesized ``External`` convergence nodes), the
    abstract ``entity`` layer, and the atomic-note ``memory`` layer. Edges carry
    a ``layer`` tag — ``ast`` (CONTAINS/IMPORTS/CALLS/EXTENDS/REFERENCES),
    ``entity`` (entity↔entity RELATES, open-vocab verb in ``label``), ``memory``
    (note RELATES) and ``cross`` (ANCHORS spanning layers).

    Construction is exclusively via ``for_slug`` so the invariant — every node +
    edge belongs to the same slug, and every emitted edge endpoint is a real
    node in the payload — stays enforced in one place. Once built, the instance
    is immutable; safe to share across requests and trivially cacheable upstream.
    """

    slug: str
    # AST layer (real in-repo nodes only) + synthesized External convergence
    # nodes (one per distinct unresolved cross-file symbol name).
    nodes: tuple[GraphNode, ...]
    external_nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]  # ast-layer edges (endpoints all in payload)
    # Entity layer.
    entity_nodes: tuple[Entity, ...]
    entity_edges: tuple[EntityRelation, ...]  # entity↔entity RELATES only
    # Memory layer.
    memory_nodes: tuple[MemoryNode, ...]
    memory_edges: tuple[MemoryEdge, ...]  # note↔note RELATES only
    # Cross-layer ANCHORS, pre-resolved to ``(source_node_id, target_node_id)``.
    cross_edges: tuple[tuple[str, str], ...]
    total_nodes: int  # full AST node count (pre-cap), for the "showing N of M" banner
    total_edges: int  # full AST edge count (pre-cap)

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def for_slug(
        cls,
        store: WikiStoreBase,
        slug: str,
        *,
        node_limit: int | None = None,
    ) -> KnowledgeGraphView:
        """Load the full multiplex (ast + entity + memory layers) for *slug*.

        AST connectivity: a cross-file IMPORTS/CALLS/EXTENDS edge carries the
        raw ``target_name``; if that name resolves to a real in-repo node it is
        re-pointed there (genuinely connecting File-clusters through shared
        symbols), otherwise every reference to the same external name converges
        on ONE synthesized ``External`` view-node.

        ``node_limit`` (when set and exceeded) degree-prunes the **AST layer
        only** — entity + memory layers are always fully included (they're
        small). Pruning keeps the highest-degree AST nodes (degree computed
        against the FULL ast edge set); ties break on ``node_id`` for stable
        output. ``total_nodes``/``total_edges`` always reflect the full AST
        graph so the wire response can honestly say "showing N of M".

        Cross-layer ANCHORS are reconciled to real node ids in O(nodes+edges):
        memory ANCHORS targets (``EntityKey`` / ``entity:<id>``) batch-resolve
        through the existing ``CodeStructureProvider`` + ``EntityAnchorResolver``;
        an anchor that resolves to nothing is dropped (no dangling edges).
        """
        from mewbo_graph.entities.anchor import EntityAnchorResolver  # noqa: PLC0415

        from .structure_provider import CodeStructureProvider  # noqa: PLC0415

        # ── AST layer ────────────────────────────────────────────────────
        all_nodes = store.query_graph(slug)
        all_edges = list(store.list_edges(slug))
        total_nodes = len(all_nodes)

        # Resolve cross-file edge targets by name → real in-repo node. Build the
        # name index once (O(nodes)); externals converge on one synthesized node.
        by_name: dict[str, str] = {}
        for n in all_nodes:
            by_name.setdefault(n.name, n.node_id)
        resolved_edges, external_nodes = cls._resolve_ast_edges(
            slug, all_edges, by_name, {n.node_id for n in all_nodes}
        )
        # ``total_edges`` is the FULL emittable AST edge count — measured AFTER
        # resolution, because that pass drops orphan structural edges (a
        # ``target_name=None`` edge with a missing endpoint). Using the raw
        # ``len(all_edges)`` would overstate "M" by the orphans that never reach
        # the payload. (Resolution adds External nodes but never adds/drops
        # cross-file edges, so this count is cap-independent.)
        total_edges = len(resolved_edges)

        # Degree-prune the AST layer only (externals follow their surviving edge).
        if node_limit is None or total_nodes <= node_limit:
            nodes = list(all_nodes)
        else:
            degree: Counter[str] = Counter()
            for e in resolved_edges:
                degree[e.source] += 1
                degree[e.target] += 1
            nodes = sorted(
                all_nodes, key=lambda n: (-degree[n.node_id], n.node_id)
            )[:node_limit]

        kept_ast_ids = {n.node_id for n in nodes}
        ext_by_id = {n.node_id: n for n in external_nodes}
        edges = [
            e
            for e in resolved_edges
            if e.source in kept_ast_ids
            and (e.target in kept_ast_ids or e.target in ext_by_id)
        ]
        # Keep only externals still referenced by a surviving edge.
        live_ext_ids = {e.target for e in edges if e.target in ext_by_id}
        kept_externals = tuple(ext_by_id[i] for i in sorted(live_ext_ids))
        payload_ast_ids = kept_ast_ids | live_ext_ids

        # ── Entity layer ─────────────────────────────────────────────────
        entity_nodes = store.query_entities(slug)
        entity_ids = {e.id for e in entity_nodes}
        entity_rels: list[EntityRelation] = []
        entity_cross: list[tuple[str, str]] = []
        for rel in store.list_entity_edges(slug):
            if rel.target_id in entity_ids and rel.source_id in entity_ids:
                # entity ↔ entity → RELATES (verb in label)
                entity_rels.append(rel)
            elif rel.source_id in entity_ids and rel.target_id in payload_ast_ids:
                # entity → AST node → cross-layer ANCHORS
                entity_cross.append((rel.source_id, rel.target_id))
            # else: dangling (target absent from this payload) → dropped

        # ── Memory layer ─────────────────────────────────────────────────
        memory_nodes = store.query_memory(slug)
        memory_ids = {n.node_id for n in memory_nodes}
        mem_rels: list[MemoryEdge] = []
        mem_anchor_edges: list[MemoryEdge] = []
        for me in store.list_memory_edges(slug, include_invalidated=False):
            if me.type == "RELATES" and me.source in memory_ids:
                mem_rels.append(me)
            elif me.type == "ANCHORS" and me.source in memory_ids:
                mem_anchor_edges.append(me)

        # Batch-resolve memory ANCHORS targets to real node ids (one pass each).
        code_keys = [e.target for e in mem_anchor_edges if not e.target.startswith("entity:")]
        ent_keys = [e.target for e in mem_anchor_edges if e.target.startswith("entity:")]
        code_map = CodeStructureProvider(store).resolve_many(slug, code_keys)
        ent_map = EntityAnchorResolver(store).resolve_many(slug, ent_keys)
        memory_cross: list[tuple[str, str]] = []
        for me in mem_anchor_edges:
            if me.target.startswith("entity:"):
                ent = ent_map.get(me.target)
                tid = ent.id if ent is not None and ent.id in entity_ids else None
            else:
                node = code_map.get(me.target)
                tid = node.node_id if node is not None and node.node_id in payload_ast_ids else None
            if tid is not None:
                memory_cross.append((me.source, tid))

        return cls(
            slug=slug,
            nodes=tuple(nodes),
            external_nodes=kept_externals,
            edges=tuple(edges),
            entity_nodes=tuple(entity_nodes),
            entity_edges=tuple(entity_rels),
            memory_nodes=tuple(memory_nodes),
            memory_edges=tuple(mem_rels),
            cross_edges=tuple(entity_cross + memory_cross),
            total_nodes=total_nodes,
            total_edges=total_edges,
        )

    @staticmethod
    def _resolve_ast_edges(
        slug: str,
        all_edges: list[GraphEdge],
        by_name: dict[str, str],
        node_ids: set[str],
    ) -> tuple[list[GraphEdge], list[GraphNode]]:
        """Re-point cross-file edges to real nodes; synthesize External nodes.

        An edge with a ``target_name`` (cross-file IMPORTS/CALLS/EXTENDS) is
        re-pointed at the in-repo node of that name when one exists; otherwise
        every reference to the same name converges on one synthesized
        ``External`` node (deterministic id over the name). Edges WITHOUT a
        ``target_name`` (CONTAINS etc.) pass through only when both endpoints are
        real nodes — orphan hygiene unchanged from the prior behaviour.
        """
        resolved: list[GraphEdge] = []
        externals: dict[str, GraphNode] = {}
        for e in all_edges:
            if e.target_name is None:
                # In-repo structural edge; keep only if both endpoints are real.
                if e.source in node_ids and e.target in node_ids:
                    resolved.append(e)
                continue
            real = by_name.get(e.target_name)
            if real is not None and real != e.source:
                resolved.append(e.model_copy(update={"target": real}))
            else:
                ext_id = _stable_id(slug, "External", e.target_name, "<external>", 0)
                externals.setdefault(
                    ext_id,
                    GraphNode(
                        slug=slug,
                        node_id=ext_id,
                        type="External",
                        name=e.target_name,
                        file="",
                        range=(0, 0),
                        docstring=None,
                    ),
                )
                resolved.append(e.model_copy(update={"target": ext_id}))
        return resolved, list(externals.values())

    # ── Derived state ───────────────────────────────────────────────────

    @property
    def node_count(self) -> int:
        """AST nodes in this view (real + External), drives the truncation banner."""
        return len(self.nodes) + len(self.external_nodes)

    @property
    def edge_count(self) -> int:
        """AST-layer edges in this view (post-filter)."""
        return len(self.edges)

    @property
    def kinds(self) -> dict[str, int]:
        """Per-type AST-node histogram — drives the legend on the FE."""
        return dict(
            Counter(n.type for n in self.nodes)
            + Counter(n.type for n in self.external_nodes)
        )

    # ── Serialisation ───────────────────────────────────────────────────

    def to_wire(self) -> dict[str, Any]:
        """Wire shape: ``{slug, nodes, edges, stats}`` over all three layers.

        Each node/edge is Cytoscape-ready (``{data: {...}}``) and carries a
        ``layer`` tag so the FE can style/filter per layer. The FE hands the
        arrays straight to ``cy.add(elements)`` with no intermediate transform.
        """
        nodes = (
            [self._node_to_wire(n, "ast") for n in self.nodes]
            + [self._node_to_wire(n, "ast") for n in self.external_nodes]
            + [self._entity_node_to_wire(e) for e in self.entity_nodes]
            + [self._memory_node_to_wire(m) for m in self.memory_nodes]
        )
        edges = (
            [self._edge_to_wire(e, "ast") for e in self.edges]
            + [self._entity_edge_to_wire(e) for e in self.entity_edges]
            + [self._memory_edge_to_wire(e) for e in self.memory_edges]
            + [self._cross_edge_to_wire(s, t) for s, t in self.cross_edges]
        )
        return {
            "slug": self.slug,
            "nodes": nodes,
            "edges": edges,
            "stats": {
                # Legacy AST-only counters kept for back-compat consumers.
                "nodeCount": self.node_count,
                "edgeCount": self.edge_count,
                "kinds": self.kinds,
                # FULL multiplex counts ("M" in the FE "showing N of M" banner).
                # Only the AST layer is ever capped, so entity + memory + the
                # view-only External nodes contribute their in-view counts; the
                # pre-cap AST total is ``self.total_nodes``. Uncapped ⇒ M == N.
                "totalNodes": (
                    self.total_nodes
                    + len(self.external_nodes)
                    + len(self.entity_nodes)
                    + len(self.memory_nodes)
                ),
                "totalEdges": (
                    self.total_edges
                    + len(self.entity_edges)
                    + len(self.memory_edges)
                    + len(self.cross_edges)
                ),
                # ``truncated`` reflects the AST-layer node cap only (entity +
                # memory layers are always fully included). Compare REAL kept
                # AST nodes (``self.nodes``) against the pre-cap total —
                # synthesized External nodes are NOT real graph nodes, so
                # including them in the count can mask a genuine cap (e.g. cap 3
                # of 5 real nodes + 3 externals → 6 > 5 would falsely read
                # un-truncated). Edge drop from orphan hygiene is not truncation.
                "truncated": len(self.nodes) < self.total_nodes,
                "perLayer": {
                    "ast": len(self.nodes) + len(self.external_nodes),
                    "entity": len(self.entity_nodes),
                    "memory": len(self.memory_nodes),
                },
            },
        }

    # ── Static helpers (per-record formatters) ──────────────────────────

    @staticmethod
    def _node_to_wire(n: GraphNode, layer: str) -> dict[str, Any]:
        return {
            "data": {
                "id": n.node_id,
                "label": n.name,
                "kind": n.type,
                "layer": layer,
                "file": n.file,
                "range": list(n.range),
                "docstring": n.docstring or "",
            },
        }

    @staticmethod
    def _entity_node_to_wire(e: Entity) -> dict[str, Any]:
        return {
            "data": {
                "id": e.id,
                "label": e.name,
                "kind": "Entity",
                "layer": "entity",
                "entityType": e.type,
                "labels": list(e.labels),
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
    def _edge_to_wire(e: GraphEdge, layer: str) -> dict[str, Any]:
        return {
            "data": {
                "id": f"{e.source}__{e.type}__{e.target}",
                "source": e.source,
                "target": e.target,
                "kind": e.type,
                "layer": layer,
            },
        }

    @staticmethod
    def _entity_edge_to_wire(e: EntityRelation) -> dict[str, Any]:
        # Open-vocab relation verb rides ``label`` (NOT ``kind``) so the FE's
        # closed kind-union stays closed — ``RELATES`` is the only entity kind.
        return {
            "data": {
                "id": f"{e.source_id}__RELATES__{e.target_id}__{e.type}",
                "source": e.source_id,
                "target": e.target_id,
                "kind": "RELATES",
                "layer": "entity",
                "label": e.type,
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
        return {
            "data": {
                "id": f"{source}__ANCHORS__{target}",
                "source": source,
                "target": target,
                "kind": "ANCHORS",
                "layer": "cross",
            },
        }


def _extract_docstring(node) -> str | None:
    """Pull the first string literal inside a function/class body.

    In the tree-sitter Python grammar, docstrings appear as ``string`` nodes
    that are direct children of the ``block`` node (not wrapped in
    ``expression_statement``). We also handle the ``expression_statement``
    wrapping as a fallback for compatibility.
    """
    body = next((c for c in node.children if c.type == "block"), None)
    if not body:
        return None
    for child in body.children:
        # Direct string child (tree-sitter 0.25 Python grammar)
        if child.type == "string":
            raw = child.text.decode()
            # Strip surrounding triple/single/double quotes and whitespace.
            return raw.strip("\"'").strip()
        # Fallback: expression_statement wrapping a string
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = sub.text.decode()
                    return raw.strip("\"'").strip()
    return None
