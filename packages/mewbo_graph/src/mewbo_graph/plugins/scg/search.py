"""``agentic_search`` SessionTool — kick off a full Mewbo Search, fetch later.

The high-level counterpart to the low-level graph tools (``scg_route`` /
``scg_observe``): instead of routing and traversing the SCG yourself, hand a
natural-language question to a dedicated ``scg-search`` SESSION that decomposes
it, fans probes across the workspace's connectors, and synthesises a cited
answer. Use the graph tools when you want to inspect reachability directly; use
this when you want a finished, cited answer.

Because a search session can run for minutes, this tool is **async by handle**:

* Call with ``query`` → it starts the run and returns IMMEDIATELY with a
  ``run_id`` and ``status:"processing"`` (it never blocks your loop). A run that
  finishes synchronously, or a recent completed run for the SAME question
  (idempotent — no duplicate launch), comes back fully formed in one call.
* Call again with that ``run_id`` → the last-known cited answer plus the
  timestamp it was computed (``computed_at``), or ``status:"processing"`` while
  it is still running.

The run lives in its own auditable session/record and is driven through the
api-registered :class:`~mewbo_graph.scg.search_launcher.SearchLauncher` seam
(read-only connector grants only — a search never mutates a source).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mewbo_core.common import MockSpeaker, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_graph.plugins.scg._core import (
    SessionToolBase,
    err_result,
    ok_result,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep


class AgenticSearchArgs(BaseModel):
    """Start an agentic search (``query``) or fetch a prior run (``run_id``).

    Exactly ONE of ``query`` / ``run_id`` is required. Starting returns a
    ``run_id`` immediately (the run executes in its own session); re-call with
    that ``run_id`` to retrieve the cited answer once it is ready.
    """

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(
        default=None,
        description=(
            "The natural-language question to search for. Starts a NEW run "
            "(returns a run_id immediately) unless an identical recent query "
            "already has a completed run, which is reused."
        ),
    )
    run_id: str | None = Field(
        default=None,
        description="Retrieve a run started earlier by its run_id (no re-run).",
    )
    workspace: str | None = Field(
        default=None,
        description=(
            "Workspace id or name to search within. Omit to use the only "
            "configured workspace; if several exist you'll get the list to pick "
            "from. Ignored when fetching by run_id."
        ),
    )
    tier: Literal["fast", "auto", "deep"] | None = Field(
        default=None,
        description=(
            "Budget knob — decomposition depth + probe fan-out (fast|auto|deep). "
            "Omit for the configured default. Ignored when fetching by run_id."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> AgenticSearchArgs:
        """Require exactly one of a non-blank ``query`` or ``run_id``."""
        has_query = bool(self.query and self.query.strip())
        has_run = bool(self.run_id and self.run_id.strip())
        if has_query == has_run:
            raise ValueError("provide exactly one of 'query' or 'run_id'")
        return self


class AgenticSearchTool(SessionToolBase):
    """SessionTool: start/fetch a workspace agentic-search run (async by handle)."""

    tool_id = "agentic_search"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Kick-off + snapshot read are both read-only over connectors and over the
    # run store, so several may run in parallel.
    concurrency_safe = True
    schema = pydantic_to_openai_tool(AgenticSearchArgs, name="agentic_search")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Start a run (``query``) or fetch a prior one (``run_id``)."""
        try:
            args = AgenticSearchArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))

        # The launcher seam carries no heavy deps (typing only), so its import
        # never fails on a core-only install; absence is signalled by
        # ``available()`` returning False (no api registered a backend).
        from mewbo_graph.scg.search_launcher import SearchLauncher  # noqa: PLC0415

        if not SearchLauncher.available():
            return err_result(
                "unavailable",
                "Agentic search is not available in this runtime "
                "(no search backend is registered).",
            )

        try:
            if args.run_id:
                snap = SearchLauncher.fetch(args.run_id.strip())
                if snap is None:
                    return err_result(
                        "not_found", f"No search run with id '{args.run_id}'."
                    )
                return ok_result(snap)

            handle = SearchLauncher.start(
                str(args.query), workspace=args.workspace, tier=args.tier
            )
            if handle is None:  # pragma: no cover — available() guarded above
                return err_result("unavailable", "Agentic search backend went away.")
            return ok_result(handle)
        except ValueError as ve:
            # Actionable request errors (unknown/ambiguous workspace, no
            # workspaces configured) carry their guidance in the message.
            return err_result("invalid_request", str(ve))
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))


__all__ = ["AgenticSearchArgs", "AgenticSearchTool"]
