"""Tests for token budget calculations."""

from meeseeks_core import token_budget as token_budget_module
from meeseeks_core.config import set_config_override
from meeseeks_core.token_budget import get_context_window, get_token_budget


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


def test_parse_context_from_model_name():
    """Parse context windows from model name suffixes."""
    assert token_budget_module._parse_context_from_model("gpt-4-128k") == 128000
    assert token_budget_module._parse_context_from_model("gpt-4-2m") == 2_000_000


def test_load_context_overrides_from_config():
    """Load context overrides from config values."""
    set_config_override({"token_budget": {"model_context_windows": {"gpt-x": 8000}}})
    overrides = token_budget_module._load_context_overrides()
    assert overrides["gpt-x"] == 8000


def test_get_context_window_uses_override(monkeypatch):
    """Use context window override when present."""
    set_config_override({"token_budget": {"model_context_windows": {"gpt-override": 1234}}})
    assert get_context_window("gpt-override") == 1234


def test_event_to_text_fallback():
    """Fallback to JSON string when payload lacks text fields."""
    event = {"type": "tool_result", "payload": {"foo": "bar"}}
    text = token_budget_module._event_to_text(event)
    assert '"foo"' in text


def test_event_to_text_non_dict_payload():
    """Fallback to payload string when payload is not a dict."""
    event = {"type": "user", "payload": "raw text"}
    assert token_budget_module._event_to_text(event) == "raw text"


def test_estimate_event_tokens_empty():
    """Return zero tokens for empty events."""
    assert token_budget_module.estimate_event_tokens([]) == 0
