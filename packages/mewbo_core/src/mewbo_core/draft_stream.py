"""Token-streaming draft synthesis — ``DraftStreamer``.

A single async ``model.astream()`` round-trip — NO ``ToolUseLoop``, NO
``Orchestrator``, NO tools bound (tool-light).  Designed for sub-500 ms
time-to-first-token on short-context synthesis tasks.

Grounding is caller-supplied as a pre-formatted ``context`` string, keeping this
module graph-free (layering DAG invariant: ``mewbo_core`` never imports
``mewbo_graph``).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from langchain_core.messages import HumanMessage, SystemMessage

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.llm import build_chat_model

logging = get_logger(name="core.draft_stream")


def _extract_text_delta(chunk: object) -> str:
    """Extract the text delta from an ``AIMessageChunk``.

    ``.content`` may be a ``str`` (most providers) or a list of content blocks
    (Anthropic / multimodal providers).  We concatenate all ``"text"``-typed
    blocks and skip non-text blocks such as ``"tool_use"``/``"thinking"``.

    Returns an empty string for any unrecognised shape — the caller skips empty
    deltas so no garbage is yielded.
    """
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                # "tool_use", "thinking", "image", … → skip
        return "".join(parts)
    return ""


class DraftStreamer:
    """Atomic streaming LLM client — one ``astream()`` round-trip, no tools.

    Constructor injects the model name; each :meth:`astream` call is stateless
    and safe for concurrent use (a new model instance is built per call to avoid
    shared mutable state inside ``ChatLiteLLM``).

    Args:
        model_name: LiteLLM model name (e.g. ``"openai/gpt-4o-mini"``).  When
            ``None`` the configured ``llm.default_model`` is used (same
            resolution as :class:`~mewbo_core.structured_synthesis.StructuredSynthesizer`).
    """

    def __init__(self, *, model_name: str | None = None) -> None:
        """Store the (possibly None) model name for lazy resolution on each call."""
        self.model_name = model_name

    async def astream(
        self,
        query: str,
        *,
        context: str = "",
    ) -> AsyncIterator[str]:
        """Stream LLM token deltas for *query*.

        Args:
            query: Natural-language question / instruction.
            context: Pre-formatted grounding context (empty = no grounding).
                When non-empty it is injected as a ``SystemMessage`` before the
                user turn.  The model MUST NOT have any tools bound (tool-light).

        Yields:
            Non-empty text delta strings as they arrive from the model.  The
            concatenation of all yielded deltas is equal to the text the model
            would return via a non-streaming ``ainvoke`` call.

        Note:
            A new :func:`~mewbo_core.llm.build_chat_model` instance is created
            on each call — no ``bind_tools`` is ever called (tool-light
            invariant).
        """
        # Resolve None → configured default (same pattern as StructuredSynthesizer)
        resolved_model = self.model_name or str(
            get_config_value("llm", "default_model", default="") or ""
        )
        model = build_chat_model(model_name=resolved_model)
        # NEVER call model.bind_tools() — this is the tool-light invariant.

        messages: list[SystemMessage | HumanMessage] = []
        if context:
            messages.append(SystemMessage(content=context))
        messages.append(HumanMessage(content=query))

        logging.debug(
            "DraftStreamer.astream: model={} context_len={} query_len={}",
            resolved_model,
            len(context),
            len(query),
        )

        async for chunk in model.astream(messages):
            delta = _extract_text_delta(chunk)
            if delta:
                yield delta


__all__ = ["DraftStreamer"]
