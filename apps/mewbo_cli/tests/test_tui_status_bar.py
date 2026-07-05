#!/usr/bin/env python3
"""Tests for the StatusBar context/cost gauge sidebar widget (issue #156).

The bar is now a focused context-window + cost gauge — model, branch and raw
token totals moved to the footer status line, so nothing is duplicated between
the sidebar and the footer. Mounts the widget in a minimal host App and drives
it via the Pilot API (pattern mirrored from test_tui_widgets.py).
"""

from __future__ import annotations

import asyncio

from mewbo_cli.tui.status.context_meter import ContextMeter
from mewbo_cli.tui.widgets.status_bar import StatusBar, StatusState
from textual.app import App, ComposeResult


class _StatusApp(App):
    """Minimal host app for StatusBar."""

    def __init__(self, meter: ContextMeter) -> None:
        super().__init__()
        self._meter = meter

    def compose(self) -> ComposeResult:
        yield StatusBar(meter=self._meter, id="sbar")


def _run(coro) -> None:
    asyncio.run(coro())


def test_status_bar_renders_context_gauge() -> None:
    """The bar mounts and renders a single context-gauge row."""
    meter = ContextMeter(windows={"m": 1000}, default_window=1000)

    async def go() -> None:
        app = _StatusApp(meter)
        async with app.run_test() as pilot:
            bar = app.query_one("#sbar", StatusBar)
            bar.apply_state(StatusState(model="openai/m", used_tokens=100))
            await pilot.pause()
            lines = bar.lines()
            assert len(lines) == 1
            assert "ctx" in lines[0]
            assert "10.0%" in lines[0]

    _run(go)


def test_status_bar_warning_style_over_80_percent() -> None:
    """Above 80% the context row uses the warning color var."""
    meter = ContextMeter(windows={"m": 100}, default_window=100)

    async def go() -> None:
        app = _StatusApp(meter)
        async with app.run_test() as pilot:
            bar = app.query_one("#sbar", StatusBar)
            bar.apply_state(StatusState(model="m", used_tokens=90))
            await pilot.pause()
            joined = "\n".join(bar.lines())
            assert "$warning" in joined
            assert "90.0%" in joined

    _run(go)


def test_status_bar_estimate_marker() -> None:
    """An unknown model (estimated window) prefixes the percent with ~."""
    meter = ContextMeter(windows={}, default_window=1000)

    async def go() -> None:
        app = _StatusApp(meter)
        async with app.run_test() as pilot:
            bar = app.query_one("#sbar", StatusBar)
            bar.apply_state(StatusState(model="unknown", used_tokens=100))
            await pilot.pause()
            assert "~10.0%" in "\n".join(bar.lines())

    _run(go)


def test_status_bar_no_estimate_marker_on_exact_window() -> None:
    """An exact-window model has no ~ prefix and uses the success color."""
    meter = ContextMeter(windows={"m": 1000}, default_window=128000)

    async def go() -> None:
        app = _StatusApp(meter)
        async with app.run_test() as pilot:
            bar = app.query_one("#sbar", StatusBar)
            bar.apply_state(StatusState(model="m", used_tokens=100))
            await pilot.pause()
            joined = "\n".join(bar.lines())
            assert "~" not in joined
            assert "$success" in joined

    _run(go)


def test_status_bar_cost_dash_when_unknown() -> None:
    """An unpriced model shows the honest em-dash cost."""
    meter = ContextMeter(windows={"made-up": 1000}, default_window=1000)

    async def go() -> None:
        app = _StatusApp(meter)
        async with app.run_test() as pilot:
            bar = app.query_one("#sbar", StatusBar)
            bar.apply_state(
                StatusState(model="made-up", input_tokens=50, output_tokens=50, used_tokens=100)
            )
            await pilot.pause()
            assert "—" in "\n".join(bar.lines())

    _run(go)


def test_status_bar_does_not_duplicate_footer_facets() -> None:
    """Model name, branch and raw token totals are NOT rendered (footer owns them)."""
    meter = ContextMeter(windows={"gpt-oss-120b": 100000}, default_window=100000)

    async def go() -> None:
        app = _StatusApp(meter)
        async with app.run_test() as pilot:
            bar = app.query_one("#sbar", StatusBar)
            bar.apply_state(
                StatusState(
                    model="openai/gpt-oss-120b",
                    input_tokens=1200,
                    output_tokens=300,
                    used_tokens=1500,
                    branch="feature-x",
                )
            )
            await pilot.pause()
            joined = "\n".join(bar.lines())
            assert "gpt-oss-120b" not in joined  # model is footer-only
            assert "feature-x" not in joined  # branch is footer-only
            assert "tok" not in joined  # raw token totals are footer-only

    _run(go)
