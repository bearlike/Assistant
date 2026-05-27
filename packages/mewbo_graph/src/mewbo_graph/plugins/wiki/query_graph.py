"""``wiki_query_graph`` SessionTool — read-only graph queries for both indexer and QA agents."""
from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.query_graph")


class WikiQueryGraphArgs(BaseModel):
    """Args for wiki_query_graph."""

    model_config = ConfigDict(extra="forbid")

    node_type: str | None = Field(default=None, description="Filter by node type")
    name_match: str | None = Field(
        default=None, description="Case-insensitive substring match on name"
    )
    neighbors_of: str | None = Field(
        default=None,
        description=(
            "Return 1-hop neighbours of this node_id. For directed, "
            "edge-kind-filtered, or multi-hop traversal use "
            "``wiki_graph_neighbors`` instead."
        ),
    )
    file_glob: str | None = Field(
        default=None,
        description=(
            "Optional fnmatch glob on ``node.file`` to scope results "
            "(e.g. ``src/grove/client/**`` to list every symbol in "
            "the client package)."
        ),
    )
    limit: int = Field(default=50, ge=1, le=500, description="Max nodes to return")


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


class WikiQueryGraphTool(WikiSessionTool):
    """SessionTool: read-only graph query usable from both indexer + QA agents."""

    tool_id = "wiki_query_graph"
    args_cls = WikiQueryGraphArgs
    schema = pydantic_to_openai_tool(WikiQueryGraphArgs, name="wiki_query_graph")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_query_graph`` tool call."""
        # Resolve from EITHER indexing job OR QA session — same query.
        ctx = self._job_ctx() or self._qa_ctx()
        slug = ctx.slug if ctx is not None else None
        if ctx is None or not slug:
            return _err_result("internal", "wiki ctx not found for this session")

        args = self._parse_args(WikiQueryGraphArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        nodes = ctx.store.query_graph(
            slug,
            node_type=args.node_type,
            name_match=args.name_match,
            neighbors_of=args.neighbors_of,
        )
        if args.file_glob:
            # fnmatch is case-sensitive on POSIX; node.file paths are
            # repo-relative POSIX-style so this matches what the model
            # sees in source / page references.
            nodes = [n for n in nodes if fnmatch.fnmatch(n.file or "", args.file_glob)]
        nodes = nodes[:args.limit]
        return MockSpeaker(content=str({
            "count": len(nodes),
            "nodes": [n.model_dump() for n in nodes],
        }))


__all__ = [
    "WikiQueryGraphArgs",
    "WikiQueryGraphTool",
]
