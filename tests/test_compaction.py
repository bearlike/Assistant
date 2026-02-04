"""Tests for transcript compaction utilities."""

from meeseeks_core.compaction import summarize_events


def test_summarize_events_handles_empty_payload():
    """Summarize events when payloads are empty."""
    events = [{"type": "user", "payload": ""}]
    summary = summarize_events(events)
    assert summary == "user."
