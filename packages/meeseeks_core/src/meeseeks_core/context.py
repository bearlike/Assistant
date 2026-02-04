#!/usr/bin/env python3
"""Context selection and rendering helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.messages import SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from pydantic.v1 import BaseModel, Field

from meeseeks_core.common import format_action_argument, get_logger
from meeseeks_core.llm import build_chat_model
from meeseeks_core.session_store import SessionStore
from meeseeks_core.token_budget import TokenBudget, get_token_budget
from meeseeks_core.types import EventRecord

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


def event_payload_text(event: EventRecord) -> str:
    """Return a readable payload string for an event."""
    payload = event.get("payload", "")
    if isinstance(payload, dict):
        if "action_argument" in payload:
            payload = dict(payload)
            payload["action_argument"] = format_action_argument(payload.get("action_argument"))
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


class ContextBuilder:
    """Build short-term and selected context for a session."""

    def __init__(self, session_store: SessionStore) -> None:
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
        context_events = [
            event for event in events if event.get("type") in {"user", "assistant", "tool_result"}
        ]
        recent_limit = int(os.getenv("MEESEEKS_RECENT_EVENT_LIMIT", "8"))
        recent_events = context_events[-recent_limit:] if recent_limit > 0 else []
        candidate_events = context_events[:-recent_limit] if recent_limit > 0 else context_events
        budget = get_token_budget(events, summary, model_name)
        selected_events: list[EventRecord] | None = None
        selection_threshold = float(os.getenv("MEESEEKS_CONTEXT_SELECT_THRESHOLD", "0.8"))
        if (
            os.getenv("MEESEEKS_CONTEXT_SELECTION", "1") != "0"
            and candidate_events
            and budget.utilization >= selection_threshold
        ):
            selected_events = self._select_context_events(
                candidate_events,
                user_query=user_query,
                model_name=model_name,
            )
        return ContextSnapshot(
            summary=summary,
            recent_events=recent_events,
            selected_events=selected_events,
            events=events,
            budget=budget,
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
            os.getenv("CONTEXT_SELECTOR_MODEL")
            or model_name
            or os.getenv("ACTION_PLAN_MODEL")
            or os.getenv("DEFAULT_MODEL")
        )
        if not selector_model:
            return events
        parser = PydanticOutputParser(pydantic_object=ContextSelection)  # type: ignore[type-var]
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
        model = build_chat_model(
            model_name=selector_model,
            temperature=0.0,
            openai_api_base=os.getenv("OPENAI_API_BASE"),
        )
        try:
            selection = (prompt | model | parser).invoke(
                {"user_query": user_query.strip(), "candidates": candidates_text}
            )
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
