"""``wiki_read_page`` SessionTool — fetch full wiki page by ID."""
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

logging = get_logger(name="core.builtin_plugins.wiki.read_page")


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


class WikiReadPageTool:
    """SessionTool: return the full WikiPage for the given page ID."""

    tool_id = "wiki_read_page"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiReadPageArgs, name="wiki_read_page"
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
        """Execute a ``wiki_read_page`` tool call."""
        # 1. Resolve runtime and QA ctx.
        runtime = _resolve_runtime()
        ctx = resolve_qa_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        # 2. Parse and validate args.
        # model_validate with by_alias=True accepts both "pageId" and "page_id"
        # because populate_by_name=True is set on the model config.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiReadPageArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        # 3. Fetch page.
        page = ctx.store.get_page(ctx.slug, args.page_id)
        if page is None:
            return _err_result("not_found", f"page '{args.page_id}' not found in slug '{ctx.slug}'")

        # 4. Return full WikiPage dump.
        return MockSpeaker(content=str(page.model_dump(by_alias=True)))


__all__ = ["WikiReadPageArgs", "WikiReadPageTool"]
