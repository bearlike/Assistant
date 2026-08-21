"""``wiki_commit_plan`` SessionTool — persist the page plan + emit finalizing event."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import ProgressReporter, emit_log, emit_phase
from mewbo_graph.plugins.wiki.clone import _resolve_runtime  # noqa: F401 — per-module test seam
from mewbo_graph.plugins.wiki.step_plans import (  # noqa: F401 — compatibility export
    PLAN_STEPS,
    planned_steps_for_slug,
)
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
    landingPageId: str | None = Field(  # noqa: N815
        default=None,
        description=(
            "The page id the finished wiki lands on. Pass it HERE, with the plan"
            " that contains it: it is checked against this plan's own ids"
            " immediately, so a wrong id fails in milliseconds instead of at"
            " wiki_finalize, after every page has been written."
        ),
    )

    @model_validator(mode="after")
    def _plan_is_internally_consistent(self) -> WikiCommitPlanArgs:
        """Refuse a plan that cannot be completed as written.

        Both checks are decidable from the plan ALONE, which is the point: each
        would otherwise be discovered only at the far end of the run.

        - A landing page the plan does not contain fails ``wiki_finalize`` with
          "not found in submitted pages" — after the whole pages fan-out has
          run, roughly 25 minutes into an index that was already doomed when the
          plan was committed.
        - A duplicated page id makes the progress denominator unreachable: the
          plan length counts the duplicate but a page id can only ever be
          written (and counted) once, so the bar stops short of its own total
          forever.
        """
        ids = [p.id for p in self.pages]
        duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
        if duplicates:
            raise ValueError(f"plan contains duplicate page ids: {duplicates}")
        if self.landingPageId is not None and self.landingPageId not in set(ids):
            raise ValueError(
                f"landingPageId '{self.landingPageId}' is not one of this plan's "
                f"page ids ({sorted(ids)})"
            )
        return self


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
        progress = ProgressReporter(ctx)
        progress.declare(planned_steps_for_slug(ctx.store, ctx.slug, "plan"))

        # Checkpoint-aware resume: reuse the plan the interrupted
        # index already committed so the reused graph stays consistent with it.
        # Done-detection lives ONLY in ResumePlan (DRY); this is the one-line
        # short-circuit.
        rp = ctx.resume_plan
        if rp is not None and rp.should_skip("plan"):
            progress.skip_group("plan", note="reused on resume")
            emit_log(ctx, f"Plan already committed ({rp.total_pages} pages) — skipped on resume")
            return MockSpeaker(content=str({
                "committed": rp.total_pages,
                "skipped": "plan already committed — reused on resume",
            }))

        # 3. Persist the plan as a sidecar (not as a field on IndexingJob).
        # by_alias keeps the camelCase wire shape the LLM/page-writer use
        # (``relevantFiles``/``relatedPages``).
        with progress.step("plan.compose"):
            plan_dicts = [p.model_dump(by_alias=True) for p in args.pages]
        total_pages = len(args.pages)
        with progress.step("plan.validate", total=total_pages) as step:
            step.advance(total_pages, total_pages)
        with progress.step("plan.persist"):
            ctx.store.save_job_plan(ctx.job_id, plan_dicts)
        # ``pages.write`` is one aggregate declaration. Its count becomes known
        # only after this plan commits; prime its denominator without claiming the
        # pages phase began before a writer actually arrives.
        progress.set_total("pages.write", total_pages)

        # 4. Read current job counts for the event payload.
        job = ctx.store.get_job(ctx.job_id)
        scanned_count = job.scanned_count if job is not None else 0
        total_count = job.total_count if job is not None else 0

        # 5. Update job status to finalizing and emit progress events.
        # ``total_pages`` is also persisted on the snapshot so the landing
        # card can render the page-bar denominator without subscribing to
        # SSE. ``landing_page_id`` rides the same write when the plan names one,
        # so a run that dies before finalize still records where it meant to
        # land — and the id is already known to be one of the planned pages.
        job_fields: dict[str, Any] = {"status": "finalizing", "total_pages": total_pages}
        if args.landingPageId is not None:
            job_fields["landing_page_id"] = args.landingPageId
        ctx.store.update_job(ctx.job_id, **job_fields)
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

        # ``pages`` is deliberately NOT stamped here. Committing a plan is not the
        # same event as writing one — the page-writers stamp it themselves on the
        # first wiki_submit_page, so the phase marks work that actually started.
        return MockSpeaker(content=str({"committed": total_pages}))


__all__ = [
    "WikiCommitPlanArgs",
    "WikiCommitPlanTool",
]
