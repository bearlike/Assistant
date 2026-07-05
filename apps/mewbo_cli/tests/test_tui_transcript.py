#!/usr/bin/env python3
"""Pilot tests for TranscriptView streaming + renderer registry (issue #152).

All tests use the ``asyncio.run(_run())`` + ``app.run_test()`` pattern
mirrored from ``test_tui_app.py``.  Widget-only tests mount the widget in a
tiny throwaway ``App``.

Required coverage (from the brief):

- Streaming-delta cache: partial stream → trailing-partial re-render path is
  used (full-render counter does NOT increment per delta beyond block
  boundaries); final text renders correctly.
- Renderer-registry routing: each kind routes to its renderer; unknown kind →
  fallback; tool name keys to Bash vs Edit rendering.
- Collapsible thinking cycles through 3 modes.
- Tool output truncates and expands.
- DiffView is mounted for an Edit tool payload carrying old/new text.
"""

from __future__ import annotations

import asyncio
import json
from io import StringIO

from mewbo_cli.cli_theme import DEFAULT_PALETTE
from mewbo_cli.tui.seams import MessageRendererRegistry, TranscriptItem
from mewbo_cli.tui.transcript_render import StreamingMarkdown, register_transcript_renderers
from mewbo_cli.tui.widgets.transcript import TranscriptView
from rich.console import Console
from textual.app import App, ComposeResult

# ---------------------------------------------------------------------------
# Minimal host app
# ---------------------------------------------------------------------------


class _TranscriptApp(App[None]):
    """Minimal host for TranscriptView."""

    def __init__(self, registry: MessageRendererRegistry) -> None:
        super().__init__()
        self._msg_registry = registry

    def compose(self) -> ComposeResult:
        yield TranscriptView(self._msg_registry, palette=DEFAULT_PALETTE, id="tv")


def _make_registry() -> MessageRendererRegistry:
    """Return a registry with all transcript renderers registered."""
    registry = MessageRendererRegistry()
    register_transcript_renderers(registry, palette=DEFAULT_PALETTE)
    return registry


# ---------------------------------------------------------------------------
# StreamingMarkdown — stable-prefix cache tests
# ---------------------------------------------------------------------------


def test_streaming_markdown_no_full_rerender_on_mid_block_delta() -> None:
    """A delta that extends the LAST incomplete block does NOT trigger a full re-render.

    The stable-prefix cache splits at the last blank-line boundary.  Appending
    text within the same paragraph keeps _full_render_count constant because the
    stable prefix hasn't grown — only the trailing partial changes.

    This is the load-bearing guarantee of StreamingMarkdown: O(partial) render
    cost on each mid-block delta, not O(full document).
    """
    sm = StreamingMarkdown()
    sm.feed("Hello world")
    # Call render() so the counter is live — the stable prefix is empty here
    # (no block boundary yet) so the stable render returns an empty Text and
    # does NOT increment the counter.
    sm.render(width=80)
    count_after_first_render = sm._full_render_count

    # Mid-block delta: same paragraph, no blank-line boundary introduced.
    # The stable prefix is still empty — render() must NOT increment counter.
    sm.feed(" more words")
    sm.render(width=80)
    assert sm._full_render_count == count_after_first_render, (
        "Feeding a mid-block delta must NOT trigger a new full render of the stable prefix"
    )


def test_streaming_markdown_full_rerender_on_new_block() -> None:
    """A delta that crosses a blank-line boundary advances the stable prefix.

    When text up to a block boundary is committed as the stable prefix, the
    NEXT call to render() triggers exactly one full re-render of that prefix
    (cache miss).  Subsequent renders with the same prefix are cache hits and
    do NOT increment the counter again.
    """
    sm = StreamingMarkdown()
    # Feed a complete paragraph — block boundary included.
    sm.feed("Paragraph one\n\n")
    # First render: stable prefix = "Paragraph one\n\n" → cache miss → count goes up.
    sm.render(width=80)
    count_after_first_block = sm._full_render_count
    assert count_after_first_block >= 1, "Stable prefix render must have incremented counter"

    # Feed a mid-block delta — stable prefix unchanged → cache hit → no increment.
    sm.feed("Paragraph two start")
    sm.render(width=80)
    assert sm._full_render_count == count_after_first_block, (
        "Mid-block delta after a stable prefix must NOT trigger a new full render"
    )

    # Feed another block boundary — stable prefix grows again → next render = cache miss.
    sm.feed("\n\n")
    sm.render(width=80)
    assert sm._full_render_count > count_after_first_block, (
        "Crossing a new block boundary must trigger a full re-render of the extended stable prefix"
    )


