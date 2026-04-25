#!/usr/bin/env python3
"""Context selection and rendering helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from langchain_core.messages import SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from pydantic import BaseModel, Field

from mewbo_core.attachments import (
    encode_image_data_uri,
    is_image,
    model_supports_vision,
    parsed_sidecar_path,
)
from mewbo_core.common import format_tool_input, get_logger
from mewbo_core.components import build_langfuse_handler, langfuse_trace_span
from mewbo_core.config import get_config_value, get_version
from mewbo_core.llm import build_chat_model
from mewbo_core.session_store import SessionStoreBase
from mewbo_core.token_budget import TokenBudget, get_token_budget
from mewbo_core.types import EventRecord

logging = get_logger(name="core.context")


class ContextSelection(BaseModel):
    """Model output for selecting context events."""

    keep_ids: list[int] = Field(default_factory=list)
    drop_ids: list[int] = Field(default_factory=list)


@dataclass(frozen=True)
class ContextSnapshot:
    """Context snapshot for planning and synthesis."""

    summary: str | None
    recent_events: list[EventRecord]
    selected_events: list[EventRecord] | None
    events: list[EventRecord]
    budget: TokenBudget
    # Markdown-rendered text from documents (PDF/DOCX/CSV/...) and raw
    # text files. Injected into the system prompt's "Attached files:" block.
    attachment_texts: list[str] = field(default_factory=list)
    # LiteLLM-style image content parts: {"type": "image_url", "image_url": {"url": ...}}
    # Spliced into the per-turn HumanMessage on vision-capable models.
    attachment_images: list[dict] = field(default_factory=list)


def event_payload_text(event: EventRecord) -> str:
    """Return a readable payload string for an event."""
    payload = event.get("payload", "")
    if isinstance(payload, dict):
        if "tool_input" in payload:
            payload = dict(payload)
            payload["tool_input"] = format_tool_input(payload.get("tool_input"))
        return str(
            payload.get("text") or payload.get("message") or payload.get("result") or payload
        )
    return str(payload)


def render_event_lines(events: list[EventRecord]) -> str:
    """Render events into bullet lines for prompts."""
    lines: list[str] = []
    for event in events:
        text = event_payload_text(event)
        if not text:
            continue
        lines.append(f"- {event.get('type', 'event')}: {text}")
    return "\n".join(lines).strip()


# Per-file and aggregate caps for parsed/text attachment content. Sized
# for Markdown rendered from PDFs/DOCX — a 10-page PDF easily produces
# 60–80 KB of MD; a single CSV can be much larger.
_MAX_ATTACHMENT_BYTES = 200_000  # per file
_MAX_TOTAL_ATTACHMENT_BYTES = 1_000_000  # aggregate cap


def _iter_attachments(events: list[EventRecord]):
    """Yield attachment dicts from all ``context`` events in order."""
    for event in events:
        if event.get("type") != "context":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if isinstance(att, dict):
                yield att


def _read_text_capped(path: str, cap: int) -> str | None:
    """Read up to ``cap`` bytes of text from ``path``; None on error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return None


