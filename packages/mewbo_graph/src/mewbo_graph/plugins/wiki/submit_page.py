"""``wiki_submit_page`` SessionTool — persist a single wiki page + track count."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import emit_log
from mewbo_graph.plugins.wiki.clone import _resolve_runtime  # noqa: F401 — per-module test seam

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.submit_page")


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


class WikiSubmitPageTool(WikiSessionTool):
    """SessionTool: persist a wiki page; track submitted count (idempotent re-submit)."""

    tool_id = "wiki_submit_page"
    args_cls = WikiSubmitPageArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(WikiSubmitPageArgs, name="wiki_submit_page")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_submit_page`` tool call."""
        # 1. Resolve runtime and job ctx.
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        args = self._parse_args(WikiSubmitPageArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

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
    from mewbo_graph.wiki.types import Frontmatter, WikiPage  # noqa: PLC0415

    # Build a minimal Frontmatter from the dict; ignore unknown fields gracefully.
    fm_data = dict(args.frontmatter)
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
