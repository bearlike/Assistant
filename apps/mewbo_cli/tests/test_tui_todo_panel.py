#!/usr/bin/env python3
"""Tests for TodoPanel — the pinned Plan/Todo dock (#161, epic #149)."""

from __future__ import annotations

import asyncio

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.tui.widgets.todo_panel import TodoItem, TodoPanel, TodoState
from textual.app import App, ComposeResult


class _DockApp(App):
    def __init__(self, todo_provider=None, queue_count_provider=None) -> None:
        super().__init__()
        self._todo_provider = todo_provider
        self._queue_count_provider = queue_count_provider

    def compose(self) -> ComposeResult:
        yield TodoPanel(
            todo_provider=self._todo_provider,
            queue_count_provider=self._queue_count_provider,
            id="dock",
        )


def _run(coro) -> None:
    asyncio.run(coro())


def _content(dock: TodoPanel) -> str:
    return str(dock.content)


def test_empty_dock_renders_no_plan() -> None:
    async def go() -> None:
        app = _DockApp(todo_provider=lambda: TodoState())
        async with app.run_test() as pilot:
            await pilot.pause()
            dock = app.query_one("#dock", TodoPanel)
            assert "no plan" in _content(dock)
            assert "todo" not in _content(dock)

    _run(go)


def test_summary_and_queue_pills() -> None:
    async def go() -> None:
        app = _DockApp(
            todo_provider=lambda: TodoState(total=5, done=2, current="writing tests"),
            queue_count_provider=lambda: 3,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            dock = app.query_one("#dock", TodoPanel)
            text = _content(dock)
            assert "2/5 todo" in text
            assert "3 queued" in text

    _run(go)


def test_tristate_checklist() -> None:
    async def go() -> None:
        items = [
            TodoItem(label="read config", state="done"),
            TodoItem(label="edit module", state="in_progress"),
            TodoItem(label="run tests", state="pending"),
        ]
        app = _DockApp(todo_provider=lambda: TodoState.from_items(items))
        async with app.run_test() as pilot:
            await pilot.pause()
            dock = app.query_one("#dock", TodoPanel)
            text = _content(dock)
            assert "1/3 todo" in text
            assert f"{ICONS.check} read config" in text
            assert "→ edit module" in text
            assert "• run tests" in text

    _run(go)


def test_apply_repulls_providers() -> None:
    """apply() re-pulls the live providers so the dock reflects new state."""
    state = {"todo": TodoState()}

    async def go() -> None:
        app = _DockApp(todo_provider=lambda: state["todo"])
        async with app.run_test() as pilot:
            await pilot.pause()
            dock = app.query_one("#dock", TodoPanel)
            assert "no plan" in _content(dock)
            state["todo"] = TodoState(total=2, done=1, current="x")
            dock.apply()
            await pilot.pause()
            assert "1/2 todo" in _content(dock)

    _run(go)


def test_expand_shows_current_without_items() -> None:
    async def go() -> None:
        app = _DockApp(
            todo_provider=lambda: TodoState(total=3, done=1, current="refactor module"),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            dock = app.query_one("#dock", TodoPanel)
            dock.toggle_pills()
            await pilot.pause()
            assert dock.pills_expanded is True
            assert "→ refactor module" in _content(dock)

    _run(go)
