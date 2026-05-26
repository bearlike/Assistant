"""``wiki_commit_plan`` SessionTool — persist the page plan + emit finalizing event."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import emit_log, emit_phase, resolve_job_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result, _resolve_runtime
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.commit_plan")


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class _PagePlanInput(BaseModel):
    """Minimal shape for one page in the plan (mirrors PagePlan in types.py)."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    description: str = ""


class WikiCommitPlanArgs(BaseModel):
    """Arguments for ``wiki_commit_plan``."""

    model_config = ConfigDict(extra="forbid")

    pages: list[_PagePlanInput] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiCommitPlanTool:
    """SessionTool: persist the page plan and emit a finalizing event."""

    tool_id = "wiki_commit_plan"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, Any] = pydantic_to_openai_tool(WikiCommitPlanArgs, name="wiki_commit_plan")

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
        """Execute a ``wiki_commit_plan`` tool call."""
        # 1. Resolve runtime and job ctx.
        runtime = _resolve_runtime()
        ctx = resolve_job_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiCommitPlanArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        emit_phase(ctx, "plan")

        # 3. Persist the plan as a sidecar (not as a field on IndexingJob).
        plan_dicts = [p.model_dump() for p in args.pages]
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
