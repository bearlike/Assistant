"""Context compaction with structured summaries and partial mode support."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mewbo_core.common import count_tokens, get_logger
from mewbo_core.config import get_config_value
from mewbo_core.prompt_registry import get_prompt_registry
from mewbo_core.types import EventRecord

logger = get_logger(name="core.compact")


def resolve_compact_models(agent_model: str) -> list[str]:
    """Return the priority-ordered list of models for compaction.

    Reads ``llm.compact_models`` from config.  The keyword ``"default"``
    (and empty strings) are replaced with *agent_model*.  If the config
    list is empty or absent, falls back to ``[agent_model]``.
    """
    cfg = get_config_value("llm", "compact_models", default=["default"])
    raw: list[str] = cfg if isinstance(cfg, list) else ["default"]
    resolved: list[str] = []
    for entry in raw:
        model = entry.strip() if isinstance(entry, str) else ""
        if not model or model == "default":
            model = agent_model
        if model and model not in resolved:  # deduplicate
            resolved.append(model)
    return resolved or [agent_model]


class CompactionMode(str, Enum):
    """Compaction strategy: FULL summarizes all events, PARTIAL keeps recent ones."""

    FULL = "full"
    PARTIAL = "partial"


@dataclass
class CompactionResult:
    """Result of a compaction operation."""

    summary: str
    model: str = ""
    kept_events: list[EventRecord] = field(default_factory=list)
    restored_attachments: list[str] = field(default_factory=list)
    tokens_saved: int = 0
    events_summarized: int = 0


def record_compaction(
    session_store: Any,
    hook_manager: Any,
    session_id: str,
    *,
    summary: str,
    mode: str,
    model: str,
    tokens_before: int,
    tokens_saved: int,
    events_summarized: int,
) -> None:
    """Persist a compaction outcome and notify on_compact hooks.

    Single source of truth shared by the auto path (``Orchestrator._maybe_auto_compact``)
    and the user-triggered path (``commands._handle_compact``).  Saves the
    summary, appends a ``context_compacted`` marker event, and fires the
    on-compact hook so telemetry, channel adapters, and ContextBuilder
    boundary detection all line up regardless of who triggered compaction.
    """
    session_store.save_summary(session_id, summary)
    session_store.append_event(
        session_id,
        {
            "type": "context_compacted",
            "payload": {
                "agent_id": None,
                "depth": 0,
                "mode": mode,
                "model": model,
                "tokens_before": tokens_before,
                "tokens_saved": tokens_saved,
                "tokens_after": max(0, tokens_before - tokens_saved),
                "events_summarized": events_summarized,
                "summary": summary,
                "fallback": False,
            },
        },
    )
    if hook_manager is not None:
        hook_manager.run_on_compact(
            session_id,
            summary=summary,
            tokens_before=tokens_before,
            tokens_saved=tokens_saved,
            events_summarized=events_summarized,
        )


def get_compact_prompt(model: str | None = None) -> str:
    """Return the active compaction system prompt for *model*.

    Reads ``compaction.caveman_mode`` from config. When true, renders the
    caveman-augmented prompt (the ``caveman`` scenario of ``compact.system``)
    that instructs the summarizer to drop filler while preserving code, paths,
    URLs, and error strings verbatim. When false (default), renders the standard
    prompt. Both bodies live in the central prompt registry.

    ``model`` threads the summarization model through to the registry so a
    per-model override of ``compact.system`` reaches the compaction prompt too
    (#113) — not just the loop's per-step prompts. ``None`` renders the base.
    The scenario (caveman) still wins over a model override per registry
    resolution order.
    """
    caveman = bool(get_config_value("compaction", "caveman_mode", default=False))
    return get_prompt_registry().render(
        "compact.system", model=model, scenario="caveman" if caveman else None
    )


def _strip_analysis(text: str) -> str:
    """Remove <analysis>...</analysis> scratchpad from summary."""
    return re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL).strip()


def _extract_summary(text: str) -> str:
    """Extract content between <summary> tags, or return full text if no tags."""
    match = re.search(r"<summary>(.*?)</summary>", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: strip analysis and return whatever remains
    return _strip_analysis(text)


def _extract_file_references(events: list[EventRecord]) -> list[str]:
    """Extract file paths referenced in tool_result events."""
    paths: list[str] = []
    for event in events:
        if event.get("type") != "tool_result":
            continue
        payload = event.get("payload", {})
        tool_input = payload.get("tool_input", "")
        # Extract file paths from common tool input patterns
        if isinstance(tool_input, dict):
            for key in ("file_path", "path", "filename"):
                if key in tool_input and isinstance(tool_input[key], str):
                    paths.append(tool_input[key])
        elif isinstance(tool_input, str):
            # Simple heuristic: look for paths with extensions
            for token in tool_input.split():
                if "/" in token and "." in token.split("/")[-1]:
                    paths.append(token)
    # Deduplicate, keep order, return most recent first
    seen: set[str] = set()
    unique: list[str] = []
    for p in reversed(paths):
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


async def compact_conversation(
    events: list[EventRecord],
    mode: CompactionMode = CompactionMode.FULL,
    *,
    model_name: str | None = None,
    pivot_index: int | None = None,
    pre_compact_hooks: list[Callable[..., Any]] | None = None,
    max_restore_files: int = 5,
    max_tokens_per_file: int = 5000,
    focus_prompt: str | None = None,
) -> CompactionResult:
    """Two-mode compaction with structured summary and post-compact restoration.

    Args:
        events: Full event transcript.
        mode: FULL (summarize everything) or PARTIAL (summarize old, keep recent).
        model_name: LLM model for summarization. Falls back to config default.
        pivot_index: For PARTIAL mode, events before this index are summarized.
            Defaults to len(events) - recent_event_limit.
        pre_compact_hooks: Optional hooks to run on events before summarization.
        max_restore_files: Max files to re-inject post-compact.
        max_tokens_per_file: Token cap per restored file.
        focus_prompt: Optional free-form directive appended to the summarizer
            system prompt (Codex-style ``/compact <focus>``). Biases what the
            summary highlights without dropping critical state.

    Returns:
        CompactionResult with summary, kept events, and restored attachments.
    """
    from mewbo_core.llm import build_chat_model  # Lazy import to avoid circular

    if not events:
        return CompactionResult(summary="", tokens_saved=0)

    # Apply pre-compact hooks
    if pre_compact_hooks:
        for hook in pre_compact_hooks:
            try:
                events = hook(events)
            except Exception:
                logger.warning("Pre-compact hook failed", exc_info=True)

    # Determine what to summarize vs keep
    if mode == CompactionMode.PARTIAL:
        recent_limit = int(get_config_value("context", "recent_event_limit", default=8))
        if pivot_index is None:
            pivot_index = max(0, len(events) - recent_limit)
        to_summarize = events[:pivot_index]
        kept_events = events[pivot_index:]
    else:
        to_summarize = events
        kept_events = []

    if not to_summarize:
        return CompactionResult(
            summary="",
            kept_events=kept_events,
            tokens_saved=0,
        )

    # Count tokens before compaction
    events_text = "\n".join(_format_event(e) for e in to_summarize)
    tokens_before = count_tokens(events_text)

    # Resolve compaction models: explicit arg wraps into a single-item list,
    # otherwise use the config-driven priority list.
    fallback_default = str(get_config_value("llm", "default_model", default="") or "").strip()
    models = [model_name] if model_name else resolve_compact_models(fallback_default)

    from langchain_core.messages import HumanMessage, SystemMessage

    # Render the compaction prompt for the model that will actually summarize
    # (the primary candidate) so its per-model override applies (#113).
    system_content = get_compact_prompt(model=models[0])
    if focus_prompt and focus_prompt.strip():
        system_content += get_prompt_registry().render(
            "compact.focus_suffix", model=models[0], focus_prompt=focus_prompt.strip()
        )
    msgs = [
        SystemMessage(content=system_content),
        HumanMessage(content=f"Summarize this conversation:\n\n{events_text}"),
    ]

    # Try each model in priority order; last failure propagates.
    response = None
    model = models[0]
    for i, candidate in enumerate(models):
        model = candidate
        try:
            llm = build_chat_model(model_name=model)
            response = await llm.ainvoke(msgs)
            break
        except Exception:
            if i < len(models) - 1:
                logger.warning(
                    "Compact model %s failed, trying next: %s",
                    model,
                    models[i + 1],
                    exc_info=True,
                )
            else:
                raise
    assert response is not None  # guaranteed by loop logic
    raw_summary = response.content if hasattr(response, "content") else str(response)
    # Reasoning models return a list of content blocks; extract text.
    if isinstance(raw_summary, list):
        raw_summary = next(
            (b["text"] for b in raw_summary if isinstance(b, dict) and b.get("type") == "text"),
            "",
        )

    # Extract clean summary
    summary = _extract_summary(raw_summary)
    tokens_after = count_tokens(summary)

    # Post-compact file restoration
    restored: list[str] = []
    file_refs = _extract_file_references(to_summarize)
    for path in file_refs[:max_restore_files]:
        try:
            from pathlib import Path

            p = Path(path)
            if p.is_file() and p.stat().st_size < 500_000:  # Skip huge files
                content = p.read_text(encoding="utf-8", errors="replace")
                tokens = count_tokens(content)
                if tokens <= max_tokens_per_file:
                    restored.append(f"## File: {path}\n```\n{content}\n```")
                else:
                    # Truncate to token limit (rough char estimate)
                    char_limit = max_tokens_per_file * 3  # ~3 chars per token
                    restored.append(
                        f"## File: {path} (truncated)\n```\n{content[:char_limit]}\n```"
                    )
        except (OSError, UnicodeDecodeError):
            logger.debug("Could not restore file: %s", path)

    return CompactionResult(
        summary=summary,
        model=model,
        kept_events=kept_events,
        restored_attachments=restored,
        tokens_saved=max(0, tokens_before - tokens_after),
        events_summarized=len(to_summarize),
    )


def _format_event(event: EventRecord) -> str:
    """Format an event record for summarization input."""
    etype = event.get("type", "unknown")
    payload = event.get("payload", {})
    ts = event.get("ts", "")

    if etype == "user":
        return f"[{ts}] User: {payload.get('text', '')}"
    elif etype == "assistant":
        return f"[{ts}] Assistant: {payload.get('text', '')}"
    elif etype == "tool_result":
        tool_id = payload.get("tool_id", "unknown")
        summary = payload.get("summary", payload.get("result", ""))
        success = payload.get("success", True)
        status = "OK" if success else "FAILED"
        return f"[{ts}] Tool({tool_id}) [{status}]: {summary}"
    elif etype == "action_plan":
        steps = payload.get("steps", [])
        step_texts = [f"  - {s.get('title', '')}" for s in steps]
        return f"[{ts}] Plan:\n" + "\n".join(step_texts)
    else:
        return f"[{ts}] {etype}: {payload}"
