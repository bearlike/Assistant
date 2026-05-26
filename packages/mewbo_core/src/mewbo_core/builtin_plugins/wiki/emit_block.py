"""``wiki_emit_block`` SessionTool — validate and persist a single answer block."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import resolve_qa_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.emit_block")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any | None:
    """Late-import the wiki API runtime. Returns None if the API is not available."""
    try:
        from mewbo_api.wiki.routes import _runtime  # noqa: PLC0415
        return _runtime
    except ImportError:
        return None


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


class WikiEmitBlockTool:
    """SessionTool: validate a block and persist block_open + block_close events."""

    tool_id = "wiki_emit_block"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiEmitBlockArgs, name="wiki_emit_block"
    )

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_emit_block`` tool call."""
        # 1. Resolve runtime and QA ctx.
        runtime = _resolve_runtime()
        ctx = resolve_qa_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")

        # 2. Parse and validate outer args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiEmitBlockArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        # 3. Validate the block payload against the discriminated union.
        try:
            from mewbo_api.wiki.types import BlockUnion  # noqa: PLC0415
            validated_block = BlockUnion.model_validate(args.block)
        except (ValidationError, Exception) as exc:
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
        ctx.store.append_qa_event(ctx.answer_id, {
            "type": "block_open",
            "index": args.index,
            "block": block_dict,
        })
        ctx.store.append_qa_event(ctx.answer_id, {
            "type": "block_close",
            "index": args.index,
        })

        return MockSpeaker(content=str({"ok": True, "index": args.index}))


__all__ = ["WikiEmitBlockArgs", "WikiEmitBlockTool"]