def _load_attachment_texts(
    session_dir: str,
    events: list[EventRecord],
    model_name: str | None = None,
) -> list[str]:
    """Read attachment content for inclusion in the system prompt.

    For documents we prefer the parsed Markdown sidecar (``<stored>.md``)
    written at upload time. Images are not loaded here — see
    :func:`_load_attachment_images`. Images on non-vision models surface
    as a clear ``[Image ... skipped]`` warning so the model knows the
    user attempted to share visual context.
    """
    texts: list[str] = []
    total_bytes = 0
    has_vision = model_supports_vision(model_name)
    for att in _iter_attachments(events):
        stored_name = att.get("stored_name")
        filename = att.get("filename", stored_name)
        content_type = str(att.get("content_type", ""))
        size = int(att.get("size_bytes", 0) or 0)
        if not stored_name:
            continue
        raw_path = os.path.join(session_dir, "attachments", stored_name)

        if is_image(content_type):
            # Surface a warning for non-vision models so the LLM knows
            # the user shared an image it can't see (Q5 option C).
            if not has_vision:
                texts.append(
                    f"[Image {filename}: model does not support vision; image skipped]"
                )
            # Vision models receive the image via ``attachment_images`` —
            # nothing to add to the text block.
            continue

        # Document path: prefer parsed-Markdown sidecar.
        sidecar = parsed_sidecar_path(raw_path)
        load_path = sidecar if os.path.isfile(sidecar) else raw_path
        if not os.path.isfile(load_path):
            continue

        if load_path is raw_path and size > _MAX_ATTACHMENT_BYTES:
            texts.append(f"[Attachment {filename}: {size} bytes, too large to include]")
            continue
        content = _read_text_capped(load_path, _MAX_ATTACHMENT_BYTES)
        if content is None:
            continue
        if total_bytes + len(content) > _MAX_TOTAL_ATTACHMENT_BYTES:
            texts.append(f"[Attachment {filename}: skipped, aggregate size limit reached]")
            continue
        total_bytes += len(content)
        texts.append(f"--- {filename} ---\n{content}")
    return texts


def _load_attachment_images(
    session_dir: str,
    events: list[EventRecord],
    model_name: str | None,
) -> list[dict]:
    """Build LiteLLM-style ``image_url`` content parts for vision models.

    Returns an empty list when the active model is not vision-capable —
    the user is informed via a text warning instead (see
    :func:`_load_attachment_texts`).
    """
    if not model_supports_vision(model_name):
        return []
    parts: list[dict] = []
    for att in _iter_attachments(events):
        stored_name = att.get("stored_name")
        content_type = str(att.get("content_type", ""))
        if not stored_name or not is_image(content_type):
            continue
        path = os.path.join(session_dir, "attachments", stored_name)
        url = encode_image_data_uri(path, content_type)
        if not url:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


