"""``wiki_emit_block`` SessionTool — validate and persist a single answer block."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.emit_block")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiEmitBlockArgs(BaseModel):
    """Arguments for ``wiki_emit_block``."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, description="Zero-based block index within the answer.")
    block: dict[str, Any] = Field(
        description=(
            "Block payload — must be a valid discriminated Block "
            "(kind: p|h2|h3|hr|ul|accordion|sources|table|diagram)."
        )
    )


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiEmitBlockTool(WikiSessionTool):
    """SessionTool: validate a block and persist block_open + block_close events."""

    tool_id = "wiki_emit_block"
    args_cls = WikiEmitBlockArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiEmitBlockArgs, name="wiki_emit_block"
    )

    def should_terminate_run(self) -> bool:
        """The terminal ``sources`` block is the answer's accept state.

        Emitting it submits the finished answer (the ``EmitStructuredResponseTool``
        pattern), so the loop stops cleanly here instead of spending a trailing
        turn. Set by :meth:`handle` once the required sources block lands.
        """
        return getattr(self, "_terminate", False)

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_emit_block`` tool call."""
        # 1. Resolve runtime and QA ctx.
        ctx = self._qa_ctx()
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")
        # A grounded structured-response session resolves a slug-only ctx
        # (``answer_id is None``) — it has no QA event log to write blocks into.
        # Emitting answer blocks is QA-only; refuse rather than NPE on the
        # ``load_qa_events``/``append_qa_event`` calls below.
        if ctx.answer_id is None:
            return _err_result(
                "internal", "wiki_emit_block requires a registered QA answer"
            )

        # 2. Parse and validate outer args.
        args = self._parse_args(WikiEmitBlockArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Validate the block payload against the discriminated union.
        try:
            from mewbo_graph.wiki.types import BlockUnion  # noqa: PLC0415
            validated_block = BlockUnion.model_validate(args.block)
        except Exception as exc:
            return _err_result("validation", f"invalid block: {exc}")

        # 4. Duplicate-index guard — scan existing QA events for a prior block_open
        #    at this index. Linear scan is fine for ≤30 blocks per answer.
        existing_events = ctx.store.load_qa_events(ctx.answer_id)
        for ev in existing_events:
            if ev.get("type") == "block_open" and ev.get("index") == args.index:
                return _err_result(
                    "validation",
                    f"block already emitted at index {args.index}",
                )

        # 5. Persist block_open then block_close.
        block_dict = validated_block.model_dump(by_alias=True)
        # Re-scheme any wiki-page citation the model emitted as a bare path
        # (``wiki:<page-id>``) BEFORE it lands on the log — at this one seam it
        # fixes both the live stream and the reconciled snapshot, so the console's
        # file SourceCard never 404s on a page ref (#70).
        if block_dict.get("kind") == "sources":
            from mewbo_graph.wiki.qa import QaFinalizer  # noqa: PLC0415

            block_dict = QaFinalizer.tag_page_citations(block_dict, ctx.store, ctx.slug)
        ctx.store.append_qa_event(ctx.answer_id, {
            "type": "block_open",
            "index": args.index,
            "block": block_dict,
        })
        ctx.store.append_qa_event(ctx.answer_id, {
            "type": "block_close",
            "index": args.index,
        })

        # 6. The sources block is required LAST and IS the answer's accept state:
        #    submit (reconcile snapshot + terminal ``complete`` — including the
        #    snapshot's ``status``, see ``QaFinalizer.close``) and terminate the
        #    run. The on_session_end net still covers a run that halts before it.
        if block_dict.get("kind") == "sources":
            from mewbo_graph.wiki.qa import QaFinalizer  # noqa: PLC0415
            QaFinalizer.close(ctx.store, ctx.answer_id)
            self._terminate = True

        return MockSpeaker(content=str({"ok": True, "index": args.index}))


__all__ = ["WikiEmitBlockArgs", "WikiEmitBlockTool"]
