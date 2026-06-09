"""``wiki_commit_plan`` SessionTool — persist the page plan + emit finalizing event."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase
from mewbo_graph.plugins.wiki.clone import _resolve_runtime  # noqa: F401 — per-module test seam
from mewbo_graph.wiki.types import PagePlan

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.commit_plan")


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiCommitPlanArgs(BaseModel):
    """Arguments for ``wiki_commit_plan``."""

    model_config = ConfigDict(extra="forbid")

    pages: list[PagePlan] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiCommitPlanTool(WikiSessionTool):
    """SessionTool: persist the page plan and emit a finalizing event."""

    tool_id = "wiki_commit_plan"
    args_cls = WikiCommitPlanArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(WikiCommitPlanArgs, name="wiki_commit_plan")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_commit_plan`` tool call."""
        # 1. Resolve runtime and job ctx.
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        args = self._parse_args(WikiCommitPlanArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        emit_phase(ctx, "plan")

        # Checkpoint-aware resume (Gitea #54): reuse the plan the interrupted
        # index already committed so the reused graph stays consistent with it.
        # Done-detection lives ONLY in ResumePlan (DRY); this is the one-line
        # short-circuit. Advance to ``pages`` so the page-writers run next.
        rp = ctx.resume_plan
        if rp is not None and rp.should_skip("plan"):
            emit_log(ctx, f"Plan already committed ({rp.total_pages} pages) — skipped on resume")
            emit_phase(ctx, "pages")
            return MockSpeaker(content=str({
                "committed": rp.total_pages,
                "skipped": "plan already committed — reused on resume",
            }))

        # 3. Persist the plan as a sidecar (not as a field on IndexingJob).
        # by_alias keeps the camelCase wire shape the LLM/page-writer use
        # (``relevantFiles``/``relatedPages``).
        plan_dicts = [p.model_dump(by_alias=True) for p in args.pages]
        ctx.store.save_job_plan(ctx.job_id, plan_dicts)

        # 4. Read current job counts for the event payload.
        job = ctx.store.get_job(ctx.job_id)
        scanned_count = job.scanned_count if job is not None else 0
        total_count = job.total_count if job is not None else 0

        total_pages = len(args.pages)

        # 5. Update job status to finalizing and emit progress events.
        # ``total_pages`` is also persisted on the snapshot so the landing
        # card can render the page-bar denominator without subscribing to
        # SSE.
        ctx.store.update_job(
            ctx.job_id, status="finalizing", total_pages=total_pages
        )
        ctx.store.append_job_event(ctx.job_id, {
            "type": "finalizing",
            "scannedCount": scanned_count,
            "totalCount": total_count,
        })
        ctx.store.append_job_event(ctx.job_id, {
            "type": "plan_committed",
            "totalPages": total_pages,
        })
        emit_log(ctx, f"Plan committed: {total_pages} pages")
        # Surface the planned titles so the timeline isn't silent during
        # the long pages phase — gives users something to watch even
        # before the first page lands.
        titles = [p.title for p in args.pages][:5]
        if titles:
            preview = ", ".join(titles)
            more = "" if len(args.pages) <= 5 else f" (+{len(args.pages) - 5} more)"
            emit_log(ctx, f"Planned pages: {preview}{more}")
        emit_phase(ctx, "pages")

        return MockSpeaker(content=str({"committed": total_pages}))


__all__ = [
    "WikiCommitPlanArgs",
    "WikiCommitPlanTool",
]
