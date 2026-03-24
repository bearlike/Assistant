"""Tests for the context compaction module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from meeseeks_core.compact import (
    COMPACT_PROMPT,
    CompactionMode,
    CompactionResult,
    _extract_file_references,
    _extract_summary,
    _format_event,
    _strip_analysis,
    compact_conversation,
)

# -- Helpers ----------------------------------------------------------------


def _user(text: str, ts: str = "T0") -> dict:
    return {"type": "user", "payload": {"text": text}, "ts": ts}


def _assistant(text: str, ts: str = "T1") -> dict:
    return {"type": "assistant", "payload": {"text": text}, "ts": ts}


def _tool(tid: str, result: str, ok: bool = True, ts: str = "T2", ti=None) -> dict:
    p: dict = {"tool_id": tid, "result": result, "success": ok, "summary": result[:50]}
    if ti:
        p["tool_input"] = ti
    return {"type": "tool_result", "payload": p, "ts": ts}


def _plan(steps: list[dict], ts: str = "T3") -> dict:
    return {"type": "action_plan", "payload": {"steps": steps}, "ts": ts}


# -- _strip_analysis --------------------------------------------------------


class TestStripAnalysis:
    def test_removes_tags(self):
        assert _strip_analysis("<analysis>x</analysis>\nY") == "Y"

    def test_passthrough(self):
        assert _strip_analysis("plain") == "plain"

    def test_multiline(self):
        assert _strip_analysis("<analysis>\na\nb\n</analysis>\nZ") == "Z"


# -- _extract_summary -------------------------------------------------------


class TestExtractSummary:
    def test_with_tags(self):
        assert _extract_summary("<analysis>skip</analysis><summary>Good</summary>") == "Good"

    def test_no_tags_strips_analysis(self):
        assert _extract_summary("<analysis>skip</analysis>Remaining") == "Remaining"

    def test_plain(self):
        assert _extract_summary("Just text") == "Just text"


# -- _extract_file_references -----------------------------------------------


class TestExtractFileReferences:
    def test_dict_file_path(self):
        refs = _extract_file_references([_tool("r", "c", ti={"file_path": "/a.py"})])
        assert "/a.py" in refs

    def test_dict_path_key(self):
        refs = _extract_file_references([_tool("r", "c", ti={"path": "/b.py"})])
        assert "/b.py" in refs

    def test_non_tool_ignored(self):
        assert _extract_file_references([_user("hi")]) == []

    def test_dedup_recent_first(self):
        refs = _extract_file_references(
            [
                _tool("r", "a", ti={"file_path": "/x"}, ts="1"),
                _tool("r", "b", ti={"file_path": "/y"}, ts="2"),
                _tool("r", "c", ti={"file_path": "/x"}, ts="3"),
            ]
        )
        assert refs[0] == "/x"
        assert refs[1] == "/y"


# -- _format_event ----------------------------------------------------------


class TestFormatEvent:
    def test_user(self):
        assert "[T0] User: hi" == _format_event(_user("hi"))

    def test_assistant(self):
        assert "Assistant:" in _format_event(_assistant("ok"))

    def test_tool_ok(self):
        f = _format_event(_tool("t", "res"))
        assert "[OK]" in f and "t" in f

    def test_tool_fail(self):
        assert "[FAILED]" in _format_event(_tool("t", "err", ok=False))

    def test_plan(self):
        f = _format_event(_plan([{"title": "S1"}]))
        assert "Plan:" in f and "S1" in f

    def test_unknown(self):
        assert "custom" in _format_event({"type": "custom", "payload": {}, "ts": "T"})


# -- CompactionResult -------------------------------------------------------


class TestCompactionResult:
    def test_defaults(self):
        r = CompactionResult(summary="s")
        assert r.kept_events == []
        assert r.restored_attachments == []
        assert r.tokens_saved == 0


# -- compact_conversation ---------------------------------------------------


class TestCompactConversation:
    def test_empty_events(self):
        r = asyncio.run(compact_conversation([]))
        assert r.summary == ""
        assert r.tokens_saved == 0

    def test_full_mode(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(
            content="<analysis>draft</analysis><summary>Done.</summary>"
        )
        with patch("meeseeks_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(compact_conversation(
                [_user("do X"), _assistant("done")], mode=CompactionMode.FULL,
            ))
        assert "Done." in r.summary
        assert r.kept_events == []
        llm.ainvoke.assert_called_once()

    def test_partial_keeps_recent(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>Old</summary>")
        with (
            patch("meeseeks_core.llm.build_chat_model", return_value=llm),
            patch("meeseeks_core.compact.get_config_value", return_value=2),
        ):
            r = asyncio.run(compact_conversation(
                [
                    _user("old", "T1"), _assistant("old_r", "T2"),
                    _user("new", "T3"), _assistant("new_r", "T4"),
                ],
                mode=CompactionMode.PARTIAL,
            ))
        assert len(r.kept_events) == 2
        assert r.kept_events[0]["ts"] == "T3"

    def test_partial_explicit_pivot(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>s</summary>")
        with patch("meeseeks_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(compact_conversation(
                [_user(f"m{i}") for i in range(5)],
                mode=CompactionMode.PARTIAL, pivot_index=3,
            ))
        assert len(r.kept_events) == 2

    def test_pre_compact_hook_applied(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>ok</summary>")
        with patch("meeseeks_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(compact_conversation(
                [_user("a"), _user("b"), _user("c")],
                mode=CompactionMode.FULL,
                pre_compact_hooks=[lambda evts: evts[1:]],
            ))
        assert r.summary == "ok"

    def test_pre_compact_hook_error_safe(self):
        def bad(evts):
            raise RuntimeError("fail")

        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>fine</summary>")
        with patch("meeseeks_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(compact_conversation(
                [_user("x")], mode=CompactionMode.FULL, pre_compact_hooks=[bad],
            ))
        assert r.summary == "fine"

    def test_partial_nothing_to_summarize(self):
        with patch("meeseeks_core.compact.get_config_value", return_value=10):
            r = asyncio.run(compact_conversation(
                [_user("recent")], mode=CompactionMode.PARTIAL,
            ))
        assert r.summary == ""
        assert len(r.kept_events) == 1

    def test_compact_prompt_exists(self):
        assert "Do NOT use any tools" in COMPACT_PROMPT
        assert "<summary>" in COMPACT_PROMPT
