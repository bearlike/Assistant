"""``wiki_list_pages`` — page catalog for the wiki-qa agent.

This is the agent's "front door". Before deciding what to search for,
the agent can fetch the list of every page in the wiki and pick the
most plausibly-relevant one by title. Way cheaper than a BM25 search
when the question maps cleanly onto a single page.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.list_pages")


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


class WikiListPagesArgs(BaseModel):
    """Arguments for ``wiki_list_pages``."""

    model_config = ConfigDict(extra="forbid")

    title_contains: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter on titles. Use to "
            "narrow a large catalog (e.g. 'auth' to find auth-related pages)."
        ),
    )


class WikiListPagesTool(WikiSessionTool):
    """SessionTool: list every wiki page for the QA session's slug."""

    tool_id = "wiki_list_pages"
    args_cls = WikiListPagesArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiListPagesArgs, name="wiki_list_pages"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_list_pages`` tool call."""
        ctx = self._qa_ctx()
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        args = self._parse_args(WikiListPagesArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        try:
            pages = ctx.store.list_pages(ctx.slug)
        except Exception as exc:  # noqa: BLE001
            return _err_result("internal", f"list_pages failed: {exc}")

        needle = (args.title_contains or "").strip().lower()
        results = []
        for p in pages:
            title = p.title or p.id
            if needle and needle not in title.lower():
                continue
            results.append({"pageId": p.id, "title": title})
        # Sort alphabetically by title for stable, scannable output.
        results.sort(key=lambda r: r["title"].lower())
        return MockSpeaker(content=str({"pages": results, "count": len(results)}))


__all__ = ["WikiListPagesArgs", "WikiListPagesTool"]
