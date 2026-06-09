"""Tests for ``mewbo_core.draft_stream.DraftStreamer``.

Design
------
- ``build_chat_model`` is patched so ``model.astream(messages)`` is a fake
  async generator that yields ``AIMessageChunk`` objects with ``.content``
  deltas — no real LLM call is made.
- Asserts that DraftStreamer yields those deltas and their concatenation equals
  the joined expected text.
- Asserts NO ``bind_tools`` is called (tool-light invariant).
- Asserts a non-empty ``context`` is prepended as a ``SystemMessage``; an empty
  one is not.
- Covers the ``_extract_text_delta`` helper: str content, list-of-blocks
  content (Anthropic style), and unrecognised shape.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from mewbo_core.draft_stream import DraftStreamer, _extract_text_delta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunks(*texts: str) -> list[AIMessageChunk]:
    """Create a list of AIMessageChunks with string .content."""
    return [AIMessageChunk(content=t) for t in texts]


def _make_list_block_chunks(*texts: str) -> list[AIMessageChunk]:
    """Create AIMessageChunks where .content is a list-of-block dicts."""
    chunks = []
    for t in texts:
        chunk = AIMessageChunk(content=[{"type": "text", "text": t}])
        chunks.append(chunk)
    return chunks


class _FakeModel:
    """Fake ChatLiteLLM — tracks bind_tools calls, yields configured chunks."""

    def __init__(self, chunks: list[AIMessageChunk]) -> None:
        self._chunks = chunks
        self.bind_tools_calls: list[Any] = []
        self.astream_messages: list[Any] = []

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _FakeModel:
        """Record the call so tests can assert it was never called."""
        self.bind_tools_calls.append(tools)
        return self

    async def astream(self, messages: list[Any]) -> AsyncIterator[AIMessageChunk]:  # type: ignore[override]
        self.astream_messages = list(messages)
        for chunk in self._chunks:
            yield chunk


def _patch_build_chat_model(chunks: list[AIMessageChunk]) -> tuple[_FakeModel, Any]:
    """Return (fake_model, patch ctx mgr) for build_chat_model in draft_stream."""
    fake = _FakeModel(chunks)
    patcher = patch(
        "mewbo_core.draft_stream.build_chat_model",
        return_value=fake,
    )
    return fake, patcher


# ---------------------------------------------------------------------------
# _extract_text_delta
# ---------------------------------------------------------------------------


class TestExtractTextDelta:
    def test_str_content(self):
        chunk = AIMessageChunk(content="hello")
        assert _extract_text_delta(chunk) == "hello"

    def test_empty_str(self):
        chunk = AIMessageChunk(content="")
        assert _extract_text_delta(chunk) == ""

    def test_list_text_block(self):
        chunk = AIMessageChunk(content=[{"type": "text", "text": "world"}])
        assert _extract_text_delta(chunk) == "world"

    def test_list_non_text_block_skipped(self):
        chunk = AIMessageChunk(content=[
            {"type": "tool_use", "id": "t1", "name": "foo"},
            {"type": "text", "text": "ok"},
        ])
        assert _extract_text_delta(chunk) == "ok"

    def test_list_thinking_block_skipped(self):
        chunk = AIMessageChunk(content=[
            {"type": "thinking", "thinking": "internal monologue"},
            {"type": "text", "text": "answer"},
        ])
        assert _extract_text_delta(chunk) == "answer"

    def test_list_multi_text_concatenated(self):
        chunk = AIMessageChunk(content=[
            {"type": "text", "text": "foo"},
            {"type": "text", "text": "bar"},
        ])
        assert _extract_text_delta(chunk) == "foobar"

    def test_unknown_shape_returns_empty(self):
        """Unrecognised content shape must return '' (never crash)."""

        class _Weird:
            pass

        chunk = _Weird()
        # _extract_text_delta should not raise — it has no .content attr
        assert _extract_text_delta(chunk) == ""

    def test_list_str_items_concatenated(self):
        """Bare string items in the list should be concatenated."""
        chunk = AIMessageChunk(content=["hello", " ", "world"])
        assert _extract_text_delta(chunk) == "hello world"


# ---------------------------------------------------------------------------
# DraftStreamer.astream — core behaviour
# ---------------------------------------------------------------------------


class TestDraftStreamer:
    def _run(self, coro: Any) -> Any:
        # asyncio.run gives each test a fresh loop — robust under full-suite
        # ordering (a shared get_event_loop() can be closed by a prior test).
        return asyncio.run(coro)

    def test_yields_all_deltas(self):
        """DraftStreamer yields every non-empty delta from the model stream."""
        chunks = _make_chunks("Hello", ", ", "world", "!")
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        with patcher:
            collected = self._run(_collect(streamer.astream("say hello")))

        assert collected == ["Hello", ", ", "world", "!"]

    def test_concatenated_equals_expected_text(self):
        """Joining all yielded deltas reconstructs the full response text."""
        words = ["The", " quick", " brown", " fox"]
        chunks = _make_chunks(*words)
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        with patcher:
            collected = self._run(_collect(streamer.astream("tell me")))

        assert "".join(collected) == "".join(words)

    def test_empty_deltas_skipped(self):
        """Empty delta strings are NOT yielded (suppressed)."""
        chunks = _make_chunks("", "token", "", "!")
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        with patcher:
            collected = self._run(_collect(streamer.astream("q")))

        assert collected == ["token", "!"]

    def test_no_bind_tools_called(self):
        """build_chat_model result must never have bind_tools called (tool-light)."""
        chunks = _make_chunks("ok")
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        with patcher:
            self._run(_collect(streamer.astream("q")))

        assert fake.bind_tools_calls == [], (
            "DraftStreamer must NOT call bind_tools — it is tool-light"
        )

    def test_context_prepended_as_system_message(self):
        """Non-empty context is prepended as a SystemMessage before the HumanMessage."""
        chunks = _make_chunks("answer")
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        ctx = "## Grounded context\n\nSome context text."
        with patcher:
            self._run(_collect(streamer.astream("q?", context=ctx)))

        msgs = fake.astream_messages
        assert len(msgs) == 2, f"Expected 2 messages, got {len(msgs)}: {msgs}"
        assert isinstance(msgs[0], SystemMessage), (
            f"First message must be SystemMessage, got {type(msgs[0])}"
        )
        assert msgs[0].content == ctx
        assert isinstance(msgs[1], HumanMessage)
        assert msgs[1].content == "q?"

    def test_empty_context_omits_system_message(self):
        """Empty context string must NOT inject a SystemMessage."""
        chunks = _make_chunks("answer")
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        with patcher:
            self._run(_collect(streamer.astream("q?", context="")))

        msgs = fake.astream_messages
        assert len(msgs) == 1, f"Expected 1 message (no context), got {len(msgs)}: {msgs}"
        assert isinstance(msgs[0], HumanMessage)

    def test_custom_model_name_forwarded(self):
        """DraftStreamer passes the injected model_name to build_chat_model."""
        chunks = _make_chunks("ok")
        fake = _FakeModel(chunks)

        captured_model_name: list[str] = []

        def _fake_build(model_name: str, **kwargs: Any) -> _FakeModel:
            captured_model_name.append(model_name)
            return fake

        streamer = DraftStreamer(model_name="openai/gpt-4o-mini")
        with patch("mewbo_core.draft_stream.build_chat_model", side_effect=_fake_build):
            self._run(_collect(streamer.astream("q")))

        assert captured_model_name == ["openai/gpt-4o-mini"]

    def test_model_name_none_uses_config_default(self):
        """When model_name=None, the configured default_model is used."""
        chunks = _make_chunks("ok")
        fake = _FakeModel(chunks)
        captured: list[str] = []

        def _fake_build(model_name: str, **kwargs: Any) -> _FakeModel:
            captured.append(model_name)
            return fake

        streamer = DraftStreamer(model_name=None)
        with patch("mewbo_core.draft_stream.build_chat_model", side_effect=_fake_build), \
             patch("mewbo_core.draft_stream.get_config_value", return_value="openai/default"):
            self._run(_collect(streamer.astream("q")))

        assert len(captured) == 1
        assert captured[0] == "openai/default"

    def test_list_block_content_yielded(self):
        """AIMessageChunk with list-of-blocks content is correctly extracted."""
        chunks = _make_list_block_chunks("block1", "block2")
        fake, patcher = _patch_build_chat_model(chunks)

        streamer = DraftStreamer()
        with patcher:
            collected = self._run(_collect(streamer.astream("q")))

        assert collected == ["block1", "block2"]


# ---------------------------------------------------------------------------
# Helper to collect all items from an async generator
# ---------------------------------------------------------------------------


async def _collect(agen: AsyncIterator[str]) -> list[str]:
    result = []
    async for item in agen:
        result.append(item)
    return result
