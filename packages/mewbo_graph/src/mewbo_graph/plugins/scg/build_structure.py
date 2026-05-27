"""``scg_build_structure`` SessionTool — parse a source into the persisted SCG.

The second MAP step: dispatch the source's persisted descriptor to its
type-provider via :meth:`ScgParser.parse_source`, which clean-re-maps (deletes
the source's prior nodes first), persists nodes/edges/recipes, and embeds every
node (best-effort). Returns the node/edge/recipe counts the mapper reports as
phase progress.

The descriptor must already be on the store (via ``scg_introspect_source``) so
this tool resolves it by ``source_id`` — keeping the two MAP steps decoupled
exactly as the wiki ``scan`` → ``build_graph`` pair is. Embedding failure is
non-fatal (degrades to a structure-only SCG; mirrors the wiki BM25 fallback).
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


class ScgBuildStructureArgs(BaseModel):
    """Parse one already-introspected source into the persisted SCG."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(
        min_length=1,
        description="Connector id whose persisted descriptor to parse.",
    )


class ScgBuildStructureTool(SessionToolBase):
    """SessionTool: ``ScgParser.parse_source`` for one persisted descriptor."""

    tool_id = "scg_build_structure"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Clean re-map mutates the whole source namespace — exclusive.
    concurrency_safe = False
    schema = pydantic_to_openai_tool(
        ScgBuildStructureArgs, name="scg_build_structure"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Resolve the persisted descriptor and map it into the SCG."""
        try:
            args = ScgBuildStructureArgs.model_validate(
                action_step.tool_input or {}
            )
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            store = ScgCore.store()
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)

        descriptor = next(
            (d for d in store.list_sources() if d.source_id == args.source_id),
            None,
        )
        if descriptor is None:
            return err_result(
                "not_found",
                f"no introspected descriptor for source_id={args.source_id!r}; "
                "call scg_introspect_source first",
            )
        try:
            graph = ScgCore.parser(store).parse_source(descriptor)
        except KeyError as ke:
            # No provider registered for this source_type.
            return err_result("unsupported_source_type", str(ke))
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))
        return ok_result(
            {
                "source_id": args.source_id,
                "nodeCount": len(graph.nodes),
                "edgeCount": len(graph.edges),
                "recipeCount": len(graph.recipes),
            }
        )


__all__ = ["ScgBuildStructureArgs", "ScgBuildStructureTool"]
