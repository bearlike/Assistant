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

    from mewbo_graph.scg.memory_bias import ScgMemoryBias
    from mewbo_graph.scg.store import ScgStore
    from mewbo_graph.scg.types import RouteRecipe

# Cap the anchored usage-hints carried per recipe — the route result must stay
# compact (the probe needs a few "how to call this right" notes, not the whole
# learned corpus). Mirrors ScgMemoryBias' own per-key cap.
_MAX_HINTS_PER_RECIPE = 3


class ScgRouteArgs(BaseModel):
    """Route a sub-query to ranked SCG entry pathways (the strategic-search seed).

    The Source Capability Graph is a living map of which connectors can reach what
    (typed hops + a learned-memory layer) — use it to PLAN before you act on any
    task that spans tools/sources. The loop: `scg_route` ranks ENTRY recipes for a
    sub-query (cheap cosine + edge weight, biased by what past tasks learned),
    `scg_observe` reads a node's typed hops so you pick the next step, you ACT
    (run the connector tools / fan out probes), then `scg_memory` deposits — with
    polarity — what worked or dead-ended so future tasks inherit it. This tool is
    step one: pass a natural-language sub-query, get up to `k` recipes (each with
    its source toolbox + any anchored usage hints). Read-only.
    """

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
            store = ScgCore.store()
            # route_with_memory carries the learned-memory bias (#76) so each
            # recipe can surface its anchored usage hints without a second lookup.
            recipes, bias = ScgCore.router(store).route_with_memory(
                args.query, k=args.k
            )
            payload = [self._enrich(store, r, bias) for r in recipes]
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))
        return ok_result({"count": len(recipes), "recipes": payload})

    @staticmethod
    def _enrich(
        store: ScgStore, recipe: RouteRecipe, bias: ScgMemoryBias
    ) -> dict[str, object]:
        """A recipe plus the probe's deterministic tool scope + learned hints.

        ``source_ids`` are the connectors on the pathway; ``source_capabilities``
        are EVERY capability of those sources — the probe's ``allowed_tools``
        scope. A probe scoped to only the step tools cannot chain a lookup
        within its own source (the first live run failed exactly this way), so
        the route result carries the full per-source toolbox and the playbook
        copies it instead of leaving the scope to model inference.

        ``memory_hints`` (#76) are the capped anchored connector insights for the
        pathway — short "how to call this right" parameter-usage guidance the
        orchestrator/probe reads inline, so it never needs a second ``scg_memory``
        read. Absent (omitted) when the learned layer has nothing for the pathway.
        """
        from mewbo_core.tool_registry import mcp_tool_id  # noqa: PLC0415

        data = recipe.model_dump(mode="json")
        source_ids = sorted({step.split("#", 1)[0] for step in recipe.steps})
        per_source = [
            (sid, node.name)
            for sid in source_ids
            for node in store.query_nodes(source_id=sid, kind="capability")
        ]
        data["source_ids"] = source_ids
        data["source_capabilities"] = sorted(name for _, name in per_source)
        # The EXECUTABLE allowlist, derived via the core id convention — the
        # playbook copies this verbatim into the probe spawn's `allowed_tools`.
        # Leaving the source_key→tool-id transformation to model inference made
        # small models pass graph addresses as tool ids (granting nothing) —
        # the run-c52e9597 NO-DATA failure.
        data["allowed_tool_ids"] = sorted(
            {mcp_tool_id(sid, name) for sid, name in per_source}
        )
        hints = bias.hints_for_steps(recipe.steps)[:_MAX_HINTS_PER_RECIPE]
        if hints:
            data["memory_hints"] = [
                {"source_key": h.source_key, "text": h.text} for h in hints
            ]
        return data


__all__ = ["ScgRouteArgs", "ScgRouteTool"]
