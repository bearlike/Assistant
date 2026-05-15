"""``wiki_submit_page`` SessionTool — persist a single wiki page + track count."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import emit_log, resolve_job_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result, _resolve_runtime
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.submit_page")


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiSubmitPageArgs(BaseModel):
    """Arguments for ``wiki_submit_page``."""

    model_config = ConfigDict(extra="forbid")

    pageId: str = Field(  # noqa: N815
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="slug-style page id",
    )
    frontmatter: dict[str, Any]
    body: str = Field(..., min_length=1, description="markdown body (frontmatter stripped)")


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiSubmitPageTool:
    """SessionTool: persist a wiki page; track submitted count (idempotent re-submit)."""

    tool_id = "wiki_submit_page"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, Any] = pydantic_to_openai_tool(WikiSubmitPageArgs, name="wiki_submit_page")

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
        """Execute a ``wiki_submit_page`` tool call."""
        # 1. Resolve runtime and job ctx.
        runtime = _resolve_runtime()
        ctx = resolve_job_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiSubmitPageArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        page_id = args.pageId

        # 3. Determine if this is a new submission or a re-submit (idempotent).
        existing = ctx.store.get_page(ctx.slug, page_id)
        is_new = existing is None

        # 4. Build the WikiPage (toc + nav deferred — front-end derives from headings).
        page = _build_wiki_page(page_id, args)

        # 5. Persist the page (overwrites if same page_id).
        ctx.store.save_page(ctx.slug, page)

        # 6. Increment counter only for genuinely new pages.
        if is_new:
            submitted_count = ctx.store.increment_job_submitted_count(ctx.job_id)
        else:
            submitted_count = ctx.store.get_job_submitted_count(ctx.job_id)

        # 7. Emit page-level progress + log line for the indexing timeline.
        # ``index`` is 0-based; ``submitted_count`` is the count INCLUDING this page,
        # so subtract 1. ``totalPages`` comes from the planned page list.
        plan = ctx.store.get_job_plan(ctx.job_id) or []
        total_pages = len(plan)
        if is_new:
            ctx.store.append_job_event(ctx.job_id, {
                "type": "page_committed",
                "pageId": page_id,
                "index": max(0, submitted_count - 1),
                "totalPages": total_pages,
            })
            # Mirror to snapshot so the landing-page card progresses too —
            # without this the snapshot pegs at the scan phase and the
            # bar never moves through pages.
            try:
                ctx.store.update_job(ctx.job_id, pages_submitted=submitted_count)
            except Exception:
                pass
            if total_pages:
                emit_log(ctx, f"Wrote page {submitted_count}/{total_pages}: {page_id}")
            else:
                emit_log(ctx, f"Wrote page: {page_id}")

        return MockSpeaker(content=str({"submitted": page_id, "pages_total": submitted_count}))


# ---------------------------------------------------------------------------
# Page builder helper
# ---------------------------------------------------------------------------


def _build_wiki_page(page_id: str, args: WikiSubmitPageArgs) -> Any:
    """Construct a WikiPage from the submitted args.

    TOC and nav entries are left empty; the front-end auto-derives them from
    the markdown headings, and a future route can rebuild nav from the page list.
    """
    from mewbo_api.wiki.types import Frontmatter, WikiPage  # noqa: PLC0415

    # Build a minimal Frontmatter from the dict; ignore unknown fields gracefully.
    fm_data = {k: v for k, v in args.frontmatter.items()}
    # Ensure required fields have defaults.
    fm_data.setdefault("title", page_id)
    fm_data.setdefault("slug", page_id)

    try:
        frontmatter = Frontmatter.model_validate(fm_data)
    except Exception:
        frontmatter = Frontmatter(title=fm_data.get("title", page_id), slug=page_id)

    return WikiPage(
        id=page_id,
        title=frontmatter.title,
        frontmatter=frontmatter,
        body=args.body,
        toc=[],
        nav=[],
    )


__all__ = [
    "WikiSubmitPageArgs",
    "WikiSubmitPageTool",
]
