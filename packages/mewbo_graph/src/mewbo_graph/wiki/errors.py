"""Wiki-layer exceptions raised by the persistence/view substrate.

These are real Python exceptions (distinct from :class:`WikiError`, which is a
Pydantic *wire* model the API serialises). They are raised DOWN here so every
upstream — the API routes, the MCP facade, and the Q&A orchestrator — inherits a
single deterministic failure from one raise site instead of re-deriving the
condition at each transport.
"""
from __future__ import annotations


class DocumentationUnavailableError(Exception):
    """A project was indexed graph-only (developer mode) — it has no docs.

    Raised at the single doc-content read seam (``WikiStoreBase.get_page``) when
    the owning project carries ``graph_only=True``: such a project has a fully
    populated AST graph (still visualisable via the graph endpoint) but ZERO
    documentation pages, so any attempt to read page content is a deterministic
    error rather than a confusing empty result.

    Carries the ``slug`` so the API can map it to a stable error envelope (HTTP
    409 ``documentation_unavailable``) and the message names the project.
    """

    def __init__(self, slug: str) -> None:
        """Build the error for *slug* with a deterministic message."""
        self.slug = slug
        super().__init__(
            f"No documentation for '{slug}': indexed in graph-only (developer) mode."
        )


__all__ = ["DocumentationUnavailableError"]
