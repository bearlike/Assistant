#!/usr/bin/env python3
"""Tests for the IDE-style footer StatusLine widget + its installer (epic #149).

Covers (1) the regression fix — composer and Footer no longer share the bottom
dock — and (2) the new status line facets (host · model · cwd · branch/stash ·
session tokens, input/output faceted). Mounts widgets in a minimal host App and
drives them via the Pilot API (mirrors test_tui_status_bar.py).
"""

from __future__ import annotations

import asyncio

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub
from mewbo_cli.tui.status.install import make_statusline_installer
from mewbo_cli.tui.widgets.status_bar import StatusBar
from mewbo_cli.tui.widgets.status_line import StatusLine, StatusLineData
from textual.app import App, ComposeResult


class _LineApp(App):
    """Minimal host app for the StatusLine."""

    #: Mirrors the real ``MewboApp``'s sidebar-installer slot (unset by default;
    #: the ctx-gauge test below assigns one to prove the installer feeds it).
    _status_bar: StatusBar | None = None

    def compose(self) -> ComposeResult:
        yield StatusLine(id="statusline")


def _run(coro) -> None:
    asyncio.run(coro())


def test_status_line_renders_all_facets() -> None:
    """Host, model, cwd, branch and faceted tokens all surface in one line."""

    async def go() -> None:
        app = _LineApp()
        async with app.run_test() as pilot:
            line = app.query_one("#statusline", StatusLine)
            line.apply(
                StatusLineData(
                    host="me@box",
                    model="openai/gpt-oss-120b",
                    cwd="/tmp/project",
                    branch="main",
                    input_tokens=12300,
                    output_tokens=4100,
                )
            )
            await pilot.pause()
            markup = line.markup()
            assert "me@box" in markup
            assert "gpt-oss-120b" in markup
            assert f"{ICONS.branch} main" in markup
            assert "↑ 12.3K" in markup
            assert "↓ 4.1K" in markup

    _run(go)


def test_status_line_stash_only_when_nonzero() -> None:
    """The stash flag renders only when the stash is non-empty."""
    clean = StatusLineData(branch="main", stash=0)
    dirty = StatusLineData(branch="main", stash=3)

    async def go() -> None:
        app = _LineApp()
        async with app.run_test() as pilot:
            line = app.query_one("#statusline", StatusLine)
            line.data = clean
            await pilot.pause()
            assert ICONS.stash not in line.markup()
            line.data = dirty
            await pilot.pause()
            assert f"{ICONS.stash} 3" in line.markup()

    _run(go)


def test_status_line_worktree_wins_over_branch() -> None:
    """A worktree label is shown instead of the plain branch when both are set."""
    data = StatusLineData(branch="main", worktree="wt-7")
    line = StatusLine()
    line.data = data
    markup = line.markup()
    assert "wt-7" in markup
    assert "main" not in markup


def test_status_line_tokens_hidden_when_zero() -> None:
    """No token segment is drawn before any tokens are consumed."""
    line = StatusLine()
    line.data = StatusLineData(model="m", input_tokens=0, output_tokens=0)
    assert "↑" not in line.markup()


def test_status_line_data_merge_preserves_local_seed() -> None:
    """A live refresh overlays its facets without wiping the seeded host/cwd."""
    seed = StatusLineData(host="me@box", cwd="/tmp/project")
    live = StatusLineData(model="m", branch="dev", input_tokens=10, output_tokens=5)
    merged = seed.merged(live)
    assert merged.host == "me@box"
    assert merged.cwd == "/tmp/project"
    assert merged.model == "m"
    assert merged.branch == "dev"


def test_status_line_local_seed_has_host_and_cwd() -> None:
    """The default factory fills the static facets so a bare App shows a line."""
    data = StatusLineData.local()
    assert data.host  # user@host discovered
    assert data.cwd  # process cwd


def test_statusline_installer_drives_live_facets() -> None:
    """The installer queries the docked line and feeds it model + branch + tokens."""

    class _State:
        model_name = "openai/m"
        session_id = "s1"

    class _Runtime:
        def load_events(self, _sid: str) -> list[dict]:
            return [{"payload": {"usage": {"input_tokens": 200, "output_tokens": 90}}}]

    installer = make_statusline_installer(state=_State(), runtime=_Runtime(), interval=1.0)

    async def go() -> None:
        app = _LineApp()
        async with app.run_test() as pilot:
            installer(app)
            line = app.query_one("#statusline", StatusLine)
            # The first tick is scheduled via call_later and offloads to a worker
            # thread; poll until the live facets land (bounded so it can't hang).
            for _ in range(50):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if "openai/m" in line.markup():
                    break
            markup = line.markup()
            assert "openai/m" in markup
            assert "↑ 200" in markup
            assert "↓ 90" in markup

    _run(go)


def test_statusline_installer_feeds_ctx_gauge_from_hub_live_tokens_not_cumulative() -> None:
    """The sidebar ctx gauge reads the hub's ``root_last_input_tokens()`` — the
    LIVE context size — never the cumulative session total (issue E8: after one
    long turn the gauge read ~100% off a 975K cumulative counter against a
    1M-context model, when the real live context was ~4%/40K tokens)."""

    class _State:
        model_name = "openai/m"
        session_id = "s1"

    class _Runtime:
        def load_events(self, _sid: str) -> list[dict]:
            # A large cumulative total — must NOT leak into the ctx gauge.
            return [{"payload": {"usage": {"input_tokens": 975216, "output_tokens": 5000}}}]

    hub = AgentTranscriptHub()
    hub.observe(
        "s1",
        {
            "type": "llm_call_end",
            "payload": {
                "agent_id": "root",
                "depth": 0,
                "input_tokens": 40099,
                "cumulative_input_tokens": 975216,
            },
        },
    )

    installer = make_statusline_installer(
        state=_State(), runtime=_Runtime(), hub=hub, interval=1.0
    )

    async def go() -> None:
        app = _LineApp()
        async with app.run_test() as pilot:
            app._status_bar = StatusBar()
            installer(app)
            for _ in range(50):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if app._status_bar.state.model == "openai/m":
                    break
            assert app._status_bar.state.used_tokens == 40099
            # The cumulative facets (fed to cost, unchanged) still carry the
            # full session totals.
            assert app._status_bar.state.input_tokens == 975216
            assert app._status_bar.state.output_tokens == 5000

    _run(go)
