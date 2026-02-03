"""Tests for token budget calculations."""
from core.token_budget import (
    get_context_window,
    get_token_budget,
)


def test_get_context_window_from_model_name():
    """Resolve context windows from model name aliases."""
    assert get_context_window("gpt-4-32k") == 32000


def test_token_budget_flags_compact_threshold():
    """Flag compaction when thresholds are exceeded."""
    events = [{"type": "user", "payload": {"text": "hello"}} for _ in range(5)]
    budget = get_token_budget(
        events,
        summary="summary",
        model_name="gpt-3.5-turbo-16k",
        threshold=0.0001,
    )
    assert budget.needs_compact is True
    assert budget.context_window == 16000
