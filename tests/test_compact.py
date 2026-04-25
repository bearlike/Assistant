"""Tests for the context compaction module."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from mewbo_core.compact import (
    CAVEMAN_COMPACT_PROMPT,
    COMPACT_PROMPT,
    CompactionMode,
    CompactionResult,
    _extract_file_references,
    _extract_summary,
    _format_event,
    _strip_analysis,
    compact_conversation,
    get_compact_prompt,
    record_compaction,
    resolve_compact_models,
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
        assert r.events_summarized == 0


# -- record_compaction (single source of truth) ----------------------------


class TestRecordCompaction:
    """``record_compaction`` is shared by the auto and user-triggered paths.

    It must persist the summary, append a ``context_compacted`` marker
    event with the canonical payload shape, and (when provided) fire the
    ``on_compact`` hook with the same numbers.
    """

    def _make_store(self, tmp_path):
        from mewbo_core.session_store import SessionStore

        store = SessionStore(root_dir=str(tmp_path))
        sid = store.create_session()
        return store, sid

    def test_writes_summary_and_marker_event(self, tmp_path):
        store, sid = self._make_store(tmp_path)
        record_compaction(
            store,
            None,
            sid,
            summary="fresh summary",
            mode="user",
            model="claude-sonnet-4-6",
            tokens_before=12_000,
            tokens_saved=9_000,
            events_summarized=42,
        )
        assert store.load_summary(sid) == "fresh summary"
        events = store.load_transcript(sid)
        assert events, "marker event should be appended"
        marker = events[-1]
        assert marker["type"] == "context_compacted"
        payload = marker["payload"]
        assert payload["mode"] == "user"
        assert payload["model"] == "claude-sonnet-4-6"
        assert payload["tokens_before"] == 12_000
        assert payload["tokens_saved"] == 9_000
        assert payload["tokens_after"] == 3_000
        assert payload["events_summarized"] == 42
        assert payload["summary"] == "fresh summary"
        assert payload["fallback"] is False

    def test_invokes_hook_manager_when_provided(self, tmp_path):
        store, sid = self._make_store(tmp_path)
        hook_calls: list[dict] = []

        class _Hooks:
            def run_on_compact(self, session_id, **kwargs):
                hook_calls.append({"session_id": session_id, **kwargs})

        record_compaction(
            store,
            _Hooks(),
            sid,
            summary="s",
            mode="auto",
            model="m",
            tokens_before=1000,
            tokens_saved=400,
            events_summarized=5,
        )
        assert len(hook_calls) == 1
        call = hook_calls[0]
        assert call["session_id"] == sid
        assert call["summary"] == "s"
        assert call["tokens_before"] == 1000
        assert call["tokens_saved"] == 400
        assert call["events_summarized"] == 5

    def test_tokens_after_clamped_at_zero(self, tmp_path):
        # Defensive: tokens_saved > tokens_before (e.g. summary larger than
        # input on a tiny transcript) must not produce a negative
        # tokens_after in the marker payload.
        store, sid = self._make_store(tmp_path)
        record_compaction(
            store,
            None,
            sid,
            summary="x",
            mode="user",
            model="m",
            tokens_before=10,
            tokens_saved=99,
            events_summarized=1,
        )
        marker = store.load_transcript(sid)[-1]
        assert marker["payload"]["tokens_after"] == 0


# -- compact_conversation: focus_prompt + events_summarized ----------------


class TestCompactConversationFocus:
    def test_focus_prompt_threaded_into_system_message(self):
        captured: dict = {}

        async def _capture(msgs):
            captured["msgs"] = msgs
            return MagicMock(content="<summary>ok</summary>")

        llm = MagicMock()
        llm.ainvoke = _capture
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            asyncio.run(
                compact_conversation(
                    [_user("hi")],
                    mode=CompactionMode.FULL,
                    focus_prompt="API auth refactor",
                )
            )
        sys_content = captured["msgs"][0].content
        assert "User Focus" in sys_content
        assert "API auth refactor" in sys_content

    def test_focus_prompt_omitted_when_blank(self):
        captured: dict = {}

        async def _capture(msgs):
            captured["msgs"] = msgs
            return MagicMock(content="<summary>ok</summary>")

        llm = MagicMock()
        llm.ainvoke = _capture
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            asyncio.run(
                compact_conversation(
                    [_user("hi")],
                    mode=CompactionMode.FULL,
                    focus_prompt="   ",
                )
            )
        sys_content = captured["msgs"][0].content
        assert "User Focus" not in sys_content

    def test_events_summarized_count_populated(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>ok</summary>")
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(
                compact_conversation(
                    [_user("a"), _user("b"), _user("c")],
                    mode=CompactionMode.FULL,
                )
            )
        assert r.events_summarized == 3


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
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(
                compact_conversation(
                    [_user("do X"), _assistant("done")],
                    mode=CompactionMode.FULL,
                )
            )
        assert "Done." in r.summary
        assert r.kept_events == []
        llm.ainvoke.assert_called_once()

    def test_partial_keeps_recent(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>Old</summary>")
        with (
            patch("mewbo_core.llm.build_chat_model", return_value=llm),
            patch("mewbo_core.compact.get_config_value", return_value=2),
        ):
            r = asyncio.run(
                compact_conversation(
                    [
                        _user("old", "T1"),
                        _assistant("old_r", "T2"),
                        _user("new", "T3"),
                        _assistant("new_r", "T4"),
                    ],
                    mode=CompactionMode.PARTIAL,
                )
            )
        assert len(r.kept_events) == 2
        assert r.kept_events[0]["ts"] == "T3"

    def test_partial_explicit_pivot(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>s</summary>")
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(
                compact_conversation(
                    [_user(f"m{i}") for i in range(5)],
                    mode=CompactionMode.PARTIAL,
                    pivot_index=3,
                )
            )
        assert len(r.kept_events) == 2

    def test_pre_compact_hook_applied(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>ok</summary>")
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(
                compact_conversation(
                    [_user("a"), _user("b"), _user("c")],
                    mode=CompactionMode.FULL,
                    pre_compact_hooks=[lambda evts: evts[1:]],
                )
            )
        assert r.summary == "ok"

    def test_pre_compact_hook_error_safe(self):
        def bad(evts):
            raise RuntimeError("fail")

        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>fine</summary>")
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(
                compact_conversation(
                    [_user("x")],
                    mode=CompactionMode.FULL,
                    pre_compact_hooks=[bad],
                )
            )
        assert r.summary == "fine"

    def test_partial_nothing_to_summarize(self):
        with patch("mewbo_core.compact.get_config_value", return_value=10):
            r = asyncio.run(
                compact_conversation(
                    [_user("recent")],
                    mode=CompactionMode.PARTIAL,
                )
            )
        assert r.summary == ""
        assert len(r.kept_events) == 1

    def test_compact_prompt_exists(self):
        assert "Do NOT use any tools" in COMPACT_PROMPT
        assert "<summary>" in COMPACT_PROMPT


# -- Caveman mode selector --------------------------------------------------


class TestGetCompactPrompt:
    """Verify compaction.caveman_mode routes to the right prompt variant
    and that both variants preserve the <analysis>/<summary> structure
    downstream parsers depend on.
    """

    def test_defaults_to_standard_when_config_missing(self):
        # Unknown config key falls through to default=False -> standard prompt.
        with patch("mewbo_core.compact.get_config_value", return_value=False):
            assert get_compact_prompt() is COMPACT_PROMPT

    def test_caveman_mode_true_returns_caveman_prompt(self):
        with patch("mewbo_core.compact.get_config_value", return_value=True):
            assert get_compact_prompt() is CAVEMAN_COMPACT_PROMPT

    def test_caveman_mode_false_returns_standard_prompt(self):
        with patch("mewbo_core.compact.get_config_value", return_value=False):
            assert get_compact_prompt() is COMPACT_PROMPT

    def test_caveman_prompt_preserves_output_structure(self):
        # Downstream _extract_summary regex requires these tags.
        assert "<analysis>" in CAVEMAN_COMPACT_PROMPT
        assert "</analysis>" in CAVEMAN_COMPACT_PROMPT
        assert "<summary>" in CAVEMAN_COMPACT_PROMPT
        assert "</summary>" in CAVEMAN_COMPACT_PROMPT

    def test_caveman_prompt_preserves_section_headings(self):
        # Downstream transcript formatters expect these section titles.
        for heading in (
            "## Primary Request",
            "## Key Technical Concepts",
            "## Files and Code",
            "## Errors and Fixes",
            "## Current State",
            "## Pending Tasks",
        ):
            assert heading in CAVEMAN_COMPACT_PROMPT

    def test_caveman_prompt_contains_core_compression_rules(self):
        # Spot-check load-bearing rule markers — if these drift, the prompt
        # has lost its compression intent.
        assert "Drop rules:" in CAVEMAN_COMPACT_PROMPT
        assert "Articles:" in CAVEMAN_COMPACT_PROMPT
        assert "Preserve EXACTLY" in CAVEMAN_COMPACT_PROMPT
        assert "Auto-Clarity" in CAVEMAN_COMPACT_PROMPT
        assert "Persistence:" in CAVEMAN_COMPACT_PROMPT

    def test_caveman_prompt_forbids_tools(self):
        # Same safety guard as standard prompt.
        assert "Do NOT use any tools" in CAVEMAN_COMPACT_PROMPT


# -- asyncio.run() from background thread (regression for orchestrator) ------


class TestCompactFromBackgroundThread:
    """Verify compact_conversation works when called via asyncio.run()
    from a background thread — the exact pattern used by the API server.

    Root cause of the production bug: orchestrator used
    get_event_loop().run_until_complete() which raises RuntimeError
    in threads without a current event loop (Python 3.10+).
    """

    def test_asyncio_run_in_thread(self):
        """asyncio.run(compact_conversation(...)) must succeed in a
        background thread, not raise 'no current event loop in thread'."""
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(
            content="<analysis>x</analysis><summary>Thread summary.</summary>"
        )
        result_holder: list[CompactionResult] = []
        error_holder: list[Exception] = []

        def run_in_thread() -> None:
            try:
                with patch("mewbo_core.llm.build_chat_model", return_value=llm):
                    r = asyncio.run(
                        compact_conversation(
                            [_user("old msg"), _assistant("old reply")],
                            mode=CompactionMode.FULL,
                        )
                    )
                result_holder.append(r)
            except Exception as exc:
                error_holder.append(exc)

        t = threading.Thread(target=run_in_thread)
        t.start()
        t.join(timeout=10)

        assert not error_holder, f"compact_conversation failed in thread: {error_holder[0]}"
        assert len(result_holder) == 1
        assert "Thread summary." in result_holder[0].summary
        llm.ainvoke.assert_called_once()

    def test_tokens_saved_nonzero(self):
        """When compaction succeeds, tokens_saved must be > 0 (not the
        fallback zero that hid the bug in production)."""
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>Compact.</summary>")
        events = [_user(f"msg {i}") for i in range(10)]
        with patch("mewbo_core.llm.build_chat_model", return_value=llm):
            r = asyncio.run(compact_conversation(events, mode=CompactionMode.FULL))
        # The summary replaces all events, so tokens_saved should be positive
        assert r.tokens_saved > 0, f"Expected positive tokens_saved, got {r.tokens_saved}"


# -- resolve_compact_models -------------------------------------------------


class TestResolveCompactModels:
    def test_default_keyword_resolves_to_agent_model(self):
        with patch("mewbo_core.compact.get_config_value", return_value=["default"]):
            assert resolve_compact_models("agent-model") == ["agent-model"]

    def test_explicit_model_preserved(self):
        with patch(
            "mewbo_core.compact.get_config_value",
            return_value=["haiku", "default"],
        ):
            assert resolve_compact_models("sonnet") == ["haiku", "sonnet"]

    def test_empty_string_treated_as_default(self):
        with patch("mewbo_core.compact.get_config_value", return_value=["", "haiku"]):
            assert resolve_compact_models("sonnet") == ["sonnet", "haiku"]

    def test_empty_list_falls_back_to_agent(self):
        with patch("mewbo_core.compact.get_config_value", return_value=[]):
            assert resolve_compact_models("sonnet") == ["sonnet"]

    def test_deduplicates(self):
        with patch(
            "mewbo_core.compact.get_config_value",
            return_value=["default", "default", "haiku"],
        ):
            assert resolve_compact_models("sonnet") == ["sonnet", "haiku"]

    def test_non_list_config_falls_back(self):
        """If config returns a non-list (e.g. mocked int), treat as default."""
        with patch("mewbo_core.compact.get_config_value", return_value=42):
            assert resolve_compact_models("sonnet") == ["sonnet"]


# -- Model fallback in compact_conversation ---------------------------------


class TestCompactModelFallback:
    def test_first_model_fails_second_succeeds(self):
        """When the first compact model fails, the next is tried."""
        good_llm = AsyncMock()
        good_llm.ainvoke.return_value = MagicMock(content="<summary>Fallback worked.</summary>")
        bad_llm = MagicMock()
        bad_llm.ainvoke = AsyncMock(side_effect=RuntimeError("model down"))

        call_count = 0

        def _build(model_name: str = "", **kw):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            return bad_llm if call_count == 1 else good_llm

        with (
            patch("mewbo_core.llm.build_chat_model", side_effect=_build),
            patch(
                "mewbo_core.compact.resolve_compact_models",
                return_value=["cheap-model", "fallback-model"],
            ),
        ):
            r = asyncio.run(
                compact_conversation(
                    [_user("test")],
                    mode=CompactionMode.FULL,
                )
            )
        assert "Fallback worked." in r.summary
        assert r.model == "fallback-model"

    def test_result_carries_successful_model(self):
        """CompactionResult.model reflects which model actually succeeded."""
        llm = AsyncMock()
        llm.ainvoke.return_value = MagicMock(content="<summary>ok</summary>")
        with (
            patch("mewbo_core.llm.build_chat_model", return_value=llm),
            patch(
                "mewbo_core.compact.resolve_compact_models",
                return_value=["my-haiku"],
            ),
        ):
            r = asyncio.run(
                compact_conversation(
                    [_user("test")],
                    mode=CompactionMode.FULL,
                )
            )
        assert r.model == "my-haiku"
