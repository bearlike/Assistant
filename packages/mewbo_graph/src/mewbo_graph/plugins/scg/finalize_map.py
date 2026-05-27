"""``scg_finalize_map`` SessionTool — close out a map job (counts + emit_phase).

The terminal MAP step: tally the whole-catalog SCG (node/edge/source counts) and
emit the ``finalize`` phase so both the SSE-tailed indexing UI and the
snapshot-polling landing card converge — the dual-write invariant
:class:`MapJobProgress.emit_phase` upholds (the SCG analogue of the wiki
``wiki_finalize`` → ``emit_phase("finalize")``).

``job_id`` is the map-job whose progress to advance; when the API store/runtime
is absent (a core-only install) the phase write is skipped and the counts are
still returned — the structure is already persisted, the phase is cosmetic.
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


class ScgFinalizeMapArgs(BaseModel):
    """Finalize a map job: tally the SCG and emit the ``finalize`` phase."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(
        min_length=1, description="Map-job id whose ``finalize`` phase to emit."
    )


class ScgFinalizeMapTool(SessionToolBase):
    """SessionTool: count the SCG + ``MapJobProgress.emit_phase('finalize')``."""

    tool_id = "scg_finalize_map"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Emits the terminal phase event/snapshot — exclusive.
    concurrency_safe = False
    schema = pydantic_to_openai_tool(ScgFinalizeMapArgs, name="scg_finalize_map")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Tally the persisted SCG, emit the ``finalize`` phase, report counts."""
        try:
            args = ScgFinalizeMapArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            store = ScgCore.store()
            sources = store.list_sources()
            node_count = len(store.query_nodes())
            edge_count = len(store.list_edges())
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))

        # Best-effort phase write — None when the API store/runtime is absent.
        emitted_idx = ScgCore.emit_phase(args.job_id, "finalize")
        return ok_result(
            {
                "complete": True,
                "job_id": args.job_id,
                "sourceCount": len(sources),
                "nodeCount": node_count,
                "edgeCount": edge_count,
                "phaseEmitted": emitted_idx is not None,
            }
        )


__all__ = ["ScgFinalizeMapArgs", "ScgFinalizeMapTool"]
