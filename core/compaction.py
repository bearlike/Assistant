#!/usr/bin/env python3
"""Transcript compaction utilities."""
from __future__ import annotations

from collections.abc import Iterable

from core.common import get_logger
from core.types import EventRecord

logging = get_logger(name="core.compaction")


def summarize_events(events: Iterable[EventRecord], max_items: int = 20) -> str:
    """Generate a lightweight summary of recent events.

    Args:
        events: Iterable of event records to summarize.
        max_items: Maximum number of recent events to include.

    Returns:
        Concise summary string of recent events.
    """
    snippets: list[str] = []
    for event in list(events)[-max_items:]:
        event_type = event.get("type", "event")
        payload_value: object = event.get("payload", "")
        if isinstance(payload_value, dict):
            payload_data = dict(payload_value)
            payload_value = payload_data.get("text") or payload_data.get("message") or str(
                payload_data
            )
        if payload_value:
            snippets.append(f"{event_type}: {payload_value}")
        else:
            snippets.append(f"{event_type}.")
    return " | ".join(snippets).strip()


def should_compact(events: Iterable[EventRecord], threshold: int = 50) -> bool:
    """Return True when the event list meets the compaction threshold.

    Args:
        events: Iterable of event records to count.
        threshold: Minimum number of events that triggers compaction.

    Returns:
        True when compaction should run.
    """
    return len(list(events)) >= threshold
