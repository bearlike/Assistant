"""``scg_introspect_source`` SessionTool — accept + persist a source descriptor.

The first MAP step: the mapper agent hands the connector's raw self-description
(an OpenAPI doc, an MCP tool list, a GraphQL SDL, a SQL schema…) to this tool,
which validates it into a :class:`SourceDescriptor` and persists it on the SCG
store so the later ``scg_build_structure`` / ``scg_link_entities`` passes can
find it. No network: the agent is responsible for *fetching* the descriptor
natively (the connector's own tools); this tool only *accepts* it — mirroring
the spec's "the connector's real return is the only check" stance.

Security invariant (spec §6): the ``raw`` payload is a schema descriptor only —
auth lives in the connector config, never here. This tool persists exactly what
it is given and copies no token/credential.
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


class ScgIntrospectSourceArgs(BaseModel):
    """Accept a connector's raw self-description as an SCG source descriptor."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(
        min_length=1, description="Stable connector id (e.g. ``github``)."
    )
    source_type: str = Field(
        min_length=1,
        description="Descriptor kind: ``openapi`` | ``mcp_tool_list`` | ``text``.",
    )
    raw: dict[str, object] = Field(
        description=(
            "The raw descriptor payload (OpenAPI doc / MCP tool list / SDL). "
            "Schema only — never a token, credential, or record value."
        )
    )


class ScgIntrospectSourceTool(SessionToolBase):
    """SessionTool: validate + persist one connector source descriptor."""

    tool_id = "scg_introspect_source"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Writes the descriptor namespace — exclusive (one re-map at a time).
    concurrency_safe = False
    schema = pydantic_to_openai_tool(
        ScgIntrospectSourceArgs, name="scg_introspect_source"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate the descriptor and upsert it onto the SCG store."""
        try:
            args = ScgIntrospectSourceArgs.model_validate(
                action_step.tool_input or {}
            )
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            store = ScgCore.store()
            descriptor = ScgCore.source_descriptor(
                source_id=args.source_id,
                source_type=args.source_type,
                raw=args.raw,
            )
            store.upsert_source(descriptor)
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))
        return ok_result(
            {
                "accepted": True,
                "source_id": args.source_id,
                "source_type": args.source_type,
            }
        )


__all__ = ["ScgIntrospectSourceArgs", "ScgIntrospectSourceTool"]
