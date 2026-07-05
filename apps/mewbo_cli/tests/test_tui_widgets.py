#!/usr/bin/env python3
"""Tests for the placeholder TUI widgets (issue #150, epic #149).

Each widget is mounted in a minimal host App and driven via Textual's
``app.run_test()`` / Pilot API (pattern mirrored from test_tui_header.py:
``asyncio.run()`` wrapping the async body inside a sync test function).
"""

from __future__ import annotations

import asyncio

from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    SidebarSlotRegistry,
    TranscriptItem,
)
from mewbo_cli.tui.widgets.input_area import InputArea
from mewbo_cli.tui.widgets.sidebar import SidebarView
from mewbo_cli.tui.widgets.transcript import TranscriptView
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

# ---------------------------------------------------------------------------
# Minimal host apps
# ---------------------------------------------------------------------------


class _TranscriptApp(App):
    """Minimal host app for TranscriptView."""

    def __init__(self, registry: MessageRendererRegistry) -> None:
        super().__init__()
        # Use a non-conflicting name: App._registry is Textual's internal WeakSet.
        self._msg_registry = registry

    def compose(self) -> ComposeResult:
        yield TranscriptView(self._msg_registry, id="tv")


class _SidebarApp(App):
    """Minimal host app for SidebarView."""

    def __init__(self, registry: SidebarSlotRegistry) -> None:
        super().__init__()
        # Use a non-conflicting name: App._registry is Textual's internal WeakSet.
        self._slot_registry = registry

    def compose(self) -> ComposeResult:
        yield SidebarView(self._slot_registry, id="sb")


class _InputApp(App):
    """Minimal host app for InputArea."""

    def __init__(self, gateway: InputGateway) -> None:
        super().__init__()
        self._input_gateway = gateway

    def compose(self) -> ComposeResult:
        yield InputArea(self._input_gateway, id="ia")


# ---------------------------------------------------------------------------
# TranscriptView tests
# ---------------------------------------------------------------------------


def test_transcript_write_item_routes_via_registry() -> None:
    """write_item renders via the registered kind renderer without error."""
    registry = MessageRendererRegistry()
    registry.register("user", lambda item: Text(f"U:{item.payload.get('text', '')}"))

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one("#tv", TranscriptView)
            tv.write_item(TranscriptItem("user", {"text": "hi"}))
            await pilot.pause()
            assert len(tv.lines) >= 1

    asyncio.run(_run())


def test_transcript_write_item_unknown_kind_uses_fallback() -> None:
    """write_item falls back to the generic renderer for unknown kinds."""
    registry = MessageRendererRegistry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one("#tv", TranscriptView)
            tv.write_item(TranscriptItem("unknown-kind", {"text": "fallback-text"}))
            await pilot.pause()
            assert len(tv.lines) >= 1

    asyncio.run(_run())


def test_transcript_write_renderable() -> None:
    """write_renderable accepts a raw Rich renderable and appends it."""
    registry = MessageRendererRegistry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test() as pilot:
            tv = app.query_one("#tv", TranscriptView)
            tv.write_renderable(Text("raw-output"))
            await pilot.pause()
            assert len(tv.lines) >= 1

    asyncio.run(_run())


def test_transcript_id_forwarded() -> None:
    """The id keyword argument is forwarded to RichLog.__init__."""
    registry = MessageRendererRegistry()

    async def _run() -> None:
        app = _TranscriptApp(registry)
        async with app.run_test():
            tv = app.query_one("#tv", TranscriptView)
            assert tv.id == "tv"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# SidebarView tests
# ---------------------------------------------------------------------------


def test_sidebar_slots_appear_in_order() -> None:
    """Children from slot factories appear sorted by (order, name)."""
    registry = SidebarSlotRegistry()
    registry.register("b-slot", lambda: Static("B", id="slot-b"), order=20)
    registry.register("a-slot", lambda: Static("A", id="slot-a"), order=10)
    registry.register("c-slot", lambda: Static("C", id="slot-c"), order=20)

    async def _run() -> None:
        app = _SidebarApp(registry)
        async with app.run_test() as pilot:
            await pilot.pause()
            sb = app.query_one("#sb", SidebarView)
            children = list(sb.children)
            # Expected order: a-slot (10), b-slot (20 alpha < c), c-slot (20)
            assert len(children) == 3
            assert children[0].id == "slot-a"
            assert children[1].id == "slot-b"
            assert children[2].id == "slot-c"

    asyncio.run(_run())