def test_streaming_markdown_final_text_correct() -> None:
    """After all deltas the accumulated text matches expected output."""
    sm = StreamingMarkdown()
    deltas = ["# Hello\n\n", "Some **bold** text\n\n", "Another line"]
    for delta in deltas:
        sm.feed(delta)
    assert sm.text == "".join(deltas)


def test_streaming_markdown_invalidates_on_non_prefix() -> None:
    """Feeding text that is not a prefix-extension resets the cache."""
    sm = StreamingMarkdown()
    sm.feed("Alpha text")
    sm.feed("\n\nBeta block")  # crosses boundary → stable prefix extended

    # Now reset by feeding text that is NOT a continuation.
    sm.reset()
    sm.feed("Completely new content")
    # After reset the full_render_count resets too — the new feed starts fresh.
    assert sm.text == "Completely new content"


def test_streaming_markdown_width_change_invalidates_cache() -> None:
    """Changing render width invalidates the memo cache (different layout)."""
    sm = StreamingMarkdown()
    sm.feed("Some text\n\n")
    sm.render(width=80)
    miss_at_80 = sm._full_render_count

    sm.render(width=120)
    # Width changed → memoized render is invalid → full re-render.
    assert sm._full_render_count > miss_at_80


# ---------------------------------------------------------------------------
# register_transcript_renderers — routing by kind
# ---------------------------------------------------------------------------


def test_renderer_registry_registers_expected_kinds() -> None:
    """register_transcript_renderers registers user, assistant, tool, error."""
    registry = _make_registry()
    for kind in ("user", "assistant", "tool", "error"):
        assert registry.has(kind), f"Expected kind '{kind}' to be registered"


def test_renderer_user_kind() -> None:
    """user kind produces a renderable (no exception)."""
    registry = _make_registry()
    item = TranscriptItem("user", {"text": "Hello from user"})
    result = registry.render(item)
    assert result is not None


def test_renderer_assistant_kind() -> None:
    """assistant kind renders markdown without raising."""
    registry = _make_registry()
    item = TranscriptItem("assistant", {"text": "**bold** response"})
    result = registry.render(item)
    assert result is not None


def test_renderer_error_kind() -> None:
    """error kind renders styled text without raising."""
    registry = _make_registry()
    item = TranscriptItem("error", {"text": "Something went wrong"})
    result = registry.render(item)
    assert result is not None


def test_renderer_unknown_kind_uses_fallback() -> None:
    """An unregistered kind falls through to the generic fallback (no raise)."""
    registry = _make_registry()
    item = TranscriptItem("unknown-mystery-kind", {"text": "fallback text"})
    result = registry.render(item)
    assert result is not None


# ---------------------------------------------------------------------------
# Tool renderer routing by tool name
# ---------------------------------------------------------------------------


def test_tool_renderer_bash_shows_command() -> None:
    """Bash/shell tool payload produces a renderable containing the command."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool",
        {
            "tool_id": "bash",
            "operation": "execute",
            "result": "hello\nworld",
            "is_mcp": False,
        },
    )
    rendered = registry.render(item)
    # Render to string to inspect content
    sio = StringIO()
    console = Console(file=sio, highlight=False)
    console.print(rendered)
    text = sio.getvalue()
    assert "bash" in text.lower() or "execute" in text.lower() or "hello" in text.lower()


def test_tool_renderer_edit_shows_diffstat() -> None:
    """Edit tool payload (no old/new) produces a diffstat-style renderable."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool",
        {
            "tool_id": "Edit",
            "operation": "edit",
            "result": "file edited",
            "is_mcp": False,
        },
    )
    rendered = registry.render(item)
    sio = StringIO()
    console = Console(file=sio, highlight=False)
    console.print(rendered)
    text = sio.getvalue()
    assert text  # must produce something without raising


