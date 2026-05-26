"""``wiki_search_pages`` SessionTool — BM25+cosine hybrid search over wiki pages."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import resolve_qa_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.search_pages")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any | None:
    """Late-import the wiki API runtime. Returns None if the API is not available."""
    try:
        from mewbo_api.wiki.routes import _runtime  # noqa: PLC0415
        return _runtime
    except ImportError:
        return None


def _make_embedder() -> Any:
    """Construct and return an Embedder instance. Module-level for test stubbing."""
    from mewbo_api.wiki.embedder import Embedder  # noqa: PLC0415
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


class WikiSearchPagesTool:
    """SessionTool: hybrid page search using BM25 + cosine fusion."""

    tool_id = "wiki_search_pages"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiSearchPagesArgs, name="wiki_search_pages"
    )

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_search_pages`` tool call."""
        # 1. Resolve runtime and QA ctx.
        runtime = _resolve_runtime()
        ctx = resolve_qa_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        # 2. Parse and validate args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiSearchPagesArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        # 3. Run hybrid search over pages.
        try:
            from mewbo_api.wiki.retriever import HybridRetriever  # noqa: PLC0415
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
        #    the source list the frontend renders before blocks arrive.
        existing = ctx.store.load_qa_events(ctx.answer_id)
        if not any(ev.get("type") == "summary_ready" for ev in existing):
            source_ids = [h.id for h in hits]
            ctx.store.append_qa_event(ctx.answer_id, {
                "type": "summary_ready",
                "sources": source_ids,
            })

        return MockSpeaker(content=str({"hits": results}))


__all__ = ["WikiSearchPagesArgs", "WikiSearchPagesTool"]
