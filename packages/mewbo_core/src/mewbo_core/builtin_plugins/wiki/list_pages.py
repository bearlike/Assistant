"""``wiki_list_pages`` — page catalog for the wiki-qa agent.

This is the agent's "front door". Before deciding what to search for,
the agent can fetch the list of every page in the wiki and pick the
most plausibly-relevant one by title. Way cheaper than a BM25 search
when the question maps cleanly onto a single page.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import resolve_qa_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.list_pages")


def _resolve_runtime():
    try:
        from mewbo_api.wiki.routes import _runtime  # noqa: PLC0415
        return _runtime
    except ImportError:
        return None


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


class WikiListPagesTool:
    """SessionTool: list every wiki page for the QA session's slug."""

    tool_id = "wiki_list_pages"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiListPagesArgs, name="wiki_list_pages"
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
        """Return ``True`` once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_list_pages`` tool call."""
        runtime = _resolve_runtime()
        ctx = resolve_qa_ctx(self._session_id, runtime) if runtime else None
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiListPagesArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

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
