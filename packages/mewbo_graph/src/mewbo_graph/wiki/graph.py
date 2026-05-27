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
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from .types import GraphEdge, GraphNode

if TYPE_CHECKING:
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

    # Ensure the library is downloaded before trying to load it.
    if not lib_path.exists():
        _log.info("Downloading tree-sitter language library for {}", lang_name)
        tlp.download([lang_name])

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

    # Imports — IMPORTS edges from file to the import target name (target node
    # may not exist if the imported module is external; the edge is informational).
    import_nodes = captures.get("import.module", []) + captures.get(
        "import.from_module", []
    )
    for mod_node in import_nodes:
        target_name = mod_node.text.decode()
        target_id = _stable_id(slug, "Module", target_name, target_name, 0)
        edges.append(
            GraphEdge(
                slug=slug, source=file_node.node_id, target=target_id, type="IMPORTS"
            )
        )

    # Calls — CALLS edges from the file to the called name. Exact scope
    # resolution is out of v1's scope.
    for call_name_node in captures.get("call.name", []):
        callee = call_name_node.text.decode()
        target_id = _stable_id(slug, "Function", callee, "<external>", 0)
        edges.append(
            GraphEdge(
                slug=slug, source=file_node.node_id, target=target_id, type="CALLS"
            )
        )

    # EXTENDS edges from subclass → superclass.
    subs = captures.get("subclass.name", [])
    sups = captures.get("superclass.name", [])
    for sub_node, sup_node in zip(subs, sups):
        sub_name = sub_node.text.decode()
        sup_name = sup_node.text.decode()
        sub_id = _stable_id(slug, "Class", sub_name, rel_path, sub_node.start_byte)
        sup_id = _stable_id(slug, "Class", sup_name, "<external>", 0)
        edges.append(
            GraphEdge(slug=slug, source=sub_id, target=sup_id, type="EXTENDS")
        )

    return GraphParseResult(nodes=nodes, edges=edges, skipped=[])


@dataclass(frozen=True, slots=True)
class KnowledgeGraphView:
    """Slug-scoped projection of the persisted code graph for the viewer.

    Construction is exclusively via ``for_slug`` so the invariant — every
    node + edge belongs to the same slug — stays enforced in one place.
    Once built, the instance is immutable; safe to share across requests
    and trivially cacheable upstream.
    """

    slug: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    total_nodes: int
    total_edges: int

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def for_slug(
        cls,
        store: WikiStoreBase,
        slug: str,
        *,
        node_limit: int | None = None,
    ) -> KnowledgeGraphView:
        """Load nodes + edges for *slug* from *store*.

        When ``node_limit`` is set and the graph exceeds it, keep the
        ``node_limit`` highest-degree nodes (computed against the FULL
        edge set, not a head-truncated one) and drop edges whose
        endpoints aren't both in the surviving set. Ties on degree
        break on ``node_id`` for stable output across requests.

        Degree ranking beats first-N: first-N keeps whichever nodes the
        AST walker happened to emit first (roughly file-scan order),
        not the structurally important ones. With degree ranking, a
        capped view still shows the hubs of the graph.

        ``total_nodes`` and ``total_edges`` always reflect the FULL graph
        — that way the wire response can honestly tell the FE "showing
        N of M" when a cap is in effect.
        """
        all_nodes = store.query_graph(slug)
        all_edges = list(store.list_edges(slug))
        total_nodes = len(all_nodes)
        total_edges = len(all_edges)

        if node_limit is None or total_nodes <= node_limit:
            nodes = all_nodes
        else:
            # Degree = total endpoints (in + out) per node. Cheap O(E).
            degree: Counter[str] = Counter()
            for e in all_edges:
                degree[e.source] += 1
                degree[e.target] += 1
            nodes = sorted(
                all_nodes,
                key=lambda n: (-degree[n.node_id], n.node_id),
            )[:node_limit]

        node_ids = {n.node_id for n in nodes}
        edges = [
            e for e in all_edges
            if e.source in node_ids and e.target in node_ids
        ]
        return cls(
            slug=slug,
            nodes=tuple(nodes),
            edges=tuple(edges),
            total_nodes=total_nodes,
            total_edges=total_edges,
        )

    # ── Derived state ───────────────────────────────────────────────────

    @property
    def node_count(self) -> int:
        """Number of nodes in this view."""
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        """Number of edges in this view (post-filter)."""
        return len(self.edges)

    @property
    def kinds(self) -> dict[str, int]:
        """Per-type node histogram — drives the legend on the FE."""
        return dict(Counter(n.type for n in self.nodes))

    # ── Serialisation ───────────────────────────────────────────────────

    def to_wire(self) -> dict[str, Any]:
        """Wire shape: ``{slug, nodes, edges, stats}``.

        Each node/edge is Cytoscape-ready — the FE can hand the arrays
        straight to ``cy.add(elements)`` with no intermediate transform.
        """
        return {
            "slug": self.slug,
            "nodes": [self._node_to_wire(n) for n in self.nodes],
            "edges": [self._edge_to_wire(e) for e in self.edges],
            "stats": {
                "nodeCount": self.node_count,
                "edgeCount": self.edge_count,
                "kinds": self.kinds,
                "totalNodes": self.total_nodes,
                "totalEdges": self.total_edges,
                # ``truncated`` reflects user-applied node cap only. Edge
                # drop from orphan filtering (endpoints missing from the
                # node table, typically left over from re-indexings) is
                # data hygiene, not truncation, and shouldn't drive the
                # FE's "showing N of M" banner.
                "truncated": self.node_count < self.total_nodes,
            },
        }

    # ── Static helpers (per-record formatters) ──────────────────────────

    @staticmethod
    def _node_to_wire(n: GraphNode) -> dict[str, Any]:
        return {
            "data": {
                "id": n.node_id,
                "label": n.name,
                "kind": n.type,
                "file": n.file,
                "range": list(n.range),
                "docstring": n.docstring or "",
            },
        }

    @staticmethod
    def _edge_to_wire(e: GraphEdge) -> dict[str, Any]:
        return {
            "data": {
                "id": f"{e.source}__{e.type}__{e.target}",
                "source": e.source,
                "target": e.target,
                "kind": e.type,
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
