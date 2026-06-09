"""``wiki_read_page`` SessionTool — fetch full wiki page by ID."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.read_page")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiReadPageArgs(BaseModel):
    """Arguments for ``wiki_read_page``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    page_id: str = Field(alias="pageId", description="Page ID to fetch.")


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiReadPageTool(WikiSessionTool):
    """SessionTool: return the full WikiPage for the given page ID."""

    tool_id = "wiki_read_page"
    args_cls = WikiReadPageArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiReadPageArgs, name="wiki_read_page"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_read_page`` tool call."""
        # 1. Resolve runtime and QA ctx.
        ctx = self._qa_ctx()
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        # 2. Parse and validate args.
        # model_validate accepts both "pageId" and "page_id" because
        # populate_by_name=True is set on the model config.
        args = self._parse_args(WikiReadPageArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Fetch page.
        page = ctx.store.get_page(ctx.slug, args.page_id)
        if page is None:
            return _err_result("not_found", f"page '{args.page_id}' not found in slug '{ctx.slug}'")

        # 4. Return full WikiPage dump.
        self._record_qa_access(ctx, [f"wiki:{args.page_id}"])
        return MockSpeaker(content=str(page.model_dump(by_alias=True)))


__all__ = ["WikiReadPageArgs", "WikiReadPageTool"]
