#!/usr/bin/env python3
"""Tests for make_sidebar_installer + cmd_context (issue #161, epic #149).

Drives the installer against a real ``MewboApp`` (imported read-only) to prove
the three faceted sidebar sections (Fleet · Plan · Context) appear and the
widgets are exposed for the controller; tests ``cmd_context`` against a fake
CommandContext.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub
from mewbo_cli.tui.app import MewboApp
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
)
from mewbo_cli.tui.status.context_meter import ContextMeter
from mewbo_cli.tui.status.install import (
    _build_statusline_state,
    cmd_context,
    make_sidebar_installer,
)
from mewbo_cli.tui.status.statusline import StatusLineRunner
from mewbo_cli.tui.widgets.fleet_panel import FleetPanel
from mewbo_cli.tui.widgets.header import HeaderContext
from mewbo_cli.tui.widgets.sidebar import SidebarSection, SidebarView
from mewbo_cli.tui.widgets.status_bar import StatusBar
from mewbo_cli.tui.widgets.todo_panel import TodoPanel, TodoState


def _header_ctx() -> HeaderContext:
    return HeaderContext(
        title="Mewbo",
        version="0.0",
        status_label="ready",
        status_color="green",
        model="openai/m",
        session_id="sess1234abcd",
        base_url="http://x",
        langfuse_enabled=False,
        langfuse_reason=None,
        builtin_enabled=1,
        builtin_disabled=0,
        external_enabled=0,
        external_disabled=0,
    )


def _make_app(installer) -> MewboApp:
    return MewboApp(
        header_ctx=_header_ctx(),
        messages=MessageRendererRegistry(),
        sidebar_slots=SidebarSlotRegistry(),
        permission=PermissionGateway(auto_approve=lambda: False),
        input_gateway=InputGateway(),
        engine_factory=lambda emit, emit_r: SimpleNamespace(handle=lambda text: True),
        installers=[installer],
    )


def _installer(**kw):
    """Build a sidebar installer with the required hub/registry defaults."""
    kw.setdefault("hub", AgentTranscriptHub())
    kw.setdefault("registry", MessageRendererRegistry())
    return make_sidebar_installer(**kw)


def test_installer_registers_three_faceted_sections() -> None:
    """The installer registers Fleet · Plan · Context as ordered sections."""
    state = SimpleNamespace(model_name="openai/gpt-oss-120b", session_id="sess1234")
    installer = _installer(
        state=state,
        statusline=StatusLineRunner(script=""),  # disabled
        meter=ContextMeter(windows={}, default_window=1000),
        queue_count_provider=lambda: 0,
    )

    async def go() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            names = [name for name, _ in app.sidebar_slots.slots()]
            # Ordered: fleet (10) → plan (20) → context (30).
            assert names == ["fleet", "plan", "context"]
            sidebar = app.query_one(SidebarView)
            sections = sidebar.query(SidebarSection)
            assert len(sections) == 3
            # The widgets are exposed for the controller's turn worker.
            assert isinstance(app._fleet_panel, FleetPanel)
            assert isinstance(app._todo_panel, TodoPanel)
            assert isinstance(app._status_bar, StatusBar)
            # The drill controller is wired up.
            assert app._drill_controller is not None

    asyncio.run(go())


def test_installer_seeds_context_gauge() -> None:
    """The seeded status bar renders the context gauge (model only feeds the meter)."""
    state = SimpleNamespace(model_name="openai/gpt-oss-120b", session_id="s")
    installer = _installer(
        state=state,
        statusline=StatusLineRunner(script=""),
        meter=ContextMeter(windows={}, default_window=1000),
    )

    async def go() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            bar = app._status_bar
            joined = "\n".join(bar.lines())
            assert "ctx" in joined
            # The model id itself is the footer's job — not duplicated here.
            assert "openai" not in joined

    asyncio.run(go())


def test_installer_does_not_break_app_without_providers() -> None:
    """A minimal installer (no providers, disabled statusline) mounts cleanly."""
    state = SimpleNamespace(model_name=None, session_id=None)
    installer = _installer(state=state, statusline=StatusLineRunner(script=""))

    async def go() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app._fleet_panel, FleetPanel)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# cmd_context
# ---------------------------------------------------------------------------


@dataclass
class _FakeConsole:
    printed: list[str]

    def print(self, text: str) -> None:
        self.printed.append(str(text))


def test_cmd_context_prints_breakdown() -> None:
    """/context prints a token-attribution breakdown and returns True."""
    console = _FakeConsole(printed=[])
    runtime = SimpleNamespace(
        load_events=lambda sid: [
            {"payload": {"usage": {"input_tokens": 600, "output_tokens": 400}}},
        ]
    )
    ctx = SimpleNamespace(
        console=console,
        state=SimpleNamespace(model_name="m", session_id="s"),
        runtime=runtime,
    )
    result = cmd_context(ctx, [])
    assert result is True
    out = "\n".join(console.printed)
    assert "Context attribution" in out
    assert "1,000 tokens" in out  # used total
    assert "input" in out and "600" in out


def test_cmd_context_handles_missing_runtime() -> None:
    """/context degrades gracefully when the runtime can't supply events."""
    console = _FakeConsole(printed=[])
    ctx = SimpleNamespace(
        console=console,
        state=SimpleNamespace(model_name=None, session_id=None),
        runtime=None,
    )
    assert cmd_context(ctx, []) is True
    assert any("Context attribution" in p for p in console.printed)


