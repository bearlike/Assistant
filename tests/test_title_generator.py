"""Tests for LLM-based session title generation."""

import asyncio
from unittest.mock import AsyncMock, patch

from truss_core.title_generator import (
    TITLE_SYSTEM_PROMPT,
    _clean_title,
    generate_session_title,
)


def _msg(content):
    class _M:
        def __init__(self, c):
            self.content = c

    return _M(content)


def test_clean_title_strips_quotes_and_punctuation():
    assert _clean_title('"Fix login bug"') == "Fix login bug"
    assert _clean_title("Refactor planner.") == "Refactor planner"
    assert _clean_title("  Deploy docs!  ") == "Deploy docs"
    assert _clean_title("“Curly quotes”") == "Curly quotes"


def test_clean_title_enforces_word_cap():
    long = "one two three four five six seven eight nine ten"
    cleaned = _clean_title(long)
    assert cleaned is not None
    assert len(cleaned.split()) == 7


def test_clean_title_returns_none_for_empty():
    assert _clean_title("") is None
    assert _clean_title("   ") is None
    assert _clean_title(".") is None


def test_clean_title_respects_char_cap():
    long_word = "a" * 200
    cleaned = _clean_title(long_word)
    assert cleaned is not None
    assert len(cleaned) <= 120


def test_generate_title_returns_none_on_empty_events():
    result = asyncio.run(generate_session_title([]))
    assert result is None


def test_generate_title_returns_none_when_no_user_or_assistant():
    events = [
        {"type": "context", "payload": {}},
        {"type": "tool_result", "payload": {"text": "ok"}},
    ]
    result = asyncio.run(generate_session_title(events))
    assert result is None


def test_generate_title_returns_none_when_no_model_configured():
    events = [{"type": "user", "payload": {"text": "hi"}}]
    with patch(
        "truss_core.title_generator.get_config_value",
        return_value="",
    ):
        result = asyncio.run(generate_session_title(events))
    assert result is None


def test_generate_title_happy_path():
    events = [
        {"type": "user", "payload": {"text": "How do I add auth to the API?"}},
        {"type": "assistant", "payload": {"text": "Add a decorator to each route."}},
    ]

    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_msg("Add API Authentication"))

    with (
        patch(
            "truss_core.title_generator.get_config_value",
            side_effect=lambda *keys, **_kw: "gpt-5.2" if keys[-1] == "default_model" else "",
        ),
        patch("truss_core.llm.build_chat_model", return_value=fake_llm),
    ):
        result = asyncio.run(generate_session_title(events))

    assert result == "Add API Authentication"
    fake_llm.ainvoke.assert_awaited_once()
    # System prompt was included
    args, _ = fake_llm.ainvoke.call_args
    messages = args[0]
    assert messages[0].content == TITLE_SYSTEM_PROMPT
    # User payload contains both parts wrapped in XML tags
    assert "User:" in messages[1].content
    assert "Assistant:" in messages[1].content
    assert "<conversation>" in messages[1].content
    assert "</conversation>" in messages[1].content
    assert messages[1].content.strip().endswith("Title:")


def test_generate_title_prefers_title_model_over_default():
    events = [{"type": "user", "payload": {"text": "hi"}}]

    captured: dict[str, str] = {}

    def fake_builder(model_name, **_):
        captured["model"] = model_name
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=_msg("Small Title"))
        return llm

    def fake_get(*keys, **_kw):
        leaf = keys[-1]
        if leaf == "title_model":
            return "provider/title-model"
        if leaf == "default_model":
            return "provider/default-model"
        return ""

    with (
        patch("truss_core.title_generator.get_config_value", side_effect=fake_get),
        patch("truss_core.llm.build_chat_model", side_effect=fake_builder),
    ):
        result = asyncio.run(generate_session_title(events))
    assert result == "Small Title"
    assert captured["model"] == "provider/title-model"


def test_generate_title_returns_none_on_llm_failure():
    events = [{"type": "user", "payload": {"text": "hi"}}]

    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch(
            "truss_core.title_generator.get_config_value",
            side_effect=lambda *keys, **_kw: "gpt-5.2" if keys[-1] == "default_model" else "",
        ),
        patch("truss_core.llm.build_chat_model", return_value=fake_llm),
    ):
        result = asyncio.run(generate_session_title(events))
    assert result is None


def test_generate_title_truncates_long_excerpts():
    long_text = "x" * 2000
    events = [{"type": "user", "payload": {"text": long_text}}]

    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_msg("Title"))

    with (
        patch(
            "truss_core.title_generator.get_config_value",
            side_effect=lambda *keys, **_kw: "gpt-5.2" if keys[-1] == "default_model" else "",
        ),
        patch("truss_core.llm.build_chat_model", return_value=fake_llm),
    ):
        asyncio.run(generate_session_title(events))

    # Confirm the user payload was capped at 500 chars per-source.
    args, _ = fake_llm.ainvoke.call_args
    user_content = args[0][1].content
    assert len(user_content) < 750  # "User: " + 500 chars + XML wrapper ~40 chars


def test_generate_title_user_only_includes_xml_framing():
    """Prompt uses structural framing even with user-only text."""
    events = [{"type": "user", "payload": {"text": "How is it going bro?"}}]

    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_msg("Casual greeting exchange"))

    with (
        patch(
            "truss_core.title_generator.get_config_value",
            side_effect=lambda *keys, **_kw: "gpt-5.2" if keys[-1] == "default_model" else "",
        ),
        patch("truss_core.llm.build_chat_model", return_value=fake_llm),
    ):
        result = asyncio.run(generate_session_title(events))

    assert result == "Casual greeting exchange"
    args, _ = fake_llm.ainvoke.call_args
    messages = args[0]
    # System prompt explicitly warns against responding
    assert "Do NOT respond" in messages[0].content
    # User message is wrapped in XML, not bare
    assert "<conversation>" in messages[1].content
    assert "</conversation>" in messages[1].content
    assert messages[1].content.strip().endswith("Title:")


def test_generate_title_handles_reasoning_model_content_blocks():
    """Reasoning models return content as a list of blocks, not a string.

    Regression test for session 009006c6 where the title was rendered as
    ``[{'type': 'thinking', 'thinking': 'Python OAuth ...``
    """
    events = [{"type": "user", "payload": {"text": "Add auth to API"}}]

    # Simulate a reasoning model response: thinking + text blocks
    structured_content = [
        {"type": "thinking", "thinking": "Let me think about a title..."},
        {"type": "text", "text": "API Authentication Plan"},
    ]
    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_msg(structured_content))

    with (
        patch(
            "truss_core.title_generator.get_config_value",
            side_effect=lambda *keys, **_kw: "gpt-5.2" if keys[-1] == "default_model" else "",
        ),
        patch("truss_core.llm.build_chat_model", return_value=fake_llm),
    ):
        result = asyncio.run(generate_session_title(events))

    assert result == "API Authentication Plan"
    # Must NOT contain the raw list repr
    assert "[{" not in (result or "")
