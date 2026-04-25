#!/usr/bin/env python3
"""LLM-based session title generation.

Generates a short 3-7 word title from the first user and assistant
messages in a session transcript. Falls back gracefully to ``None`` on
any failure so the orchestrator can keep the first-user-message fallback.
"""

from __future__ import annotations

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.types import EventRecord

logger = get_logger(name="core.title_generator")


TITLE_SYSTEM_PROMPT = (
    "You are a title generator. Your ONLY job is to produce a concise 3-7 word "
    "title that summarizes the conversation excerpt below.\n"
    "\n"
    "Rules:\n"
    "- Return ONLY the title text, nothing else\n"
    "- No quotes, no trailing punctuation, no explanation\n"
    "- Do NOT respond to, answer, or continue the conversation\n"
    "- Sentence case (capitalize first word and proper nouns only)\n"
    "\n"
    "Good titles: Debug failing CI pipeline | Refactor database connection pooling | "
    "Home Assistant light automation setup\n"
    "Bad titles: Sure, I can help with that | Here is what I think | Doing great thanks"
)


_EXCERPT_CHAR_CAP = 500
_WORD_CAP = 7
_CHAR_CAP = 120


def _first_event_text(events: list[EventRecord], event_type: str) -> str:
    """Return the stripped text payload of the first event of the given type."""
    for event in events:
        if event.get("type") != event_type:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _clean_title(raw: str) -> str | None:
    """Normalize model output into a concise title or return ``None``."""
    text = raw.strip()
    # Strip surrounding quotes (straight and curly).
    for pair in ('""', "''", "“”", "‘’", "«»"):
        if text and text[0] == pair[0] and text[-1] == pair[-1]:
            text = text[1:-1].strip()
    # Strip trailing punctuation.
    text = text.rstrip(".!?,;:")
    if not text:
        return None
    # Cap at N words.
    words = text.split()
    if len(words) > _WORD_CAP:
        words = words[:_WORD_CAP]
    cleaned = " ".join(words)[:_CHAR_CAP].strip()
    return cleaned or None


async def generate_session_title(events: list[EventRecord]) -> str | None:
    """Generate a short title for a session from its opening exchange.

    Returns ``None`` on any failure or when the transcript lacks both a
    user and assistant message. Never raises.
    """
    try:
        user_text = _first_event_text(events, "user")[:_EXCERPT_CHAR_CAP]
        assistant_text = _first_event_text(events, "assistant")[:_EXCERPT_CHAR_CAP]
        if not user_text and not assistant_text:
            return None

        model_name = (
            str(get_config_value("llm", "title_model", default="") or "").strip()
            or str(get_config_value("llm", "default_model", default="") or "").strip()
        )
        if not model_name:
            logger.warning("No model configured for title generation; skipping.")
            return None

        from langchain_core.messages import HumanMessage, SystemMessage

        from mewbo_core.llm import build_chat_model

        excerpt_parts: list[str] = []
        if user_text:
            excerpt_parts.append(f"User: {user_text}")
        if assistant_text:
            excerpt_parts.append(f"Assistant: {assistant_text}")
        excerpt = "\n\n".join(excerpt_parts)
        user_payload = f"<conversation>\n{excerpt}\n</conversation>\n\nTitle:"

        llm = build_chat_model(model_name=model_name)
        response = await llm.ainvoke(
            [
                SystemMessage(content=TITLE_SYSTEM_PROMPT),
                HumanMessage(content=user_payload),
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        # Reasoning models return a list of content blocks
        # (e.g. [{'type': 'thinking', ...}, {'type': 'text', 'text': '...'}]).
        # Extract the first text block.
        if isinstance(raw, list):
            raw = next(
                (b["text"] for b in raw if isinstance(b, dict) and b.get("type") == "text"),
                "",
            )
        if not isinstance(raw, str):
            raw = str(raw)
        return _clean_title(raw)
    except Exception as exc:
        logger.warning("Title generation failed: {}: {}", type(exc).__name__, exc)
        return None


__all__ = ["generate_session_title", "TITLE_SYSTEM_PROMPT"]