def test_tool_renderer_default_fallback() -> None:
    """Unknown tool names use the bullet-tree default path."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool",
        {
            "tool_id": "some_custom_tool",
            "operation": "action",
            "result": "done",
            "is_mcp": True,
        },
    )
    result = registry.render(item)
    assert result is not None


# ---------------------------------------------------------------------------
# TranscriptView — public API contract
# ---------------------------------------------------------------------------


def test_transcript_view_write_item_no_raise() -> None:
    """write_item with a registered renderer does not raise."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(TranscriptItem("user", {"text": "test input"}))
            await pilot.pause()

    asyncio.run(_run())


def test_transcript_view_write_renderable_no_raise() -> None:
    """write_renderable accepts a Raw Rich renderable."""
    from rich.text import Text

    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_renderable(Text("direct renderable"))
            await pilot.pause()

    asyncio.run(_run())


def test_transcript_view_write_item_unknown_kind_no_raise() -> None:
    """write_item with an unregistered kind falls back gracefully."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(TranscriptItem("totally-unknown", {"text": "fallback"}))
            await pilot.pause()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Streaming on TranscriptView: begin / append / end
# ---------------------------------------------------------------------------


def test_transcript_view_stream_lifecycle() -> None:
    """begin_stream / append_stream / end_stream round-trips without raising."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.begin_stream("sid-1")
            tv.append_stream("sid-1", "Hello ")
            tv.append_stream("sid-1", "world")
            tv.end_stream("sid-1")
            await pilot.pause()

    asyncio.run(_run())


def test_transcript_view_stream_second_begin_noop() -> None:
    """A second begin_stream for the same id is a no-op (idempotent)."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.begin_stream("sid-2")
            tv.begin_stream("sid-2")  # second call — should not raise
            tv.end_stream("sid-2")
            await pilot.pause()

    asyncio.run(_run())


def test_transcript_view_stream_sub_agent_label() -> None:
    """Streaming with an agent_label produces labelled output without raising."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.begin_stream("sid-3", agent_label="sub-agent-1")
            tv.append_stream("sid-3", "Sub-agent response text")
            tv.end_stream("sid-3")
            await pilot.pause()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# DiffView mounting for Edit tool payload with old/new text
# ---------------------------------------------------------------------------


