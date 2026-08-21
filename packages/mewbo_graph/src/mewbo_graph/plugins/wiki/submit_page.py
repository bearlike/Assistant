"""``wiki_submit_page`` SessionTool — persist a single wiki page + track count."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import ProgressReporter, emit_log, emit_phase_once
from mewbo_graph.plugins.wiki.clone import _resolve_runtime  # noqa: F401 — per-module test seam
from mewbo_graph.plugins.wiki.step_plans import (  # noqa: F401 — compatibility export
    PAGES_STEPS,
    planned_steps_for_slug,
)
from mewbo_graph.wiki.types import IndexingJob

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

        # The ``pages`` phase is a page-writer FAN-OUT with no boundary tool, so
        # writing a page IS the phase starting: the first writer to arrive moves
        # the job, the rest are no-ops. Its predecessor (wiki_commit_plan) must
        # NOT stamp it on the way out: that reports the phase as underway for a
        # fan-out that has not spawned, so a run dying in that gap reads as
        # writing pages it never began.
        # The phase is stamped before the resume guard on purpose: unlike the
        # whole-phase skips, this one refuses ONE page while the rest of the
        # fan-out keeps writing, so the pages phase really is underway.
        emit_phase_once(ctx, "pages")
        progress = ProgressReporter(ctx)
        progress.declare(planned_steps_for_slug(ctx.store, ctx.slug, "pages"))

        # 3. Checkpoint-aware resume, per page. ``graph``/``enrich``/``plan`` are
        # whole-phase skips; ``pages`` is decided page by page, and until this
        # guard existed the rule lived only as prose in ``ResumePlan.summary()``
        # — an instruction to a model, not an invariant. A re-written page costs
        # a full page-writer generation and overwrites work that was already
        # correct.
        rp = ctx.resume_plan
        if rp is not None and page_id in rp.pages_done:
            emit_log(ctx, f"Page already written ({page_id}) — skipped on resume")
            return MockSpeaker(content=str({
                "submitted": page_id,
                "skipped": "page already written — reused on resume",
            }))

        # 4. Claim the page for THIS job — one atomic write that both dedups and
        # counts. Keying on the slug instead ("does a page with this id exist
        # for the slug") answers yes for every page a previous index of the same
        # repository wrote, so on a REFRESH nothing counts as new: the counter
        # stays at zero, no ``page_committed`` event fires, and the progress bar
        # is dead for the entire re-index.
        #
        # BEFORE the save, not after, and the order is load-bearing: a job with
        # no claim record yet seeds one from page ATTRIBUTION, and saving first
        # would put THIS page into that seed — so the page would be read back as
        # already claimed and never counted.
        # A SLUG-bound ctx has no job (a maintainer session editing an already
        # indexed project — ``WikiJobCtx.job_bound``), and every counter in this
        # step belongs to one: the claim set is keyed by job id, and its count
        # IS the job's submitted-pages progress. There is no progress to report
        # for a one-off edit, so the claim is skipped rather than filed under an
        # empty id, where it would both mean nothing and dedupe against nothing.
        claim = (
            ctx.store.claim_job_page(ctx.slug, ctx.job_id, page_id)
            if ctx.job_bound
            else None
        )
        submitted_count = claim.count if claim is not None else 0

        # 5. Build the WikiPage (toc + nav deferred — front-end derives from headings).
        page = _build_wiki_page(page_id, args)

        # 6. Persist the page (overwrites if same page_id), attributed to the
        # commit this job indexed — provenance only; pages supersede by the
        # finalize plan-prune, not by commit.
        # ``job_id or None`` keeps attribution honest for a jobless edit: the
        # column means "the index that wrote this page", and an empty string
        # names an index that never existed.
        ctx.store.save_page(
            ctx.slug, page, commit_sha=ctx.commit_sha, job_id=ctx.job_id or None
        )

        # 7. Re-derive this page's doc note from the body just written. Without
        # it a page keeps its ORIGINAL content hash and anchors for the life of
        # the project, so a staleness verdict can never return to ``keep`` and
        # an act phase rewrites the same pages on every refresh, forever.
        self._restamp_doc_note(ctx, page)

        # 8. Emit page-level progress + log line for the indexing timeline.
        # ``index`` is 0-based; ``claim.count`` is the count INCLUDING this page,
        # so subtract 1. ``totalPages`` comes from the planned page list. Skipped
        # wholesale without a job, for the reason step 4 gives.
        if claim is None:
            return MockSpeaker(content=str({"submitted": page_id}))
        plan = ctx.store.get_job_plan(ctx.job_id) or []
        total_pages = len(plan)
        if claim.is_new:
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
                ctx.store.update_job(
                    ctx.job_id,
                    pages_submitted=submitted_count,
                    last_progress_at=IndexingJob.format_stamp(
                        datetime.now(timezone.utc)
                    ),
                )
            except Exception:
                pass
            # An empty plan names no denominator. A page outside it is still
            # persisted and counted so the divergence stays visible, but a
            # fraction of 1/0 is not a valid progress measurement. The aggregate
            # opened by plan commit remains running until its final planned page.
            progress.report("pages.write", submitted_count, total_pages or None, detail=page_id)
            if total_pages and submitted_count >= total_pages:
                progress.finish("pages.write")
            if total_pages:
                emit_log(ctx, f"Wrote page {submitted_count}/{total_pages}: {page_id}")
            else:
                emit_log(ctx, f"Wrote page: {page_id}")

        return MockSpeaker(content=str({"submitted": page_id, "pages_total": submitted_count}))

    @staticmethod
    def _restamp_doc_note(ctx: Any, page: Any) -> None:
        """Re-stamp the page's ``DocPageNote``; never fail the page write for it.

        Degrades the way the refresh path's own embedding step does: the page is
        persisted either way, and a note that could not be re-derived costs one
        redundant staleness verdict on the next refresh — where failing the tool
        would cost a whole page-writer generation.
        """
        try:
            # INSIDE the try on purpose: the docstring above promises this never
            # fails a page write, and an ImportError is one of the ways it could.
            # ``refresh``'s module imports are light today, so this is a
            # guarantee held by construction rather than by luck.
            from mewbo_graph.wiki.refresh import DocStalenessPlanner  # noqa: PLC0415

            DocStalenessPlanner(store=ctx.store).restamp_page(
                ctx.slug, page, commit=ctx.commit_sha
            )
        except Exception as exc:  # noqa: BLE001 — a note is never worth the page
            logging.warning(
                "wiki_submit_page {}: doc note not re-stamped for {}; its "
                "staleness verdict stays as the previous refresh left it. "
                "Reason: {}",
                ctx.slug,
                page.id,
                exc,
            )


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