class ContextBuilder:
    """Build short-term and selected context for a session."""

    def __init__(self, session_store: SessionStoreBase) -> None:
        """Initialize the context builder."""
        self._session_store = session_store

    def build(
        self,
        session_id: str,
        user_query: str,
        model_name: str | None,
    ) -> ContextSnapshot:
        """Build a context snapshot for planning and synthesis."""
        events = self._session_store.load_transcript(session_id)
        summary = self._session_store.load_summary(session_id)
        # Compaction boundary: events at or before the most recent
        # ``context_compacted`` event are already represented in ``summary``.
        # Replaying their raw payloads under "Recent conversation:" would
        # double-count them and leak pre-compaction noise that the user
        # explicitly asked to summarize away. Slice forward past the marker.
        last_compact_ts = max(
            (e.get("ts", "") for e in events if e.get("type") == "context_compacted"),
            default="",
        )
        if last_compact_ts:
            events = [e for e in events if e.get("ts", "") > last_compact_ts]
        context_events = [
            event
            for event in events
            if event.get("type")
            in {
                "user",
                "assistant",
                "tool_result",
                "step_reflection",
            }
        ]
        recent_limit = int(get_config_value("context", "recent_event_limit", default=8))
        recent_events = context_events[-recent_limit:] if recent_limit > 0 else []
        candidate_events = context_events[:-recent_limit] if recent_limit > 0 else context_events
        # Anchor the first user event so long sessions cannot FIFO-evict the
        # original task from recent_events. Cached prefix-friendly: the anchor
        # renders into the SystemMessage's "Recent conversation:" block, which
        # is inside the cacheable prefix rather than the per-turn HumanMessage.
        # After a compaction boundary the original task already lives inside
        # ``summary``; only anchor the post-boundary first user event so we
        # don't re-introduce pre-boundary content the user just summarized.
        first_user = next((e for e in context_events if e.get("type") == "user"), None)
        if first_user is not None and first_user not in recent_events:
            recent_events = [first_user, *recent_events]
        from mewbo_core.token_budget import read_last_input_tokens

        last_input_tokens = read_last_input_tokens(list(events))
        budget = get_token_budget(
            events,
            summary,
            model_name,
            last_input_tokens=last_input_tokens,
        )
        selected_events: list[EventRecord] | None = None
        selection_threshold = float(get_config_value("context", "selection_threshold", default=0.8))
        if (
            bool(get_config_value("context", "selection_enabled", default=True))
            and candidate_events
            and budget.utilization >= selection_threshold
        ):
            selected_events = self._select_context_events(
                candidate_events,
                user_query=user_query,
                model_name=model_name,
            )
        session_dir = self._session_store.session_dir(session_id)
        attachment_texts = _load_attachment_texts(session_dir, events, model_name)
        attachment_images = _load_attachment_images(session_dir, events, model_name)
        return ContextSnapshot(
            summary=summary,
            recent_events=recent_events,
            selected_events=selected_events,
            events=events,
            budget=budget,
            attachment_texts=attachment_texts,
            attachment_images=attachment_images,
        )

    def _select_context_events(
        self,
        events: list[EventRecord],
        user_query: str,
        model_name: str | None,
    ) -> list[EventRecord]:
        if not events:
            return []
        selector_model = (
            get_config_value("context", "context_selector_model")
            or model_name
            or get_config_value("llm", "action_plan_model")
            or get_config_value("llm", "default_model")
        )
        if not selector_model:
            return events
        parser = PydanticOutputParser(pydantic_object=ContextSelection)
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(
                    content=(
                        "You select which prior events are still relevant to the user's "
                        "current request. Keep only events that directly help answer the "
                        "current query. If unsure, keep the event."
                    )
                ),
                HumanMessagePromptTemplate.from_template(
                    "User query:\n{user_query}\n\n"
                    "Candidate events:\n{candidates}\n\n"
                    "Return keep_ids and drop_ids.\n{format_instructions}"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["user_query", "candidates"],
        )
        lines: list[str] = []
        for idx, event in enumerate(events, start=1):
            text = event_payload_text(event)
            if not text:
                continue
            lines.append(f"{idx}. {event.get('type', 'event')}: {text}")
        candidates_text = "\n".join(lines).strip()
        if not candidates_text:
            return events
        model = build_chat_model(model_name=selector_model)
        handler = build_langfuse_handler(
            user_id="mewbo-context",
            session_id=f"context-{os.getpid()}-{os.urandom(4).hex()}",
            trace_name="context-select",
            version=get_version(),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        config: dict[str, object] = {}
        if handler is not None:
            config["callbacks"] = [handler]
            metadata = getattr(handler, "langfuse_metadata", None)
            if isinstance(metadata, dict) and metadata:
                config["metadata"] = metadata
        try:
            with langfuse_trace_span(
                "context-select",
                metadata={
                    "model": selector_model,
                    "candidates": str(len(lines)),
                },
                input_data={
                    "user_query": user_query.strip()[:200],
                    "candidate_count": len(lines),
                },
            ) as span:
                selection = (prompt | model | parser).invoke(
                    {"user_query": user_query.strip(), "candidates": candidates_text},
                    config=config or None,
                )
                if span is not None:
                    try:
                        span.update_trace(
                            output={
                                "keep_ids": selection.keep_ids,
                                "drop_ids": selection.drop_ids,
                            }
                        )
                    except Exception:
                        pass
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("Context selection failed: {}", exc)
            return events[-3:]

        keep_ids = set(selection.keep_ids or [])
        if not keep_ids:
            return events[-3:]
        kept: list[EventRecord] = []
        for idx, event in enumerate(events, start=1):
            if idx in keep_ids:
                kept.append(event)
        return kept or events[-3:]


__all__ = [
    "ContextBuilder",
    "ContextSnapshot",
    "ContextSelection",
    "event_payload_text",
    "render_event_lines",
]