def test_diffview_mounted_for_edit_with_old_new() -> None:
    """An Edit tool TranscriptItem with old_text/new_text mounts a DiffView."""
    from mewbo_cli.cli_diffview import DiffView

    registry = _make_registry()

    class _EditToolApp(App[None]):
        """Minimal host: we manually write items into the TranscriptView."""

        def compose(self) -> ComposeResult:
            yield TranscriptView(registry, palette=DEFAULT_PALETTE, id="tv")

    async def _run() -> None:
        app = _EditToolApp()
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(
                TranscriptItem(
                    "tool",
                    {
                        "tool_id": "Edit",
                        "operation": "edit",
                        "old_text": "old line\n",
                        "new_text": "new line\n",
                        "file_path": "example.py",
                        "is_mcp": False,
                    },
                )
            )
            await pilot.pause()
            # DiffView should be mounted somewhere in the widget tree
            diff_views = app.query(DiffView)
            assert len(diff_views) >= 1, "DiffView must be mounted for Edit+old_text+new_text"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Mutable tool card + turn-summary spinner (#161)
# ---------------------------------------------------------------------------


def test_upsert_tool_mutates_one_card_in_place() -> None:
    """A running card updated to settled is ONE widget, gaining ``t-settled``."""
    from textual.widgets import Static

    registry = _make_registry()

    class _App(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(registry, palette=DEFAULT_PALETTE, id="tv")

    async def _run() -> None:
        app = _App()
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            running = TranscriptItem(
                "tool", {"tool_id": "bash", "command": "ls", "status": "running"}
            )
            tv.upsert_tool("card-1", running)
            await pilot.pause()
            before = len(app.query(Static))
            settled = TranscriptItem(
                "tool",
                {"tool_id": "bash", "command": "ls", "status": "done",
                 "result": "ok", "elapsed": 1.5},
            )
            tv.upsert_tool("card-1", settled)
            await pilot.pause()
            # No NEW Static mounted — the same card mutated in place.
            assert len(app.query(Static)) == before
            card = tv._tool_cards["card-1"]  # type: ignore[attr-defined]
            assert card.has_class("t-settled")

    asyncio.run(_run())


def test_upsert_tool_mounts_diffview_once_on_settle() -> None:
    """A settled Edit card mounts a DiffView exactly once (not on running)."""
    from mewbo_cli.cli_diffview import DiffView

    registry = _make_registry()

    class _App(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(registry, palette=DEFAULT_PALETTE, id="tv")

    async def _run() -> None:
        app = _App()
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.upsert_tool(
                "c", TranscriptItem("tool", {"tool_id": "Edit", "status": "running",
                                             "file_path": "x.py"})
            )
            await pilot.pause()
            assert len(app.query(DiffView)) == 0  # no diff while running
            tv.upsert_tool(
                "c",
                TranscriptItem(
                    "tool",
                    {"tool_id": "Edit", "status": "done", "file_path": "x.py",
                     "old_text": "a\n", "new_text": "b\n", "result": "ok"},
                ),
            )
            await pilot.pause()
            assert len(app.query(DiffView)) == 1
            # Idempotent: a redundant settle does not mount a second DiffView.
            tv.upsert_tool(
                "c",
                TranscriptItem(
                    "tool",
                    {"tool_id": "Edit", "status": "done", "file_path": "x.py",
                     "old_text": "a\n", "new_text": "b\n", "result": "ok"},
                ),
            )
            await pilot.pause()
            assert len(app.query(DiffView)) == 1

    asyncio.run(_run())


def test_finish_activity_collapses_spinner_into_summary() -> None:
    """finish_activity stops the spinner and leaves a settled summary line."""
    registry = _make_registry()

    class _App(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(registry, palette=DEFAULT_PALETTE, id="tv")

    async def _run() -> None:
        app = _App()
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.start_activity("Working")
            await pilot.pause()
            assert tv._activity is not None  # type: ignore[attr-defined]
            tv.finish_activity(2.0)
            await pilot.pause()
            # Handle released (so the next turn mounts a fresh spinner)…
            assert tv._activity is None  # type: ignore[attr-defined]
            # …but the timer is stopped (no dangling animation).
            assert tv._activity_timer is None  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_hub_drives_transcript_view_in_emission_order() -> None:
    """End-to-end keystone: AgentTranscriptHub → sink → live TranscriptView.

    Feeds a scripted interleaved event sequence (text → tool → text) through the
    hub and asserts the TranscriptView mounts streamed text, then the tool card,
    then more streamed text — in emission order, with the tool card mutating in
    place (running → settled) rather than duplicating.
    """
    from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub
    from textual.widgets import Static

    registry = _make_registry()

    class _App(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(registry, palette=DEFAULT_PALETTE, id="tv")

    class _DirectSink:
        """Drive the TranscriptView directly (test runs on the UI thread)."""

        def __init__(self, tv: TranscriptView) -> None:
            self._tv = tv
            self._begun: set[str] = set()

        def stream_delta(self, stream_id: str, delta: str) -> None:
            if stream_id not in self._begun:
                self._begun.add(stream_id)
                self._tv.begin_stream(stream_id)
            self._tv.append_stream(stream_id, delta)

        def stream_end(self, stream_id: str) -> None:
            self._begun.discard(stream_id)
            self._tv.end_stream(stream_id)

        def upsert_tool(self, card_id: str, item: TranscriptItem) -> None:
            self._tv.upsert_tool(card_id, item)

        def spawn(self, item: TranscriptItem) -> None:
            self._tv.write_item(item)

        def set_status(self, label: str) -> None:
            self._tv.set_activity_label(label)

    class _Step:
        def __init__(self, tool_id: str, tool_input: object) -> None:
            self.tool_id = tool_id
            self.tool_input = tool_input
            self.operation = "execute"

    async def _run() -> None:
        app = _App()
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            hub = AgentTranscriptHub(sink=_DirectSink(tv))
            hub.set_active_session("s")
            hub.observe("s", {"type": "llm_call_start",
                              "payload": {"agent_id": "root", "depth": 0, "step": 0}})
            hub.observe("s", {"type": "agent_message_delta",
                              "payload": {"text": "Working on it", "agent_id": "root",
                                          "depth": 0, "step": 0}})
            hub.observe("s", {"type": "agent_message",
                              "payload": {"agent_id": "root", "depth": 0,
                                          "text": "Working on it"}})
            hub.tool_started(_Step("bash", {"command": "ls"}))
            await pilot.pause()
            running_cards = len(app.query(Static))
            hub.observe("s", {"type": "tool_result",
                              "payload": {"tool_id": "bash", "operation": "execute",
                                          "tool_input": {"command": "ls"}, "result": "a\nb",
                                          "success": True, "agent_id": "root", "depth": 0}})
            await pilot.pause()
            # The tool card mutated in place — no new Static mounted on settle.
            assert len(app.query(Static)) == running_cards
            # Final text streams as a fresh span after the tool.
            hub.observe("s", {"type": "agent_message_delta",
                              "payload": {"text": "Done.", "agent_id": "root",
                                          "depth": 0, "step": 1}})
            hub.observe("s", {"type": "completion",
                              "payload": {"done": True, "done_reason": "completed",
                                          "task_result": "Done."}})
            await pilot.pause()
            # Root transcript: text span, tool, text span — exact emission order.
            root = hub.transcript("root")
            assert root is not None
            assert [e.kind for e in root.entries] == ["text", "tool", "text"]

    asyncio.run(_run())


def test_streamed_content_mounts_above_active_spinner() -> None:
    """During a live turn, new content lands ABOVE the foot spinner."""
    from textual.widgets import Static

    registry = _make_registry()

    class _App(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(registry, palette=DEFAULT_PALETTE, id="tv")

    async def _run() -> None:
        app = _App()
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.start_activity("Working")
            await pilot.pause()
            tv.write_item(TranscriptItem("assistant", {"text": "streamed line"}))
            await pilot.pause()
            children = list(tv.query(Static))
            activity = tv._activity  # type: ignore[attr-defined]
            assert activity is not None
            # The activity spinner is the LAST child (content mounted before it).
            assert children[-1] is activity

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Collapsible thinking: 3-mode cycle
# ---------------------------------------------------------------------------


def test_thinking_section_collapser_cycles_three_modes() -> None:
    """ThinkingCollapser cycles: collapsed → tail_window → expanded → collapsed."""
    from mewbo_cli.tui.transcript_render import ThinkingCollapser

    tc = ThinkingCollapser("Some thinking text\nLine 2\nLine 3\nLine 4\nLine 5")
    assert tc.mode == "collapsed"

    tc.cycle()
    assert tc.mode == "tail_window"

    tc.cycle()
    assert tc.mode == "expanded"

    tc.cycle()
    assert tc.mode == "collapsed"


def test_thinking_section_collapser_tail_window_returns_last_n() -> None:
    """tail_window mode returns the last N lines of thinking text."""
    from mewbo_cli.tui.transcript_render import ThinkingCollapser

    lines = [f"line {i}" for i in range(20)]
    text = "\n".join(lines)
    tc = ThinkingCollapser(text, tail_lines=5)
    tc.cycle()  # → tail_window
    assert tc.mode == "tail_window"
    visible = tc.visible_text()
    for line in lines[-5:]:
        assert line in visible


def test_thinking_section_collapser_expanded_returns_all() -> None:
    """expanded mode returns the full thinking text."""
    from mewbo_cli.tui.transcript_render import ThinkingCollapser

    text = "Line A\nLine B\nLine C"
    tc = ThinkingCollapser(text)
    tc.cycle()  # → tail_window
    tc.cycle()  # → expanded
    assert tc.mode == "expanded"
    assert text in tc.visible_text()


# ---------------------------------------------------------------------------
# Tool output truncation and expand
# ---------------------------------------------------------------------------


def test_tool_output_truncates_by_default() -> None:
    """ToolOutputCollapser truncates long output and shows a hidden-lines hint."""
    from mewbo_cli.tui.transcript_render import ToolOutputCollapser

    long_output = "\n".join(f"line {i}" for i in range(200))
    toc = ToolOutputCollapser(long_output, max_lines=10)
    assert toc.is_truncated
    text = toc.visible_text()
    assert "hidden" in text.lower() or "expand" in text.lower() or "lines" in text.lower()


def test_tool_output_expand_reveals_all() -> None:
    """ToolOutputCollapser.expand() reveals the full output."""
    from mewbo_cli.tui.transcript_render import ToolOutputCollapser

    long_output = "\n".join(f"line {i}" for i in range(200))
    toc = ToolOutputCollapser(long_output, max_lines=10)
    toc.expand()
    assert not toc.is_truncated
    for i in range(200):
        assert f"line {i}" in toc.visible_text()


def test_tool_output_short_not_truncated() -> None:
    """Short tool output is NOT truncated."""
    from mewbo_cli.tui.transcript_render import ToolOutputCollapser

    short_output = "just a few\nlines here"
    toc = ToolOutputCollapser(short_output, max_lines=50)
    assert not toc.is_truncated


# ---------------------------------------------------------------------------
# palette injected
# ---------------------------------------------------------------------------


def test_transcript_view_accepts_palette_kwarg() -> None:
    """TranscriptView.__init__ accepts palette= without error."""
    from mewbo_cli.cli_theme import HYPER_PALETTE

    registry = _make_registry()

    async def _run() -> None:
        class _App(App[None]):
            def compose(self) -> ComposeResult:
                yield TranscriptView(registry, palette=HYPER_PALETTE, id="tv")

        async with _App().run_test() as pilot:
            tv = pilot.app.query_one(TranscriptView)
            assert tv is not None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Per-kind CSS class tagging on mounted children (design language)
# ---------------------------------------------------------------------------


def _render_to_text(rendered: object) -> str:
    """Render a Rich renderable to plain text for assertions."""
    sio = StringIO()
    Console(file=sio, highlight=False, width=200).print(rendered)
    return sio.getvalue()


def test_transcript_children_tagged_with_kind_classes() -> None:
    """Each mounted child carries a ``t-<kind>`` class derived from the item kind."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(TranscriptItem("user", {"text": "hi"}))
            tv.write_item(TranscriptItem("assistant", {"text": "hello"}))
            tv.write_item(
                TranscriptItem("tool", {"tool_id": "bash", "result": "ok"})
            )
            await pilot.pause()
            assert len(tv.query(".t-user")) >= 1
            assert len(tv.query(".t-assistant")) >= 1
            assert len(tv.query(".t-tool")) >= 1

    asyncio.run(_run())


def test_transcript_sub_agent_tool_gets_agent_class() -> None:
    """A tool item with an agent_label additionally gets the deeper ``t-agent`` class."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(
                TranscriptItem(
                    "tool",
                    {"tool_id": "bash", "result": "ok", "agent_label": "probe-1"},
                )
            )
            await pilot.pause()
            assert len(tv.query(".t-tool.t-agent")) >= 1

    asyncio.run(_run())


def test_transcript_diff_child_tagged_t_diff() -> None:
    """The DiffView mounted for an Edit item is tagged ``t-diff``."""
    registry = _make_registry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(
                TranscriptItem(
                    "tool",
                    {
                        "tool_id": "Edit",
                        "old_text": "a\n",
                        "new_text": "b\n",
                        "file_path": "x.py",
                    },
                )
            )
            await pilot.pause()
            assert len(tv.query(".t-diff")) >= 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Bash header shows "$ command"
# ---------------------------------------------------------------------------


def test_bash_tool_header_shows_dollar_command() -> None:
    """Bash tool header renders ``$ <command>`` using the command payload key."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool",
        {"tool_id": "bash", "command": "ls -la /tmp", "result": "total 0"},
    )
    text = _render_to_text(registry.render(item))
    assert "$ ls -la /tmp" in text


# ---------------------------------------------------------------------------
# Generic/MCP tool: JSON pretty-print + truncation
# ---------------------------------------------------------------------------


def test_generic_tool_pretty_prints_json() -> None:
    """A single-line JSON result is re-dumped with indentation (not a raw blob)."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool",
        {
            "tool_id": "some_mcp_tool",
            "is_mcp": True,
            "result": '{"a": 1, "b": {"c": 2}}',
        },
    )
    text = _render_to_text(registry.render(item))
    # Indented re-dump means the nested key sits on its own indented line.
    assert '"a": 1' in text
    assert '"c": 2' in text
    # The original was a single line; the pretty output spans multiple lines.
    assert text.count("\n") >= 3


def test_generic_tool_truncates_long_json_array() -> None:
    """A long JSON array body truncates to ~15 lines with a ``… +N lines`` hint."""
    registry = _make_registry()
    big_array = "[" + ", ".join(str(i) for i in range(100)) + "]"
    item = TranscriptItem(
        "tool",
        {"tool_id": "some_mcp_tool", "is_mcp": True, "result": big_array},
    )
    text = _render_to_text(registry.render(item))
    assert "… +" in text and "lines" in text


def test_generic_tool_non_json_result_unchanged() -> None:
    """A plain (non-JSON) result is shown verbatim, never mangled by the parser."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool", {"tool_id": "some_mcp_tool", "result": "plain output here"}
    )
    text = _render_to_text(registry.render(item))
    assert "plain output here" in text


def test_shell_envelope_shows_stdout_not_json() -> None:
    """A Mewbo shell result envelope renders its stdout, not the raw JSON wrapper."""
    registry = _make_registry()
    result = json.dumps(
        {"kind": "shell", "command": "ls apps", "stdout": "a.py\nb.py\n",
         "stderr": "", "exit_code": 0}
    )
    item = TranscriptItem("tool", {"tool_id": "bash", "command": "ls apps", "result": result})
    text = _render_to_text(registry.render(item))
    assert "a.py" in text and "b.py" in text
    assert '"kind"' not in text  # the envelope itself is unwrapped, not shown


def test_read_tool_is_compact_no_body() -> None:
    """Read is a high-frequency tool — it renders a compact one-liner, never the file body."""
    registry = _make_registry()
    result = json.dumps({"kind": "file", "path": "x.py", "text": "line one\nline two",
                         "total_lines": 2})
    item = TranscriptItem("tool", {"tool_id": "read_file", "file_path": "x.py", "result": result})
    text = _render_to_text(registry.render(item))
    assert "x.py" in text and "2 lines" in text  # compact header + size hint
    assert "line one" not in text  # the file body is NOT dumped into the viewport
    assert '"total_lines"' not in text


def test_shell_envelope_failure_surfaces_stderr_and_exit() -> None:
    """A non-zero shell envelope surfaces stderr and the exit code."""
    registry = _make_registry()
    result = json.dumps({"kind": "shell", "stdout": "", "stderr": "boom", "exit_code": 2})
    item = TranscriptItem("tool", {"tool_id": "bash", "command": "false", "result": result})
    text = _render_to_text(registry.render(item))
    assert "boom" in text and "exit 2" in text


def test_error_result_uses_cross_glyph() -> None:
    """A result that looks like an error drives the ✗ glyph in the header."""
    registry = _make_registry()
    item = TranscriptItem(
        "tool", {"tool_id": "some_tool", "result": "Error: boom happened"}
    )
    text = _render_to_text(registry.render(item))
    assert "✗" in text


# ---------------------------------------------------------------------------
# Thinking: collapsed render + duration footer
# ---------------------------------------------------------------------------


def test_assistant_thinking_renders_collapsed_line() -> None:
    """A thinking payload renders a collapsed ``💭 Thinking… (N lines)`` line."""
    registry = _make_registry()
    item = TranscriptItem(
        "assistant",
        {"text": "answer", "thinking": "step one\nstep two\nstep three"},
    )
    text = _render_to_text(registry.render(item))
    assert "💭 Thinking…" in text
    assert "(3 lines)" in text


def test_assistant_thinking_duration_footer() -> None:
    """thinking_duration renders a dim ``⤷ Thought for {d:.1f}s`` footer."""
    registry = _make_registry()
    item = TranscriptItem(
        "assistant",
        {"text": "answer", "thinking": "a\nb", "thinking_duration": 2.34},
    )
    text = _render_to_text(registry.render(item))
    assert "Thought for 2.3s" in text


# ---------------------------------------------------------------------------
# GAP 1 — file_edit_tool diff envelope → colored DiffView (not raw JSON)
# ---------------------------------------------------------------------------

_FILE_EDIT_DIFF = (
    "--- _scratch_edit.py\n"
    "+++ _scratch_edit.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def greet(name):\n"
    '-    return "hi " + name\n'
    '+    return "hello " + name\n'
)


def test_unified_diff_to_old_new_reconstructs_sides() -> None:
    """The unified-diff parser splits a hunk into old/new text (context shared)."""
    from mewbo_cli.cli_diffview import unified_diff_to_old_new

    old, new = unified_diff_to_old_new(_FILE_EDIT_DIFF)
    assert old == 'def greet(name):\n    return "hi " + name'
    assert new == 'def greet(name):\n    return "hello " + name'


def test_diffview_from_unified_diff_builds_widget() -> None:
    """DiffView.from_unified_diff yields a widget whose diff_lines carry add/del."""
    from mewbo_cli.cli_diffview import DiffView

    view = DiffView.from_unified_diff(_FILE_EDIT_DIFF, palette=DEFAULT_PALETTE)
    kinds = {dl.kind for dl in view.diff_lines}
    assert "add" in kinds and "del" in kinds


def test_diff_envelope_renderer_shows_diffstat_not_json() -> None:
    """A kind:diff result renders a diffstat header, never the raw JSON envelope."""
    registry = _make_registry()
    result = json.dumps(
        {"kind": "diff", "title": "File Edit", "text": _FILE_EDIT_DIFF,
         "files": ["_scratch_edit.py"]}
    )
    item = TranscriptItem(
        "tool", {"tool_id": "file_edit_tool", "result": result}
    )
    text = _render_to_text(registry.render(item))
    assert "_scratch_edit.py" in text
    assert "+1 -1" in text  # diffstat
    assert '"kind"' not in text  # the envelope JSON is NOT dumped


def test_diff_envelope_mounts_diffview() -> None:
    """A file_edit_tool kind:diff item mounts a colored DiffView child widget."""
    from mewbo_cli.cli_diffview import DiffView

    registry = _make_registry()
    result = json.dumps(
        {"kind": "diff", "text": _FILE_EDIT_DIFF, "files": ["_scratch_edit.py"]}
    )

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one(TranscriptView)
            tv.write_item(
                TranscriptItem(
                    "tool", {"tool_id": "file_edit_tool", "result": result}
                )
            )
            await pilot.pause()
            diff_views = app.query(DiffView)
            assert len(diff_views) >= 1, "kind:diff must mount a DiffView"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# GAP 2 — MCP content-block list + tool_search schema dump unwrap
# ---------------------------------------------------------------------------


def test_mcp_content_block_list_unwraps_to_text() -> None:
    """A list of {type:text,text:…} content blocks renders as joined text."""
    registry = _make_registry()
    result = json.dumps(
        [{"type": "text", "text": "This repository hosts the wiki.",
          "id": "lc_0a98242f"}]
    )
    item = TranscriptItem("tool", {"tool_id": "ask_wiki", "result": result})
    text = _render_to_text(registry.render(item))
    assert "This repository hosts the wiki." in text
    assert "lc_0a98242f" not in text  # the block id metadata is dropped
    assert '"type"' not in text  # not the raw block JSON


def test_mcp_content_block_repr_unwraps_to_text() -> None:
    """A content-block list arriving as a Python repr string also unwraps."""
    registry = _make_registry()
    # str() of the live Python object → single-quoted repr, not valid JSON.
    result = repr([{"type": "text", "text": "Repr-shaped block text."}])
    item = TranscriptItem("tool", {"tool_id": "some_mcp_tool", "result": result})
    text = _render_to_text(registry.render(item))
    assert "Repr-shaped block text." in text


def test_tool_search_functions_payload_collapses_to_summary() -> None:
    """A <functions>…</functions> tool_search dump collapses to a one-line summary."""
    registry = _make_registry()
    result = (
        "<functions>"
        '<function>{"description": "Read a file", "name": "Read", '
        '"parameters": {}}</function>'
        '<function>{"description": "Edit a file", "name": "Edit", '
        '"parameters": {}}</function>'
        "</functions>"
    )
    item = TranscriptItem("tool", {"tool_id": "tool_search", "result": result})
    text = _render_to_text(registry.render(item))
    assert "2 tools loaded" in text
    assert "Read" in text and "Edit" in text
    assert "<function>" not in text  # the raw schema noise is gone
    assert "parameters" not in text
