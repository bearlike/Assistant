#!/usr/bin/env python3
"""Behavioural tests for the rich InputArea widget.

Mount the widget in a minimal host App and drive it via the Pilot API.
"""

from __future__ import annotations

import asyncio

from mewbo_cli.tui.input.completion import CommandCandidate, CompletionEngine
from mewbo_cli.tui.input.history import PromptHistory
from mewbo_cli.tui.seams import InputGateway
from mewbo_cli.tui.widgets.input_area import InputArea
from textual.app import App, ComposeResult

FILES = ["src/app.py", "src/widgets/header.py", "README.md"]
CMDS = [
    CommandCandidate("help", "Show help"),
    CommandCandidate("plan", "Toggle plan", argument_hint="on|off"),
]


def _engine():
    return CompletionEngine(
        files_provider=lambda: list(FILES),
        commands_provider=lambda: list(CMDS),
    )


class _Host(App):
    def __init__(self, *, engine=None, history=None) -> None:
        super().__init__()
        self._engine = engine
        self._history = history

    def compose(self) -> ComposeResult:
        yield InputArea(InputGateway(), id="ia")

    def on_mount(self) -> None:
        ia = self.query_one(InputArea)
        if self._engine is not None:
            ia.set_completion_engine(self._engine)
        if self._history is not None:
            ia.set_history(self._history)


def test_overlay_shows_file_candidates_on_at():
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.value = "@app"
            ia.cursor_position = 4
            ia._refresh_completions()
            await pilot.pause()
            assert ia._overlay_visible
            assert ia._candidates[0].display == "src/app.py"

    asyncio.run(_run())


def test_overlay_hidden_when_empty():
    """The screen-mounted overlay must not render an empty box with no candidates.

    Regression: its `.input--completion { display: none }` DEFAULT_CSS is scoped
    to the widget subtree and never reaches the screen-mounted overlay, so
    `display` is driven imperatively. Empty input → hidden; candidates → shown;
    cleared → hidden again.
    """

    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            await pilot.pause()
            assert ia._overlay is not None
            assert ia._overlay.display is False  # nothing typed → no empty box
            ia.value = "@app"
            ia.cursor_position = 4
            ia._refresh_completions()
            await pilot.pause()
            assert ia._overlay.display is True  # candidates → shown
            ia.value = ""
            ia.cursor_position = 0
            ia._refresh_completions()
            await pilot.pause()
            assert ia._overlay.display is False  # cleared → hidden again

    asyncio.run(_run())


def test_tab_accepts_completion_and_splices_token():
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.value = "look at @app"
            ia.cursor_position = len(ia.value)
            ia._refresh_completions()
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert ia.value == "look at @src/app.py "
            assert not ia._overlay_visible

    asyncio.run(_run())


def test_shift_down_inserts_without_dismiss():
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.value = "@"
            ia.cursor_position = 1
            ia._refresh_completions()
            await pilot.pause()
            await pilot.press("shift+down")
            await pilot.pause()
            # value updated to a candidate, overlay still open (no trailing space)
            assert ia.value.startswith("@")
            assert ia._overlay_visible

    asyncio.run(_run())


def test_escape_dismisses_overlay():
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.value = "/he"
            ia.cursor_position = 3
            ia._refresh_completions()
            await pilot.pause()
            assert ia._overlay_visible
            await pilot.press("escape")
            await pilot.pause()
            assert not ia._overlay_visible

    asyncio.run(_run())


def test_cursor_move_out_of_token_hides_stale_overlay():
    # Regression: moving the caret out of the @token (home) fires no
    # Input.Changed, but the overlay must hide rather than keep stale candidates.
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.value = "@app"
            ia.cursor_position = 4
            ia._refresh_completions()
            await pilot.pause()
            assert ia._overlay_visible
            await pilot.press("home")  # caret now at col 0, outside the token
            await pilot.pause()
            assert not ia._overlay_visible

    asyncio.run(_run())


def test_typing_resets_history_navigation(tmp_path):
    # Regression: after browsing history, typing must reset the browse
    # cursor so the next up starts fresh (not jump from the stale index) and a
    # later down doesn't discard the user's mid-browse edit.
    async def _run() -> None:
        hist = PromptHistory(path=tmp_path / "hist")
        hist._entries = ["older", "newer"]
        app = _Host(engine=_engine(), history=hist)
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            await pilot.press("up")  # -> "newer", hist_index = 0
            await pilot.pause()
            assert ia.value == "newer"
            assert ia._hist_index == 0
            await pilot.press("x")  # user edits -> history nav must reset
            await pilot.pause()
            assert ia._hist_index is None
            await pilot.press("up")  # fresh browse from the edited draft
            await pilot.pause()
            assert ia.value == "newer"  # newest again, not jumped to "older"

    asyncio.run(_run())


def test_busy_queues_instead_of_submitting():
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.set_busy(True)
            ia.value = "queued msg"
            await pilot.press("enter")
            await pilot.pause()
            assert ia.queued_count() == 1
            assert ia.drain_next() == "queued msg"
            assert ia.queued_count() == 0

    asyncio.run(_run())


def test_escape_pulls_queued_message_back():
    async def _run() -> None:
        app = _Host(engine=_engine())
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.enqueue("first")
            ia.enqueue("second")
            await pilot.press("escape")
            await pilot.pause()
            # most-recent queued message pulled back into the box
            assert ia.value == "second"
            assert ia.queued_count() == 1

    asyncio.run(_run())


def test_up_arrow_walks_history(tmp_path):
    async def _run() -> None:
        hist = PromptHistory(path=tmp_path / "hist")
        hist._entries = ["older", "newer"]
        app = _Host(engine=_engine(), history=hist)
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            await pilot.press("up")
            await pilot.pause()
            assert ia.value == "newer"
            await pilot.press("up")
            await pilot.pause()
            assert ia.value == "older"

    asyncio.run(_run())


def test_ctrl_r_opens_reverse_search(tmp_path):
    async def _run() -> None:
        hist = PromptHistory(path=tmp_path / "hist")
        hist._entries = ["run tests", "fix bug"]
        app = _Host(engine=_engine(), history=hist)
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert ia._rsearch_active
            await pilot.press("t")  # narrow to "tests"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert ia.value == "run tests"
            assert not ia._rsearch_active

    asyncio.run(_run())


def test_history_recorded_on_submit(tmp_path):
    async def _run() -> None:
        hist = PromptHistory(path=tmp_path / "hist")
        app = _Host(engine=_engine(), history=hist)
        async with app.run_test() as pilot:
            ia = app.query_one(InputArea)
            ia.focus()
            ia.value = "remember me"
            await pilot.press("enter")
            await pilot.pause()
            assert "remember me" in hist.entries()

    asyncio.run(_run())
