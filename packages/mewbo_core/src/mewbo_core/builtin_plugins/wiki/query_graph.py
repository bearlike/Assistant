"""``wiki_query_graph`` SessionTool — read-only graph queries for both indexer and QA agents."""
from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import resolve_job_ctx, resolve_qa_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.query_graph")


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


def _resolve_runtime():
    """Late-import runtime; monkeypatched in tests."""
    try:
        from mewbo_api.wiki.routes import _runtime  # noqa: PLC0415
        return _runtime
    except ImportError:
        return None


class WikiQueryGraphTool:
    """SessionTool: read-only graph query usable from both indexer + QA agents."""

    tool_id = "wiki_query_graph"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema = pydantic_to_openai_tool(WikiQueryGraphArgs, name="wiki_query_graph")

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise the tool with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_query_graph`` tool call."""
        runtime = _resolve_runtime()
        # Resolve from EITHER indexing job OR QA session — same query.
        ctx = None
        slug = None
        if runtime is not None:
            jc = resolve_job_ctx(self._session_id, runtime)
            if jc is not None:
                ctx = jc
                slug = jc.slug
            else:
                qc = resolve_qa_ctx(self._session_id, runtime)
                if qc is not None:
                    ctx = qc
                    slug = qc.slug
        if ctx is None or not slug:
            return _err_result("internal", "wiki ctx not found for this session")

        try:
            args = WikiQueryGraphArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return _err_result("validation", str(ve))

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
