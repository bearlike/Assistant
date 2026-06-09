"""WikiGroundingProvider — implements ``GroundingProvider`` over ``HybridRetriever``.

Capability-gated: ``mewbo_graph.wiki`` is an optional extra (``mewbo-api[wiki]``).
When the extra is not installed ``search`` returns ``[]`` (graceful absence).

The concrete provider lives here (in an app) rather than in ``mewbo_core``
because it imports ``mewbo_graph`` — keeping core graph-free (layering DAG
invariant).
"""
from __future__ import annotations

from mewbo_core.common import get_logger
from mewbo_core.structured_synthesis import Citation

logging = get_logger(name="api.realtime.grounding")


class WikiGroundingProvider:
    """Implements :class:`~mewbo_core.structured_synthesis.GroundingProvider`.

    Wraps :class:`~mewbo_graph.wiki.retriever.HybridRetriever` over the
    process-wide wiki store singleton + the configured embedder.  Lazy import
    guards ensure the class is constructable even when ``mewbo-graph`` is not
    installed — ``search`` simply returns ``[]``.

    Satisfies the :class:`~mewbo_core.structured_synthesis.GroundingProvider`
    Protocol structurally (duck-typed), so no explicit ``Protocol`` base is
    needed and ``mewbo_core`` never imports this class.
    """

    def search(self, slug: str, query: str, *, k: int = 8) -> list[Citation]:
        """Return up to *k* :class:`~mewbo_core.structured_synthesis.Citation` records.

        Args:
            slug: Wiki workspace slug.
            query: Free-text search query.
            k: Maximum number of results.

        Returns:
            Empty list when ``mewbo-graph[retrieval]`` is not installed or the
            store has no data for the workspace (graceful degradation).
        """
        try:
            from mewbo_graph.wiki.retriever import HybridRetriever  # noqa: PLC0415
            from mewbo_graph.wiki.store import get_wiki_store  # noqa: PLC0415
        except ImportError:
            logging.debug("mewbo_graph not installed — WikiGroundingProvider returns [].")
            return []

        store = get_wiki_store()

        try:
            # Mirror the canonical wiki-tool pattern (search_pages.py): construct a
            # real Embedder and run the hybrid search. Any embedder construction or
            # embed failure surfaces here and degrades to [] — exactly like the wiki
            # retrieval tools, which also wrap Embedder() + retriever.search in a
            # single try/except. (HybridRetriever requires a non-None embedder.)
            from mewbo_graph.wiki.embedder import Embedder  # noqa: PLC0415
            retriever = HybridRetriever(store=store, embedder=Embedder())
            hits = retriever.search(slug, query, k=k)
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "WikiGroundingProvider.search failed for slug={}: {}", slug, exc
            )
            return []

        citations: list[Citation] = []
        for hit in hits:
            meta = hit.metadata or {}
            # Best-effort source: page title, node name, or bare id.
            source = meta.get("title") or meta.get("name") or hit.id
            citations.append(
                Citation(
                    id=hit.id,
                    kind=hit.kind,
                    snippet=hit.snippet,
                    score=hit.score,
                    source=str(source),
                )
            )
        return citations


__all__ = ["WikiGroundingProvider"]