def test_sidebar_empty_shows_placeholder() -> None:
    """When no slots are registered, a sidebar-empty placeholder is rendered."""
    registry = SidebarSlotRegistry()  # empty

    async def _run() -> None:
        app = _SidebarApp(registry)
        async with app.run_test() as pilot:
            await pilot.pause()
            sb = app.query_one("#sb", SidebarView)
            children = list(sb.children)
            assert len(children) == 1
            assert "sidebar-empty" in children[0].classes

    asyncio.run(_run())


def test_sidebar_refresh_slots_remounts_children() -> None:
    """refresh_slots() replaces children when the registry changes."""
    registry = SidebarSlotRegistry()

    async def _run() -> None:
        app = _SidebarApp(registry)
        async with app.run_test() as pilot:
            await pilot.pause()
            sb = app.query_one("#sb", SidebarView)
            # Initially empty → placeholder
            assert len(list(sb.children)) == 1
            assert "sidebar-empty" in list(sb.children)[0].classes

            # Register a slot and refresh
            registry.register("new-slot", lambda: Static("new", id="slot-new"))
            await sb.refresh_slots()
            await pilot.pause()

            children = list(sb.children)
            assert len(children) == 1
            assert children[0].id == "slot-new"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# InputArea tests
# ---------------------------------------------------------------------------


def test_input_area_mounts_without_error() -> None:
    """InputArea mounts successfully with a basic gateway."""
    gateway = InputGateway()

    async def _run() -> None:
        app = _InputApp(gateway)
        async with app.run_test():
            ia = app.query_one("#ia", InputArea)
            assert ia.id == "ia"

    asyncio.run(_run())


def test_input_area_suggester_returns_matching_completion() -> None:
    """Suggester adapts gateway.complete() and returns a prefix-matching suggestion."""
    gateway = InputGateway()
    gateway.set_completion_provider(lambda text: ["/help", "/history"])

    async def _run() -> None:
        app = _InputApp(gateway)
        async with app.run_test():
            ia = app.query_one("#ia", InputArea)
            suggester = ia.suggester
            assert suggester is not None
            # Textual casefolds the value before get_suggestion; pass as-is here.
            suggestion = await suggester.get_suggestion("/he")
            assert suggestion is not None
            assert suggestion.casefold().startswith("/he")

    asyncio.run(_run())


def test_input_area_suggester_no_match_returns_none() -> None:
    """Suggester returns None when no completion starts with the current value."""
    gateway = InputGateway()
    gateway.set_completion_provider(lambda text: ["/help"])

    async def _run() -> None:
        app = _InputApp(gateway)
        async with app.run_test():
            ia = app.query_one("#ia", InputArea)
            suggester = ia.suggester
            assert suggester is not None
            suggestion = await suggester.get_suggestion("/xyz-no-match")
            assert suggestion is None

    asyncio.run(_run())


def test_input_area_gateway_complete_called() -> None:
    """gateway.complete() is consulted when the suggester is invoked."""
    calls: list[str] = []

    def provider(text: str) -> list[str]:
        calls.append(text)
        return ["/help"]

    gateway = InputGateway()
    gateway.set_completion_provider(provider)

    async def _run() -> None:
        app = _InputApp(gateway)
        async with app.run_test():
            ia = app.query_one("#ia", InputArea)
            suggester = ia.suggester
            assert suggester is not None
            await suggester.get_suggestion("/he")
            assert len(calls) >= 1

    asyncio.run(_run())


def test_input_area_placeholder_default() -> None:
    """InputArea uses the expected default placeholder text."""
    gateway = InputGateway()

    async def _run() -> None:
        app = _InputApp(gateway)
        async with app.run_test():
            ia = app.query_one("#ia", InputArea)
            ph = ia.placeholder.lower()
            assert "message" in ph or "command" in ph

    asyncio.run(_run())
