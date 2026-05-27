"""``scg_memory`` SessionTool — the learned-layer flywheel over the SCG.

One tool, two operations over the shared #13 memory substrate (corpus
``connector``) via :class:`ScgMemoryBridge`:

* ``operation="write"`` — deposit a durable connector insight (a data-location
  win, a failure constraint, a resolved binding, a learned edge-weight hint)
  anchored to ``source_keys``. This is the search agent's deposit step: facts
  written now bias every future traversal (the Exa Fast/Auto/Deep flywheel).
* ``operation="read"`` — embed *query* and return the top-*k* connector insights
  to seed traversal. Read-only.

There is ZERO re-implementation of the atomic-note / anchor / dedup machinery —
the bridge routes through the shared ``InsightIngestor`` and the store's
``memory_vector_search`` ANN seam. The in-session deposit is deterministic (no
LLM dedup tier-3 — ``llm`` is not injected).

Security invariant (spec §6): a connector insight is a propositional fact about
*reachability* — never a record value, token, or credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mewbo_core.common import MockSpeaker, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_graph.plugins.scg._core import (
    SCG_CORE_UNAVAILABLE,
    ScgCore,
    SessionToolBase,
    err_result,
    ok_result,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

    from mewbo_graph.scg.memory_bridge import ScgMemoryBridge


class ScgMemoryArgs(BaseModel):
    """Read or write a connector insight over the SCG learned layer."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["read", "write"] = Field(
        description="``write`` deposits an insight; ``read`` retrieves the top-k."
    )
    query: str | None = Field(
        default=None,
        description="``read``: the NL query to retrieve connector insights for.",
    )
    content: str | None = Field(
        default=None,
        description=(
            "``write``: one durable, ≤200-char, single-claim connector fact "
            "(NO pronouns). Never a record value, token, or credential."
        ),
    )
    source_keys: list[str] = Field(
        default_factory=list,
        description=(
            "``write``: the ``<source_id>#<Qualified.Name>`` anchors the insight "
            "hangs off (must resolve to live SCG nodes to be retrievable)."
        ),
    )
    k: int = Field(default=10, ge=1, le=50, description="``read``: max insights.")

    @model_validator(mode="after")
    def _check_operation_fields(self) -> ScgMemoryArgs:
        """Require the fields each operation needs (fail closed at the boundary)."""
        if self.operation == "write":
            if not (self.content and self.content.strip()):
                raise ValueError("operation=write requires non-empty `content`")
            if not self.source_keys:
                raise ValueError("operation=write requires at least one `source_key`")
        elif not (self.query and self.query.strip()):
            raise ValueError("operation=read requires non-empty `query`")
        return self


class ScgMemoryTool(SessionToolBase):
    """SessionTool: deposit / retrieve connector insights (``ScgMemoryBridge``)."""

    tool_id = "scg_memory"
    modes = DEFAULT_SESSION_TOOL_MODES
    # A write mutates the memory substrate; the loop's per-call partitioning
    # treats this conservatively. Reads are safe but the tool does both, so it
    # is NOT marked concurrency_safe.
    concurrency_safe = False
    schema = pydantic_to_openai_tool(ScgMemoryArgs, name="scg_memory")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Dispatch read/write to the SCG memory bridge."""
        try:
            args = ScgMemoryArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            from mewbo_graph.scg.memory_bridge import (  # noqa: PLC0415
                CONNECTOR_SLUG,
            )

            store = ScgCore.store()
            bridge = ScgCore.memory_bridge(store)
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001
            # The memory substrate could not be constructed (e.g. the configured
            # Mongo backend is unreachable). The flywheel is best-effort — degrade
            # to a clean non-fatal warning so the search/map run continues rather
            # than failing on a purely-additive deposit/read.
            return self._unavailable(args.operation, str(exc))
        try:
            if args.operation == "write":
                return self._write(bridge, CONNECTOR_SLUG, args)
            return self._read(bridge, CONNECTOR_SLUG, args)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))

    # -- operations ---------------------------------------------------------

    @staticmethod
    def _unavailable(operation: str, reason: str) -> MockSpeaker:
        """A clean, non-fatal result when the memory substrate can't be built.

        The flywheel is purely additive — a write/read that can't reach the
        substrate must NOT fail the surrounding search/map run. Reports the
        deposit as skipped (write) or an empty result (read) with a warning, so
        the agent sees the degradation without an error path.
        """
        payload: dict[str, object] = {
            "operation": operation,
            "warning": f"connector memory unavailable: {reason}",
        }
        if operation == "write":
            payload.update({"ok": False, "claims": [], "anchors": []})
        else:
            payload.update({"count": 0, "insights": []})
        return ok_result(payload)

    @staticmethod
    def _write(bridge: ScgMemoryBridge, slug: str, args: ScgMemoryArgs) -> MockSpeaker:
        """Deposit one connector insight anchored to ``source_keys``.

        ``write_insight`` returns an :class:`IngestResult` (one claim per atomic
        fact); report its ``ok`` flag + the per-claim ``{action, node_id}`` so the
        agent sees whether the fact was created/merged/rejected.
        """
        result = bridge.write_insight(
            slug,
            args.content or "",
            source_keys=list(args.source_keys),
        )
        return ok_result(
            {
                "operation": "write",
                "ok": result.ok,
                "claims": [
                    {"action": c.action, "node_id": c.node_id} for c in result.claims
                ],
                "anchors": list(args.source_keys),
            }
        )

    @staticmethod
    def _read(bridge: ScgMemoryBridge, slug: str, args: ScgMemoryArgs) -> MockSpeaker:
        """Embed the query and return the top-k connector insights."""
        qvec = ScgCore.embedder().embed_query(args.query or "")
        notes = bridge.read_insights(slug, qvec, k=args.k)
        return ok_result(
            {
                "operation": "read",
                "count": len(notes),
                "insights": [
                    {"node_id": n.node_id, "content": n.content} for n in notes
                ],
            }
        )


__all__ = ["ScgMemoryArgs", "ScgMemoryTool"]
