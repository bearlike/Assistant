"""``wiki_code_search`` SessionTool — hybrid graph-node search (BM25 + cosine + 1-hop)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime
from mewbo_graph.wiki.qa_access import QaAccessRecord

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.code_search")


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
        default=False,
        description=(
            "Expand the top hits with their 1-hop graph neighbours. Off by "
            "default: expansion returns more than ``k`` results, so the response "
            "size stops being bounded by the argument that is supposed to bound "
            "it. Turn it on when neighbours are the point of the query."
        ),
    )


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiCodeSearchTool(WikiSessionTool):
    """SessionTool: hybrid code-graph search (BM25 + cosine + optional 1-hop expansion)."""

    tool_id = "wiki_code_search"
    args_cls = WikiCodeSearchArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiCodeSearchArgs, name="wiki_code_search"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_code_search`` tool call."""
        # 1. Resolve runtime and QA ctx.
        ctx = self._qa_ctx()
        if ctx is None:
            return self._ungrounded_result()

        # 2. Parse and validate args.
        args = self._parse_args(WikiCodeSearchArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Run hybrid search over graph nodes.
        try:
            from mewbo_graph.wiki.retriever import HybridRetriever  # noqa: PLC0415
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
            logging.warning("wiki_code_search retrieval error: {}", exc)
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
        # Grounding: record only the node hits clearing the score floor (a ratio
        # of the top hit's score), carrying the retriever's real score + rank.
        # ``hits`` is already score-sorted (the ONE ranking engine) — no re-rank.
        node_hits = [(f"graph:{h.id}", h.score) for h in hits if h.kind == "node"]
        self._record_qa_access(
            ctx, QaAccessRecord.from_ranked_hits(node_hits, tool=self.tool_id)
        )
        return MockSpeaker(content=str({"hits": results}))


__all__ = ["WikiCodeSearchArgs", "WikiCodeSearchTool"]
