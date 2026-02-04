"""Tests for token budget calculations."""

from meeseeks_core import token_budget as token_budget_module
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


def test_load_context_overrides_env_json(monkeypatch):
    """Load context overrides from JSON environment values."""
    monkeypatch.setenv("MESEEKS_MODEL_CONTEXT_WINDOWS", '{"gpt-x": 8000}')
    overrides = token_budget_module._load_context_overrides()
    assert overrides["gpt-x"] == 8000


def test_load_context_overrides_from_file(tmp_path, monkeypatch):
    """Load context overrides from a JSON file path."""
    config_path = tmp_path / "contexts.json"
    config_path.write_text('{"gpt-y": 9000}', encoding="utf-8")
    monkeypatch.setenv("MESEEKS_MODEL_CONTEXT_WINDOWS", str(config_path))
    overrides = token_budget_module._load_context_overrides()
    assert overrides["gpt-y"] == 9000


def test_load_context_overrides_from_toml(tmp_path, monkeypatch):
    """Load context overrides from a TOML file path."""
    config_path = tmp_path / "contexts.toml"
    config_path.write_text("gpt-z = 7777\n", encoding="utf-8")
    monkeypatch.setenv("MESEEKS_MODEL_CONTEXT_WINDOWS", str(config_path))
    overrides = token_budget_module._load_context_overrides()
    assert overrides["gpt-z"] == 7777


def test_get_context_window_uses_override(monkeypatch):
    """Use context window override when present."""
    monkeypatch.setenv("MESEEKS_MODEL_CONTEXT_WINDOWS", '{"gpt-override": 1234}')
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
