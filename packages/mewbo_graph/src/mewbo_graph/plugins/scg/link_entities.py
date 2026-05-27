"""``scg_link_entities`` SessionTool — cross-source RESOLVES_TO + param edges.

The third MAP step: wire the *cross-source* edges no single provider can see.
Two deterministic, abstain-by-default passes over the persisted graph:

* :meth:`ScgParser.link_sources` runs the :class:`TypeAligner` to deposit
  weighted, provenanced ``RESOLVES_TO`` *hypothesis* edges between matching
  ``entity_type`` nodes across the given sources (e.g. ``Jira.Issue <=>
  Linear.Ticket``) — never an asserted truth; band pairs abstain without an LLM.
* :meth:`ScgParser.compute_param_edges` runs the In-N-Out (``2509.01560``)
  producer→consumer join, wiring ``CONSUMES`` edges so traversal can chain ops
  into qualified multi-hop paths.

Both are no-ops on an empty/single-source graph — never a raise. Instance-level
ER is explicitly NOT here: that happens online inside the probe agent over live
data. This step owns only the offline, type-level schema correspondence.
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


class ScgLinkEntitiesArgs(BaseModel):
    """Wire cross-source RESOLVES_TO + producer→consumer edges over the SCG."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Source ids to align pairwise. Empty/one source ⇒ the type-align "
            "pass is a no-op; the param-edge pass still runs over all sources."
        ),
    )


class ScgLinkEntitiesTool(SessionToolBase):
    """SessionTool: ``ScgParser.link_sources`` + ``compute_param_edges``."""

    tool_id = "scg_link_entities"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Deposits cross-source edges — exclusive against concurrent re-maps.
    concurrency_safe = False
    schema = pydantic_to_openai_tool(ScgLinkEntitiesArgs, name="scg_link_entities")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Run the type-align + param-edge passes; report the edge counts."""
        try:
            args = ScgLinkEntitiesArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            parser = ScgCore.parser(ScgCore.store())
            resolves_to = parser.link_sources(args.source_ids)
            consumes = parser.compute_param_edges()
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))
        return ok_result(
            {
                "resolvesToCount": len(resolves_to),
                "consumesCount": len(consumes),
                "source_ids": list(args.source_ids),
            }
        )


__all__ = ["ScgLinkEntitiesArgs", "ScgLinkEntitiesTool"]
