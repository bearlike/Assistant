#!/usr/bin/env python3
"""Tests for FleetPanel — the selectable hypervisor fleet.

The acceptance bar: each agent is a SELECTABLE row whose content is a compact
summary (``glyph · label · model · N tools · elapsed · in→out``) with NO
tool-name dump; selecting a row hands its ``agent_id`` to the drill-in callback.
"""

from __future__ import annotations

import asyncio

from mewbo_cli.tui.agent_transcript_hub import FleetRow
from mewbo_cli.tui.widgets.fleet_panel import FleetPanel
from textual.app import App, ComposeResult


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class _FleetApp(App):
    def __init__(self, rows_provider, on_select=None) -> None:
        super().__init__()
        self._rows_provider = rows_provider
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        yield FleetPanel(
            rows_provider=self._rows_provider,
            on_select=self._on_select,
            clock=_Clock(),
            id="fleet",
        )


def _run(coro) -> None:
    asyncio.run(coro())


def _row(agent_id, parent_id, depth, **kw) -> FleetRow:
    base = dict(
        agent_id=agent_id,
        parent_id=parent_id,
        depth=depth,
        model="openai/gpt-oss-120b",
        status="running",
        tool_count=0,
        input_tokens=0,
        output_tokens=0,
        started_at=0.0,
        stopped_at=None,
        agent_type=None,
        is_root=(depth == 0),
    )
    base.update(kw)
    return FleetRow(**base)


def _prompts(panel: FleetPanel) -> list[str]:
    return [
        str(panel.get_option_at_index(i).prompt)
        for i in range(panel.option_count)
    ]


def test_idle_when_no_agents() -> None:
    """An empty fleet shows an idle marker, not a crash."""

    async def go() -> None:
        app = _FleetApp(lambda: [])
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            assert "idle" in " ".join(_prompts(panel))

    _run(go)


def test_row_is_a_compact_summary_not_a_tool_name_dump() -> None:
    """The row shows model · N tools · elapsed · in→out — and NO tool names."""
    rows = [
        _row(
            "root1234", None, 0,
            status="running", tool_count=3,
            input_tokens=1500, output_tokens=500,
            started_at=95.0,  # clock at 100 → 5s elapsed
        )
    ]

    async def go() -> None:
        app = _FleetApp(lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            text = " ".join(_prompts(panel))
            # Metadata facets are present.
            assert "gpt-oss-120b" in text  # model
            assert "3 tools" in text  # COUNT, a number — never tool names
            assert "5s" in text  # elapsed
            assert "1.5K→500" in text  # tokens in→out
            assert "root" in text  # depth-0 label
            # The state glyph for running is ●.
            assert "●" in text

    _run(go)


def test_no_tool_names_appear_in_rows() -> None:
    """Even with tools recorded, the row never spells a tool id (count only)."""
    rows = [_row("r0000001", None, 0, tool_count=2)]

    async def go() -> None:
        app = _FleetApp(lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            text = " ".join(_prompts(panel))
            assert "2 tools" in text
            # No bare tool ids leak (the old panel dumped e.g. "▸ bash").
            assert "bash" not in text
            assert "▸" not in text

    _run(go)


def test_tree_nesting_indents_children() -> None:
    """A parent/child fleet renders with tree connectors under the root."""
    rows = [
        _row("root0001", None, 0, agent_type=None),
        _row("child001", "root0001", 1, agent_type="researcher"),
    ]

    async def go() -> None:
        app = _FleetApp(lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            text = "\n".join(_prompts(panel))
            assert "root" in text
            assert "researcher" in text  # child labelled by its agent type
            assert "└─" in text or "├─" in text

    _run(go)


def test_state_glyphs_reflect_status() -> None:
    """Completed → ✓, failed → ✗ (the documented state map)."""
    rows = [
        _row("ok000001", None, 0, status="completed", started_at=1.0, stopped_at=2.0),
        _row("bad00001", "ok000001", 1, status="failed", started_at=1.0, stopped_at=2.0),
    ]

    async def go() -> None:
        app = _FleetApp(lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            text = " ".join(_prompts(panel))
            assert "✓" in text
            assert "✗" in text

    _run(go)


def test_blocked_and_unmet_goal_never_render_a_green_checkmark() -> None:
    """A blocked or unmet-goal root row must render its OWN glyph, never ✓.

    Regression for the false-success bug: the fleet row's status used to be a
    raw ``done_reason`` passthrough, so a ``blocked_code``-carrying completion
    (whose ``done_reason`` stays ``"completed"``) resolved straight to the
    ``completed`` glyph — a green ✓ on a run that never got past a wall.
    """
    rows = [
        _row("blk00001", None, 0, status="blocked", started_at=1.0, stopped_at=2.0),
        _row("ung00001", "blk00001", 1, status="unmet_goal", started_at=1.0, stopped_at=2.0),
    ]

    async def go() -> None:
        app = _FleetApp(lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            text = " ".join(_prompts(panel))
            assert "✓" not in text
            assert "⚠" in text  # blocked
            assert "◎" in text  # unmet_goal — distinct from blocked

    _run(go)


def test_selecting_a_row_invokes_on_select_with_agent_id() -> None:
    """Highlighting a row + Enter hands its agent_id to the drill-in callback."""
    picked: list[str] = []
    rows = [
        _row("root0001", None, 0),
        _row("child001", "root0001", 1, agent_type="probe"),
    ]

    async def go() -> None:
        app = _FleetApp(lambda: rows, on_select=picked.append)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            panel.focus()
            await pilot.pause()
            # Highlight the child (index 1) and select it.
            panel.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            assert picked == ["child001"]

    _run(go)


def test_selection_survives_a_refresh() -> None:
    """A rebuild (fleet grew) preserves the highlighted agent."""
    rows = [_row("root0001", None, 0)]

    async def go() -> None:
        app = _FleetApp(lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#fleet", FleetPanel)
            panel.focus()
            panel.highlighted = 0
            await pilot.pause()
            # Fleet grows; the highlighted root must stay highlighted.
            rows.append(_row("child001", "root0001", 1))
            panel.refresh_fleet()
            await pilot.pause()
            assert panel.selected_agent_id == "root0001"

    _run(go)
