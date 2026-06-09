"""CatalogIngestor — programmatic (non-git) document ingestion.

A catalog workspace is a wiki project that was NOT built from a git clone +
tree-sitter pass. Instead, a caller hands a batch of plain records
(``{id, title, text, metadata}``) over REST and each one is written, by this
single atomic class, as BOTH:

- a :class:`WikiPage` — so BM25 + ``wiki_search_pages`` / ``wiki_read_page``
  find it, and
- a code-graph :class:`GraphNode` carrying the text — so the same node
  embeds (cosine) and ``wiki_code_search`` finds it.

That is exactly the corpus :class:`HybridRetriever` already grounds over
(pages via BM25, graph nodes via BM25 + cosine), so the existing retriever and
Q&A work unchanged — no fake clone, no agent pipeline, no tree-sitter.

Design notes (KISS / DRY / down-only):

- Pure engine, lives DOWN in ``mewbo_graph.wiki`` over the existing
  :class:`WikiStoreBase` (+ optional :class:`EmbedderProtocol`); the API route
  is thin glue that composes it.
- Deterministic, content-addressed ids (``sha1`` over slug+doc-id) ⇒ every
  write is an UPSERT: re-ingesting the same doc id updates, never duplicates
  (mirrors the graph/memory/entity upsert idiom).
- Embedding is optional and import-guarded at the construction site: when no
  embedder is available (or one raises), ingestion degrades to BM25-only and
  warns — never crashes — mirroring ``build_graph.py``'s embed-failure path.
- Creates a minimal-but-honest ``complete`` :class:`Project` with a populated
  graph, so the same completion-correctness invariant the git pipeline enforces
  (``finalize._graph_is_populated``) holds for catalog projects too.
"""
from __future__ import annotations

import datetime
import hashlib
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger

from .store import WikiStoreBase
from .types import (
    CatalogDocument,
    CatalogIngestReport,
    Frontmatter,
    GraphNode,
    GraphNodeType,
    PlatformId,
    Project,
    WikiPage,
)

if TYPE_CHECKING:
    from .embedder import EmbedderProtocol

logging = get_logger(name="mewbo_graph.wiki.catalog")

# A catalog node is a synthetic File-kind entry: it isn't a code symbol, but
# ``GraphNode`` is the only text-carrying node the retriever embeds, and File
# is the most honest of the closed ``GraphNodeType`` union for a whole document.
# Semantic reuse, NOT a perfect fit: a dedicated ``"Document"`` node type (+ the
# FE KG ``Record``-map update to render it) is a deferred follow-up the FE
# catalog task owns. Until then, catalog docs are distinguished from git AST
# File nodes by their ``file`` prefix (``_CATALOG_FILE_PREFIX``), not their type.
_CATALOG_NODE_TYPE: GraphNodeType = "File"
_CATALOG_FILE_PREFIX = "catalog/"
_PLATFORM: PlatformId = "git"  # neutral PlatformId for a non-git workspace
_LANDING_PAGE_ID = "catalog-index"