def test_cmd_context_over_warning_hint(monkeypatch) -> None:
    """A >80% session surfaces the compact hint.

    Force a tiny default window via config so the 950 used tokens read as 95%.
    """
    console = _FakeConsole(printed=[])
    runtime = SimpleNamespace(
        load_events=lambda sid: [
            {"payload": {"usage": {"input_tokens": 900, "output_tokens": 50}}},
        ]
    )

    import mewbo_cli.tui.status.context_meter as cm

    def _fake_config(*keys, default=None):
        if keys == ("llm", "model_context_windows"):
            return {}
        if keys == ("llm", "default_context_window"):
            return 1000
        return default

    monkeypatch.setattr(cm, "get_config_value", _fake_config)
    ctx = SimpleNamespace(
        console=console,
        state=SimpleNamespace(model_name="x", session_id="s"),
        runtime=runtime,
    )
    cmd_context(ctx, [])
    assert any("over 80%" in p for p in console.printed)


def test_todo_provider_feeds_the_plan_dock() -> None:
    """The installer wires a todo provider through to the Plan dock."""
    state = SimpleNamespace(model_name="m", session_id="s")
    installer = _installer(
        state=state,
        statusline=StatusLineRunner(script=""),
        todo_provider=lambda: TodoState(total=2, done=1, current="x"),
    )

    async def go() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            app._todo_panel.apply()
            await pilot.pause()
            assert "1/2 todo" in str(app._todo_panel.content)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# statusLine payload — live fields (I1) + non-blocking offload (C1)
# ---------------------------------------------------------------------------


def test_statusline_payload_carries_live_fields() -> None:
    """The statusLine state carries real token/context/cost, not zeros (I1)."""
    runtime = SimpleNamespace(
        load_events=lambda sid: [
            {"payload": {"usage": {"input_tokens": 600, "output_tokens": 400}}},
        ]
    )
    meter = ContextMeter(windows={"gpt-oss-120b": 4000}, default_window=4000)
    state = _build_statusline_state(
        model="openai/gpt-oss-120b",
        work_dir="/tmp/x",
        branch="main",
        session_id="s",
        runtime=runtime,
        meter=meter,
    )
    assert state.input_tokens == 600
    assert state.output_tokens == 400
    assert state.context_used == 1000
    assert state.context_total == 4000
    assert state.context_percent == 25.0
    assert state.context_estimated is False
    # Serialized payload must expose the same non-zero live fields.
    payload = StatusLineRunner.build_payload(state)
    assert payload["tokens"] == {"input": 600, "output": 400, "total": 1000}
    assert payload["context_window"]["used"] == 1000
    assert payload["context_window"]["total"] == 4000


def test_statusline_payload_zeros_without_runtime() -> None:
    """With no runtime the live fields fall back to zeros (graceful)."""
    state = _build_statusline_state(
        model="m",
        work_dir="/tmp",
        branch=None,
        session_id=None,
        runtime=None,
        meter=ContextMeter(windows={}, default_window=1000),
    )
    assert state.input_tokens == 0
    assert state.output_tokens == 0
    assert state.context_used == 0


def test_statusline_runs_offloaded_off_event_loop() -> None:
    """A configured statusLine script is offloaded (the loop is not blocked).

    The runner records the thread it executed on; it must differ from the event
    loop's running thread, proving ``asyncio.to_thread`` offloads the blocking
    ``subprocess.run`` so a slow script can't freeze the TUI.
    """
    import threading

    run_threads: list[int] = []

    class _RecordingRunner(StatusLineRunner):
        def run(self, state):  # type: ignore[override]
            run_threads.append(threading.get_ident())
            return "status-output"

    runner = _RecordingRunner(script="x")  # enabled
    state = SimpleNamespace(model_name="m", session_id="s")
    installer = _installer(
        state=state,
        statusline=runner,
        meter=ContextMeter(windows={}, default_window=1000),
    )

    async def go() -> None:
        app = _make_app(installer)
        loop_thread = threading.get_ident()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            # Give the scheduled async tick + its to_thread offload time to run.
            for _ in range(5):
                await pilot.pause()
                if run_threads:
                    break
            assert run_threads, "statusline runner was never invoked"
            assert all(t != loop_thread for t in run_threads), (
                "statusline ran on the event-loop thread (would block the TUI)"
            )
            assert app.sub_title == "status-output"

    asyncio.run(go())
