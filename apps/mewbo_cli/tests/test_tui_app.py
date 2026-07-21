#!/usr/bin/env python3
"""Pilot tests for MewboApp — the foundation shell.

Drive the app through Textual's ``Pilot`` (``app.run_test()``), mirroring the
``asyncio.run(_run())`` wrapper used by the other TUI tests. The TurnEngine is
replaced by a fake so these tests exercise App wiring (compose, screen-state,
submit→engine→transcript, exit, seams, compact layout) without an LLM.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mewbo_cli.tui.app import MewboApp, ScreenState
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
    TranscriptItem,
)
from mewbo_cli.tui.widgets.header import HeaderContext
from mewbo_cli.tui.widgets.input_area import InputArea
from mewbo_cli.tui.widgets.sidebar import SidebarView
from mewbo_cli.tui.widgets.transcript import TranscriptView
from rich.text import Text
from textual.widgets import Footer


def _header_ctx() -> HeaderContext:
    return HeaderContext(
        title="Mewbo",
        version="0.0.13",
        status_label="Ready",
        status_color="green",
        model="openai/gpt-5.2",
        session_id="sess-123",
        base_url="http://localhost:4000",
        langfuse_enabled=False,
        langfuse_reason=None,
        builtin_enabled=3,
        builtin_disabled=0,
        external_enabled=1,
        external_disabled=0,
        skill_count=2,
    )


class _FakeEngine:
    """Stand-in for TurnEngine: echoes queries, ``/exit`` ends the loop."""

    def __init__(
        self,
        emit: Any,
        emit_renderable: Any,
    ) -> None:
        self.emit = emit
        self.emit_renderable = emit_renderable
        self.handled: list[str] = []

    def handle(self, text: str) -> bool:
        self.handled.append(text)
        if text.strip() == "/exit":
            return False
        self.emit(TranscriptItem("assistant", {"text": f"echo:{text}"}))
        return True

    def last_turn_outcome(self) -> str | None:
        """Stand-in for TurnEngine.last_turn_outcome — no real run, no outcome."""
        return None


def _make_app(
    *,
    onboarding: bool = False,
    installers: Any = (),
) -> tuple[MewboApp, dict[str, Any]]:
    seams: dict[str, Any] = {
        "messages": MessageRendererRegistry(),
        "sidebar_slots": SidebarSlotRegistry(),
        "permission": PermissionGateway(auto_approve=lambda: True),
        "input": InputGateway(),
    }
    made: dict[str, Any] = {}

    def factory(emit: Any, emit_renderable: Any) -> _FakeEngine:
        eng = _FakeEngine(emit, emit_renderable)
        made["engine"] = eng
        return eng

    app = MewboApp(
        header_ctx=_header_ctx(),
        messages=seams["messages"],
        sidebar_slots=seams["sidebar_slots"],
        permission=seams["permission"],
        input_gateway=seams["input"],
        engine_factory=factory,
        onboarding=onboarding,
        installers=installers,
    )
    return app, {**seams, "made": made}


# --- compose / layout ----------------------------------------------------


def test_app_composes_all_regions() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test():
            assert app.query_one(TranscriptView) is not None
            assert app.query_one(SidebarView) is not None
            assert app.query_one(InputArea) is not None
            assert app.query_one(Footer) is not None

    asyncio.run(_run())


def test_app_starts_in_landing() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test():
            assert app.screen_state == ScreenState.LANDING

    asyncio.run(_run())


def test_app_onboarding_state() -> None:
    async def _run() -> None:
        app, _ = _make_app(onboarding=True)
        async with app.run_test():
            assert app.screen_state == ScreenState.ONBOARDING

    asyncio.run(_run())


# --- submit → engine → transcript ---------------------------------------


def test_submit_runs_engine_and_enters_chat() -> None:
    async def _run() -> None:
        app, ctx = _make_app()
        async with app.run_test() as pilot:
            app.query_one(InputArea).value = "hello there"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert ctx["made"]["engine"].handled == ["hello there"]
            assert app.screen_state == ScreenState.CHAT

    asyncio.run(_run())


def test_submit_writes_to_transcript() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            app.query_one(InputArea).value = "hi"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(TranscriptView).lines  # something was written

    asyncio.run(_run())


def test_blank_submit_is_ignored() -> None:
    async def _run() -> None:
        app, ctx = _make_app()
        async with app.run_test() as pilot:
            app.query_one(InputArea).value = "   "
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert ctx["made"].get("engine") is None or ctx["made"]["engine"].handled == []

    asyncio.run(_run())


def test_exit_command_exits_app() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        exits: list[Any] = []
        async with app.run_test() as pilot:
            app.exit = lambda *a, **k: exits.append(a)  # type: ignore[method-assign]
            app.query_one(InputArea).value = "/exit"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert exits, "engine returning False should exit the app"

    asyncio.run(_run())


# --- seams ---------------------------------------------------------------


def test_seams_are_exposed() -> None:
    async def _run() -> None:
        app, ctx = _make_app()
        async with app.run_test():
            assert app.messages is ctx["messages"]
            assert app.sidebar_slots is ctx["sidebar_slots"]
            assert app.permission is ctx["permission"]
            assert app.input is ctx["input"]

    asyncio.run(_run())


def test_default_renderers_do_not_clobber_preregistered() -> None:
    async def _run() -> None:
        seams_messages = MessageRendererRegistry()
        seams_messages.register("assistant", lambda item: Text("CHILD-OWNED"))
        app = MewboApp(
            header_ctx=_header_ctx(),
            messages=seams_messages,
            sidebar_slots=SidebarSlotRegistry(),
            permission=PermissionGateway(auto_approve=lambda: True),
            input_gateway=InputGateway(),
            engine_factory=lambda e, r: _FakeEngine(e, r),
        )
        async with app.run_test():
            out = app.messages.render(TranscriptItem("assistant", {"text": "x"}))
            assert isinstance(out, Text)
            assert out.plain == "CHILD-OWNED"
            # but a foundation kind it did not register IS provided:
            assert app.messages.has("notice")

    asyncio.run(_run())


# --- installers ----------------------------------------------------------


def test_installers_run_post_mount_with_app() -> None:
    async def _run() -> None:
        seen: list[Any] = []

        def installer(app: MewboApp) -> None:
            # The App is fully mounted: widgets are queryable here.
            seen.append(app.query_one(InputArea))

        app, _ = _make_app(installers=[installer])
        async with app.run_test():
            assert len(seen) == 1
            assert isinstance(seen[0], InputArea)

    asyncio.run(_run())


def test_failing_installer_does_not_break_app() -> None:
    async def _run() -> None:
        def boom(app: MewboApp) -> None:
            raise RuntimeError("nope")

        ran: list[bool] = []

        def after(app: MewboApp) -> None:
            ran.append(True)

        app, _ = _make_app(installers=[boom, after])
        async with app.run_test():
            # App survives a failing installer and still runs later ones.
            assert ran == [True]
            assert app.query_one(InputArea) is not None

    asyncio.run(_run())


# --- turn hooks (integration ordering) -----------------------------------


def test_busy_gate_closes_before_pre_turn_checkpoint() -> None:
    """The input must be marked busy BEFORE the (blocking) pre-turn checkpoint.

    Regression for the cross-feature window where a second Enter during the git
    checkpoint could start a concurrent turn (gate vs checkpoint).
    """

    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            order: list[str] = []
            real_set_busy = app._input_widget.set_busy  # type: ignore[union-attr]

            def _rec_set_busy(value: bool) -> None:
                if value:
                    order.append("busy")
                real_set_busy(value)

            app._input_widget.set_busy = _rec_set_busy  # type: ignore[union-attr,assignment]

            class _Controller:
                def checkpoint_turn(self, text: str) -> None:
                    order.append("checkpoint")

                def autotitle_after_turn(self) -> None:
                    pass

            app._session_controller = _Controller()  # type: ignore[attr-defined]
            app.query_one(InputArea).value = "hi"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert order[:2] == ["busy", "checkpoint"]

    asyncio.run(_run())


# --- compact layout ------------------------------------------------------


def test_sidebar_hidden_when_compact() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test(size=(60, 30)):
            await app.workers.wait_for_complete()
            assert app.query_one(SidebarView).display is False

    asyncio.run(_run())


def test_sidebar_visible_when_wide() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test(size=(140, 30)):
            await app.workers.wait_for_complete()
            assert app.query_one(SidebarView).display is True

    asyncio.run(_run())