class CatalogIngestor:
    """Atomic engine: write a batch of catalog documents as pages + graph nodes.

    Constructed over the wiki ``store`` and an optional ``embedder`` (DI, like
    the other graph engines). ``ingest`` is the one public method.
    """

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: EmbedderProtocol | None = None,
    ) -> None:
        """Inject the store and an optional embedder (None ⇒ try to build one)."""
        self._store = store
        self._embedder = embedder

    # -- Public API ----------------------------------------------------------

    def ingest(self, slug: str, documents: list[CatalogDocument]) -> CatalogIngestReport:
        """Ingest *documents* into project *slug*; return an upsert report.

        Idempotent on doc ``id``: re-ingesting the same ids updates the page +
        node in place (deterministic ids). The project is (re)created as
        ``complete`` with a populated graph. Embedding is best-effort.
        """
        pages: list[WikiPage] = []
        nodes: list[GraphNode] = []
        embed_items: list[tuple[str, str]] = []

        for doc in documents:
            # One content-addressed id keys BOTH the page and the node so they
            # can never desync. Slugifying ``doc.id`` (the old page id) collided
            # distinct ids ("foo bar" vs "foo-bar") to the same page → silent
            # overwrite while the SHA-1 node id stayed distinct.
            doc_id = self._doc_id(slug, doc.id)
            pages.append(self._build_page(doc, doc_id))
            node = self._build_node(slug, doc, doc_id)
            nodes.append(node)
            embed_items.append((doc_id, _node_text(doc)))

        # 1. Persist pages + nodes (both stores upsert by id).
        for page in pages:
            self._store.save_page(slug, page)
        self._store.upsert_nodes(slug, nodes)

        # 2. Embed the nodes — best-effort. A missing/failed embedder degrades
        #    to BM25-only (pages aren't embedded anyway; graph nodes simply lose
        #    their cosine ranker and fall back to BM25 over name+text). Mirrors
        #    build_graph.py: warn, never crash.
        bm25_only = not self._embed_nodes(slug, embed_items)

        # 3. Ensure a landing page exists, then create/refresh the Project as a
        #    complete catalog workspace with a non-empty graph. ``Project.pages``
        #    counts every page (incl. the synthetic landing index); the catalog
        #    SIZE the report advertises is the document count (= graph nodes,
        #    one per doc), which excludes the landing page.
        landing_id = self._ensure_landing_page(slug)
        page_total = len(self._store.list_pages(slug))
        # Count ONLY catalog nodes (by their ``catalog/`` file prefix). A bare
        # ``node_type="File"`` query would also fold in git AST File nodes if the
        # same slug was ever git-indexed, over-counting the catalog size.
        doc_total = sum(
            1
            for n in self._store.query_graph(slug, node_type=_CATALOG_NODE_TYPE)
            if n.file.startswith(_CATALOG_FILE_PREFIX)
        )
        self._upsert_project(slug, total_pages=page_total, landing_id=landing_id)

        return CatalogIngestReport(
            slug=slug,
            ingested=len(documents),
            embedded=0 if bm25_only else len(embed_items),
            totalDocuments=doc_total,
            bm25Only=bm25_only,
            landingPageId=landing_id,
        )

    # -- Builders ------------------------------------------------------------

    @staticmethod
    def _doc_id(slug: str, doc_id: str) -> str:
        """Deterministic content-addressed id keying the page AND the node.

        Pages are fetched by id (never by a user-typed name), so a SHA-1 over
        ``slug|catalog|doc_id`` is the right key: distinct user ids stay
        distinct (no slug collision) and re-ingesting the same id upserts.
        """
        return hashlib.sha1(f"{slug}|catalog|{doc_id}".encode()).hexdigest()[:16]

    def _build_page(self, doc: CatalogDocument, doc_id: str) -> WikiPage:
        """Build a WikiPage whose body is the document text (BM25 corpus)."""
        frontmatter = Frontmatter(title=doc.title, slug=doc_id)
        # Metadata is rendered as a small table-free preamble so it is part of
        # the BM25 body without inventing a new block kind.
        meta_lines = "".join(f"- **{k}**: {v}\n" for k, v in sorted(doc.metadata.items()))
        body = f"# {doc.title}\n\n{doc.text}\n"
        if meta_lines:
            body += f"\n## Details\n\n{meta_lines}"
        return WikiPage(
            id=doc_id,
            title=doc.title,
            frontmatter=frontmatter,
            body=body,
            toc=[],
            nav=[],
        )

    @staticmethod
    def _build_node(slug: str, doc: CatalogDocument, node_id: str) -> GraphNode:
        """Build a graph node carrying the doc text (embedding + code-search)."""
        return GraphNode(
            slug=slug,
            node_id=node_id,
            type=_CATALOG_NODE_TYPE,
            name=doc.title,
            file=f"{_CATALOG_FILE_PREFIX}{doc.id}",
            range=(0, 0),
            docstring=doc.text,
        )

    # -- Embedding (best-effort, BM25 fallback) ------------------------------

    def _embed_nodes(self, slug: str, items: list[tuple[str, str]]) -> bool:
        """Embed *items*; return True iff vectors were written (else BM25-only).

        Resolves an embedder lazily (the same ``make_embedder_or_none`` path the
        insight ingestor uses) when none was injected, then guards the call so a
        proxy with no embedding model never fails the ingest.
        """
        if not items:
            return False
        embedder = self._resolve_embedder()
        if embedder is None:
            logging.warning(
                "catalog ingest: no embedder available; grounding catalog %s "
                "with BM25 only",
                slug,
            )
            return False
        try:
            embeddings = embedder.embed_nodes(items, slug=slug)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully like build_graph
            logging.warning(
                "catalog ingest: embeddings unavailable for %s (%s); falling "
                "back to BM25",
                slug,
                exc,
            )
            return False
        if not embeddings:
            return False
        self._store.upsert_embeddings(slug, embeddings)
        return True

    def _resolve_embedder(self) -> EmbedderProtocol | None:
        """Return the injected embedder, or try to build one (None ⇒ BM25-only)."""
        if self._embedder is not None:
            return self._embedder
        try:
            from .embedder import make_embedder_or_none  # noqa: PLC0415

            return make_embedder_or_none()
        except Exception:  # pragma: no cover — import-guard for a graph-less install
            return None

    # -- Project + landing page ----------------------------------------------

    def _ensure_landing_page(self, slug: str) -> str:
        """Create (once) a catalog index landing page; return its id.

        Reuses any existing landing page on re-ingest (don't churn it). The page
        is itself part of the BM25 corpus, which is harmless — it just lists the
        catalog as a stable entry point.
        """
        existing = self._store.get_page(slug, _LANDING_PAGE_ID)
        if existing is not None:
            return existing.id
        page = WikiPage(
            id=_LANDING_PAGE_ID,
            title="Catalog",
            frontmatter=Frontmatter(title="Catalog", slug=_LANDING_PAGE_ID),
            body="# Catalog\n\nProgrammatically ingested documents.\n",
            toc=[],
            nav=[],
        )
        self._store.save_page(slug, page)
        return page.id

    def _upsert_project(self, slug: str, *, total_pages: int, landing_id: str) -> None:
        """Create/refresh the Project record as a complete catalog workspace.

        ``create_project`` is an upsert in both backends. The graph is non-empty
        (one node per doc), so the same completion-correctness invariant the git
        finalize enforces (``_graph_is_populated``) holds honestly here. We keep
        any existing description on re-ingest rather than blanking it.
        """
        indexed_at = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        existing = self._store.get_project(slug)
        desc = (existing.desc if existing is not None and existing.desc else "") or (
            "Programmatically ingested catalog workspace."
        )
        project = Project(
            slug=slug,
            source=_PLATFORM,
            lang="en",
            indexedAt=indexed_at,
            pages=total_pages,
            primary=False,
            desc=desc,
            landingPageId=landing_id,
            repoUrl=None,
            host=None,
        )
        self._store.create_project(project)


def _node_text(doc: CatalogDocument) -> str:
    """Text fed to the embedder for a catalog node (title + body)."""
    return f"{doc.title} — {doc.text}".strip()


__all__ = ["CatalogIngestor"]
