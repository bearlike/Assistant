"""Tests for context selection helpers."""

import types

from meeseeks_core.config import set_config_override
from meeseeks_core.context import ContextBuilder, event_payload_text, render_event_lines
from meeseeks_core.session_store import SessionStore


def test_event_payload_text_variants():
    """Format payloads with and without dict structures."""
    assert event_payload_text({"type": "user", "payload": "hello"}) == "hello"
    text = event_payload_text(
        {"type": "tool_result", "payload": {"tool_input": {"a": 1}, "result": "ok"}}
    )
    assert "ok" in text
    fallback = event_payload_text({"type": "tool_result", "payload": {"foo": "bar"}})
    assert "foo" in fallback


def test_render_event_lines_skips_empty_payloads():
    """Skip empty event payload text in rendered lines."""
    events = [
        {"type": "user", "payload": ""},
        {"type": "assistant", "payload": {"text": "hi"}},
    ]
    rendered = render_event_lines(events)
    assert "assistant" in rendered
    assert "user" not in rendered


def test_select_context_events_empty_list(tmp_path):
    """Return empty list when there are no events to select."""
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    assert builder._select_context_events([], "query", "model") == []


def test_select_context_events_keep_ids(monkeypatch, tmp_path):
    """Return only events selected by the model."""
    selection = types.SimpleNamespace(keep_ids=[2], drop_ids=[])

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.context.ChatPromptTemplate", lambda *args, **kwargs: DummyPrompt(selection)
    )
    monkeypatch.setattr("meeseeks_core.context.build_chat_model", lambda **_k: object())
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [
        {"type": "user", "payload": {"text": "one"}},
        {"type": "tool_result", "payload": {"text": "two"}},
        {"type": "assistant", "payload": {"text": "three"}},
    ]
    selected = builder._select_context_events(events, "query", "model")
    assert selected == [events[1]]


def test_select_context_events_empty_keep_ids(monkeypatch, tmp_path):
    """Fallback to the last three events when keep_ids is empty."""
    selection = types.SimpleNamespace(keep_ids=[], drop_ids=[])

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.context.ChatPromptTemplate", lambda *args, **kwargs: DummyPrompt(selection)
    )
    monkeypatch.setattr("meeseeks_core.context.build_chat_model", lambda **_k: object())
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [
        {"type": "user", "payload": {"text": "one"}},
        {"type": "tool_result", "payload": {"text": "two"}},
        {"type": "assistant", "payload": {"text": "three"}},
        {"type": "assistant", "payload": {"text": "four"}},
    ]
    selected = builder._select_context_events(events, "query", "model")
    assert selected == events[-3:]


def test_select_context_events_empty_candidates(monkeypatch, tmp_path):
    """Return original events when there are no candidate lines."""
    selection = types.SimpleNamespace(keep_ids=[1], drop_ids=[])

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.context.ChatPromptTemplate", lambda *args, **kwargs: DummyPrompt(selection)
    )
    monkeypatch.setattr("meeseeks_core.context.build_chat_model", lambda **_k: object())
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [
        {"type": "user", "payload": ""},
        {"type": "assistant", "payload": ""},
    ]
    selected = builder._select_context_events(events, "query", "model")
    assert selected == events


def test_select_context_events_without_model(monkeypatch, tmp_path):
    """Return events when no selector model is configured."""
    set_config_override(
        {
            "context": {"context_selector_model": ""},
            "llm": {"default_model": "", "action_plan_model": ""},
        }
    )
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [{"type": "user", "payload": {"text": "one"}}]
    selected = builder._select_context_events(events, "query", None)
    assert selected == events
