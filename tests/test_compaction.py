"""Tests for transcript compaction utilities."""

from meeseeks_core.compaction import micro_compact_events


def test_micro_compact_events_truncates_large_results():
    """Truncate tool_result payloads exceeding 2000 chars."""
    large_result = "x" * 5000
    events = [
        {"type": "tool_result", "payload": {"tool_id": "shell", "result": large_result}},
        {"type": "user", "payload": {"text": "hello"}},
    ]
    compacted = micro_compact_events(events)
    assert len(compacted) == 2
    # Tool result truncated to ~2000 chars + "[truncated]"
    tool_result = compacted[0]["payload"]["result"]
    assert len(tool_result) < 2100
    assert tool_result.endswith("[truncated]")
    # Non-tool events unchanged
    assert compacted[1] == events[1]


def test_micro_compact_events_strips_ansi():
    """Strip ANSI escape sequences from large tool results."""
    # Result with ANSI codes that exceeds threshold
    ansi_result = "\x1b[31mERROR\x1b[0m: " + "y" * 3000
    events = [
        {"type": "tool_result", "payload": {"tool_id": "shell", "result": ansi_result}},
    ]
    compacted = micro_compact_events(events)
    result = compacted[0]["payload"]["result"]
    assert "\x1b[" not in result


def test_micro_compact_events_small_results_unchanged():
    """Small tool results pass through unchanged."""
    events = [
        {"type": "tool_result", "payload": {"tool_id": "read", "result": "short"}},
    ]
    compacted = micro_compact_events(events)
    assert compacted[0]["payload"]["result"] == "short"
