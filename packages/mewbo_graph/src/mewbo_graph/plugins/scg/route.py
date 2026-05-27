"""``scg_route`` SessionTool — the cheap query→route over the SCG (read-only).

The graph's only query-time job: control routing. Given a natural-language
sub-query, :meth:`ScgRouter.route` embeds it, vector-searches seed nodes,
expands one hop along capability/route edges, and returns up to *k* ranked
:class:`RouteRecipe`s scored by a deterministic, zero-LLM ``cosine + weight``.
The search agent consumes the ranked recipes and fans one probe sub-agent out
per recipe — spending agents is the *agent's* job, not this tool's (route first,
traverse second; HippoRAG2's cheap-pre-rank stance).

Read-only: no graph mutation, so it is ``concurrency_safe`` (the search agent
may route several decomposed sub-queries in parallel).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mewbo_core.common import MockSpeaker, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_graph.plugins.scg._core import (
    SCG_CORE_UNAVAILABLE,
    ScgCore,
    SessionToolBase,
    err_result,
    ok_result,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep


class ScgRouteArgs(BaseModel):
    """Route a natural-language sub-query to ranked SCG RouteRecipes."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, description="The natural-language sub-query.")
    k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Max recipes to return (the probe-count fan-out knob).",
    )


class ScgRouteTool(SessionToolBase):
    """SessionTool: ``ScgRouter.route`` → ranked RouteRecipes as JSON."""

    tool_id = "scg_route"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Read-only — safe to route several decomposed sub-queries in parallel.
    concurrency_safe = True
    schema = pydantic_to_openai_tool(ScgRouteArgs, name="scg_route")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Route the query and return up to *k* ranked recipes (best first)."""
        try:
            args = ScgRouteArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            recipes = ScgCore.router(ScgCore.store()).route(args.query, k=args.k)
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))
        return ok_result(
            {
                "count": len(recipes),
                "recipes": [r.model_dump(mode="json") for r in recipes],
            }
        )


__all__ = ["ScgRouteArgs", "ScgRouteTool"]
