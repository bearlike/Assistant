"""``wiki_code_search`` SessionTool — hybrid graph-node search (BM25 + cosine + 1-hop)."""
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

logging = get_logger(name="core.builtin_plugins.wiki.code_search")


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


class WikiCodeSearchArgs(BaseModel):
    """Arguments for ``wiki_code_search``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Free-text query to search over code graph nodes.")
    k: int = Field(default=8, ge=1, le=50, description="Maximum number of results.")
    types: list[str] | None = Field(
        default=None,
        description="Optional list of node types to filter (e.g. ['Class', 'Function']).",
    )
    graph_expand: bool = Field(
        default=True,
        description="Whether to expand top hits with 1-hop graph neighbours.",
    )


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiCodeSearchTool:
    """SessionTool: hybrid code-graph search (BM25 + cosine + optional 1-hop expansion)."""

    tool_id = "wiki_code_search"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiCodeSearchArgs, name="wiki_code_search"
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
        """Execute a ``wiki_code_search`` tool call."""
        # 1. Resolve runtime and QA ctx.
        runtime = _resolve_runtime()
        ctx = resolve_qa_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        # 2. Parse and validate args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiCodeSearchArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        # 3. Run hybrid search over graph nodes.
        try:
            from mewbo_api.wiki.retriever import HybridRetriever  # noqa: PLC0415
            embedder = _make_embedder()
            retriever = HybridRetriever(store=ctx.store, embedder=embedder)
            hits = retriever.search(
                ctx.slug,
                args.query,
                k=args.k,
                types=args.types,
                graph_expand=args.graph_expand,
                sources="graph",
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("wiki_code_search retrieval error: %s", exc)
            return _err_result("internal", f"retrieval failed: {exc}")

        # 4. Map to wire shape.
        results = [
            {
                "nodeId": h.id,
                "type": h.metadata.get("type", ""),
                "name": h.metadata.get("name", h.id),
                "file": h.metadata.get("file", ""),
                "score": round(h.score, 6),
                "snippet": h.snippet,
            }
            for h in hits
            if h.kind == "node"
        ]
        return MockSpeaker(content=str({"hits": results}))


__all__ = ["WikiCodeSearchArgs", "WikiCodeSearchTool"]
