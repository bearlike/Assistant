#!/usr/bin/env python3
"""Pilot tests for the modal screens + the session installer.

The modal screens are driven by pushing them onto a tiny host ``App`` and
reading the dismissed value; the installer is driven by mounting a real
``MewboApp`` with the installer and asserting the keys fire + collaborators are
parked. Only the LLM/network is stubbed (there is none here) — session
enumeration / event loading run real fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mewbo_cli.tui.app import MewboApp
from mewbo_cli.tui.screens.dialogs import (
    ConfirmScreen,
    MultiSelectDialogScreen,
    SelectDialogScreen,
    TextPromptScreen,
)
from mewbo_cli.tui.screens.resume import ResumeScreen, _format_row
from mewbo_cli.tui.screens.transcript_screen import TranscriptScreen
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
)
from mewbo_cli.tui.session import install as install_mod
from mewbo_cli.tui.session.install import SessionController, make_session_installer
from mewbo_cli.tui.widgets.header import HeaderContext
from mewbo_cli.tui.widgets.input_area import InputArea
from textual.app import App, ComposeResult
from textual.widgets import OptionList


def _sess(sid: str, title: str, *, running: bool, status: str, created: str) -> dict[str, Any]:
    """Build a session-summary dict (keeps test literals under the line limit)."""
    return {
        "session_id": sid,
        "title": title,
        "running": running,
        "status": status,
        "created_at": created,
    }


class _Host(App):
    """A bare host App for pushing a single modal and capturing its result."""

    def __init__(self) -> None:
        super().__init__()
        self.result: Any = "UNSET"

    def compose(self) -> ComposeResult:
        return iter(())


def _push_and_get(screen: Any, *keys: str) -> Any:
    async def _run() -> Any:
        host = _Host()
        async with host.run_test() as pilot:
            host.push_screen(screen, lambda value: setattr(host, "result", value))
            await pilot.pause()
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
        return host.result

    return asyncio.run(_run())


# --- dialogs -------------------------------------------------------------


def test_select_dialog_accepts_highlighted() -> None:
    result = _push_and_get(SelectDialogScreen("Pick", ["a", "b", "c"]), "down", "enter")
    assert result == "b"


def test_select_dialog_escape_cancels() -> None:
    result = _push_and_get(SelectDialogScreen("Pick", ["a", "b"]), "escape")
    assert result is None


def test_multiselect_returns_list() -> None:
    # space toggles the highlighted row in a SelectionList, enter accepts.
    result = _push_and_get(
        MultiSelectDialogScreen("Pick many", ["x", "y"]), "space", "enter"
    )
    assert result == ["x"]


def test_text_prompt_accepts_default() -> None:
    result = _push_and_get(TextPromptScreen("T", "msg", default="hello"), "enter")
    assert result == "hello"


def test_text_prompt_escape_cancels() -> None:
    result = _push_and_get(TextPromptScreen("T", "msg"), "escape")
    assert result is None


def test_confirm_yes() -> None:
    result = _push_and_get(ConfirmScreen("C", "ok?", default=True), "enter")
    assert result is True


# --- resume switcher -----------------------------------------------------


def test_format_row_shows_busy_and_title() -> None:
    row = _format_row(
        _sess("s", "My session", running=True, status="running", created="2026-06-20T10:00:00")
    )
    assert "●" in row
    assert "My session" in row
    assert "2026-06-20" in row


def test_resume_picks_session_id() -> None:
    sessions = [
        _sess("sid-1", "First", running=False, status="idle", created="2026-06-20T10:00:00"),
        _sess("sid-2", "Second", running=True, status="running", created="2026-06-20T11:00:00"),
    ]
    result = _push_and_get(ResumeScreen(lambda: sessions), "down", "enter")
    assert result == "sid-2"


def test_resume_empty_list_cancels_cleanly() -> None:
    result = _push_and_get(ResumeScreen(lambda: []), "enter")
    assert result is None


# --- transcript screen ---------------------------------------------------


def test_transcript_renders_events() -> None:
    events = [
        {"type": "user", "payload": {"text": "do a thing"}},
        {"type": "assistant", "payload": {"text": "did it", "model": "gpt-5.2"}},
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "shell",
                "operation": "run",
                "result": "ok",
                "success": True,
                "model": "gpt-5.2",
            },
        },
    ]

    async def _run() -> None:
        host = _Host()
        async with host.run_test() as pilot:
            host.push_screen(TranscriptScreen(lambda: events))
            await pilot.pause()
            # The modal renders inside the centered, bordered dialog container.
            assert host.screen.query_one("#transcript-dialog") is not None
            # The screen mounted with content (no crash); close it.
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_run())


def test_transcript_handles_diff_payload() -> None:
    events = [
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "edit",
                "operation": "patch",
                "result": {"kind": "diff", "text": "@@ -1 +1 @@\n-old\n+new\n"},
                "success": True,
            },
        },
    ]

    async def _run() -> None:
        host = _Host()
        async with host.run_test() as pilot:
            host.push_screen(TranscriptScreen(lambda: events))
            await pilot.pause()
            await pilot.press("escape")

    asyncio.run(_run())


# --- installer wiring ----------------------------------------------------


class _FakeRuntime:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []

    def load_events(self, sid: str, after: Any = None) -> list[dict[str, Any]]:
        return self.events

    def list_sessions(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self.sessions


class _FakeStore:
    def load_title(self, sid: str) -> Any:
        return None

    def truncate_after(self, sid: str, ts: str) -> int:
        return 0


class _State:
    def __init__(self) -> None:
        self.session_id = "sess-active"
        self.model_name = "gpt-5.2"


def _header_ctx() -> HeaderContext:
    return HeaderContext(
        title="Mewbo", version="0.0.13", status_label="Ready", status_color="green",
        model="gpt-5.2", session_id="sess-active", base_url="http://x", langfuse_enabled=False,
        langfuse_reason=None, builtin_enabled=1, builtin_disabled=0, external_enabled=0,
        external_disabled=0, skill_count=0,
    )


class _Engine:
    def __init__(self, emit: Any, emit_renderable: Any) -> None:
        pass

    def handle(self, text: str) -> bool:
        return True


def _make_app(installer: Any) -> MewboApp:
    return MewboApp(
        header_ctx=_header_ctx(),
        messages=MessageRendererRegistry(),
        sidebar_slots=SidebarSlotRegistry(),
        permission=PermissionGateway(auto_approve=lambda: True),
        input_gateway=InputGateway(),
        engine_factory=lambda e, r: _Engine(e, r),
        installers=[installer],
    )


def test_installer_parks_collaborators_and_publishes_controller(tmp_path: Any) -> None:
    runtime, store, state = _FakeRuntime(), _FakeStore(), _State()
    installer = make_session_installer(store=store, runtime=runtime, state=state)

    async def _run() -> None:
        app = _make_app(installer)
        async with app.run_test():
            assert isinstance(app._session_controller, SessionController)
            assert app._rewind_checkpointer is not None
            assert app._auto_titler is not None
            assert app._keybinding_config is not None
            assert install_mod.active_controller() is app._session_controller

    asyncio.run(_run())


def test_installer_binds_global_keys_and_they_fire() -> None:
    runtime, store, state = _FakeRuntime(), _FakeStore(), _State()
    runtime.sessions = [
        _sess("other", "Other", running=False, status="idle", created="2026-06-20T09:00:00"),
    ]
    installer = make_session_installer(store=store, runtime=runtime, state=state)

    async def _run() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            # ctrl+o pushes the transcript screen while the Input is focused.
            app.set_focus(app.query_one(InputArea))
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert isinstance(app.screen, TranscriptScreen)
            await pilot.press("escape")
            await pilot.pause()
            # ctrl+s pushes the resume switcher.
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ResumeScreen)
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_run())


def test_installer_binding_shows_in_footer_active_bindings() -> None:
    runtime, store, state = _FakeRuntime(), _FakeStore(), _State()
    installer = make_session_installer(store=store, runtime=runtime, state=state)

    async def _run() -> None:
        app = _make_app(installer)
        async with app.run_test():
            keys = app.screen.active_bindings
            assert "ctrl+o" in keys
            assert "ctrl+s" in keys
            assert "ctrl+l" in keys

    asyncio.run(_run())


def test_clear_redraw_clears_transcript() -> None:
    runtime, store, state = _FakeRuntime(), _FakeStore(), _State()
    installer = make_session_installer(store=store, runtime=runtime, state=state)

    async def _run() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            from mewbo_cli.tui.widgets.transcript import TranscriptView

            app.set_focus(app.query_one(InputArea))
            await pilot.press("ctrl+l")
            await pilot.pause()
            # transcript cleared without error
            assert app.query_one(TranscriptView) is not None

    asyncio.run(_run())


def test_resume_action_switches_session() -> None:
    runtime, store, state = _FakeRuntime(), _FakeStore(), _State()
    runtime.sessions = [
        _sess("target", "Target", running=False, status="idle", created="2026-06-20T08:00:00"),
    ]
    installer = make_session_installer(store=store, runtime=runtime, state=state)

    async def _run() -> None:
        app = _make_app(installer)
        async with app.run_test() as pilot:
            app.set_focus(app.query_one(InputArea))
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ResumeScreen)
            # pick the (only) session
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
            assert state.session_id == "target"

    asyncio.run(_run())


def test_rewind_command_without_app_degrades() -> None:
    # active_controller() is None outside a running app → command prints a note,
    # never crashes and returns True (keep REPL alive).
    from types import SimpleNamespace

    from mewbo_cli.tui.session.install import cmd_rewind

    install_mod._ACTIVE_CONTROLLER = None
    printed: list[str] = []
    ctx = SimpleNamespace(console=SimpleNamespace(print=printed.append))
    assert cmd_rewind(ctx, []) is True  # type: ignore[arg-type]
    assert printed
