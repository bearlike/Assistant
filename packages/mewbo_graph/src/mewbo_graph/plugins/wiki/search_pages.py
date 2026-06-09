"""``wiki_search_pages`` SessionTool — BM25+cosine hybrid search over wiki pages."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.search_pages")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


def _make_embedder() -> Any:
    """Construct and return an Embedder instance. Module-level for test stubbing."""
    from mewbo_graph.wiki.embedder import Embedder  # noqa: PLC0415
    return Embedder()


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiSearchPagesArgs(BaseModel):
    """Arguments for ``wiki_search_pages``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Free-text search query over wiki page bodies.")
    k: int = Field(default=5, ge=1, le=20, description="Maximum number of results to return.")


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiSearchPagesTool(WikiSessionTool):
    """SessionTool: hybrid page search using BM25 + cosine fusion."""

    tool_id = "wiki_search_pages"
    args_cls = WikiSearchPagesArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiSearchPagesArgs, name="wiki_search_pages"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_search_pages`` tool call."""
        # 1. Resolve runtime and QA ctx.
        ctx = self._qa_ctx()
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        # 2. Parse and validate args.
        args = self._parse_args(WikiSearchPagesArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Run hybrid search over pages.
        try:
            from mewbo_graph.wiki.retriever import HybridRetriever  # noqa: PLC0415
            embedder = _make_embedder()
            retriever = HybridRetriever(store=ctx.store, embedder=embedder)
            hits = retriever.search(ctx.slug, args.query, k=args.k, sources="pages")
        except Exception as exc:  # noqa: BLE001
            logging.warning("wiki_search_pages retrieval error: %s", exc)
            return _err_result("internal", f"retrieval failed: {exc}")

        # 4. Map to wire shape.
        results = [
            {
                "pageId": h.id,
                "title": h.metadata.get("title", h.id),
                "score": round(h.score, 6),
                "snippet": h.snippet,
            }
            for h in hits
        ]

        # 5. Emit summary_ready once — first search call in a QA session sets
        #    the source list the frontend renders before blocks arrive. Only a
        #    registered QA answer has an event log; a grounded structured-response
        #    session (``answer_id is None``) has nowhere to write it, so skip.
        if ctx.answer_id is not None:
            existing = ctx.store.load_qa_events(ctx.answer_id)
            if not any(ev.get("type") == "summary_ready" for ev in existing):
                source_ids = [h.id for h in hits]
                ctx.store.append_qa_event(ctx.answer_id, {
                    "type": "summary_ready",
                    "sources": source_ids,
                })

        self._record_qa_access(ctx, [f"wiki:{h.id}" for h in hits])
        return MockSpeaker(content=str({"hits": results}))


__all__ = ["WikiSearchPagesArgs", "WikiSearchPagesTool"]
