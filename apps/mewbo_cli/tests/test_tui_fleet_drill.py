#!/usr/bin/env python3
"""Tests for the in-place fleet drill-in (#161, epic #149).

The acceptance bar: selecting a fleet agent swaps the MAIN transcript region (in
place — not a separate screen) for that agent's transcript from the hub, with a
breadcrumb + per-agent footer; ``esc`` returns; navigation to the root AND any
child is first-class (child→child and child→root both work).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub
from mewbo_cli.tui.app import MewboApp
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
)
from mewbo_cli.tui.widgets.fleet_drill import FleetDrillController, FleetDrillView
from mewbo_cli.tui.widgets.header import HeaderContext

ROOT = "agent-root"
CHILD = "agent-child"


def _header_ctx() -> HeaderContext:
    return HeaderContext(
        title="Mewbo", version="0.0", status_label="ready", status_color="green",
        model="openai/m", session_id="sess1234", base_url="http://x",
        langfuse_enabled=False, langfuse_reason=None,
        builtin_enabled=1, builtin_disabled=0, external_enabled=0, external_disabled=0,
    )


def _make_app() -> MewboApp:
    return MewboApp(
        header_ctx=_header_ctx(),
        messages=MessageRendererRegistry(),
        sidebar_slots=SidebarSlotRegistry(),
        permission=PermissionGateway(auto_approve=lambda: False),
        input_gateway=InputGateway(),
        engine_factory=lambda emit, emit_r: SimpleNamespace(handle=lambda text: True),
    )


def _populated_hub() -> AgentTranscriptHub:
    """A hub with a root (text + tool) and one child sub-agent (researcher)."""
    hub = AgentTranscriptHub()
    hub.set_active_session("s")
    hub.observe("s", {"type": "llm_call_start", "payload": {"agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "agent_message_delta",
                      "payload": {"agent_id": ROOT, "depth": 0, "step": 0, "text": "root says hi"}})
    hub.observe("s", {"type": "agent_message",
                      "payload": {"agent_id": ROOT, "depth": 0, "text": "root says hi"}})
    step = SimpleNamespace(tool_id="bash", tool_input={"command": "ls"}, operation="execute")
    hub.tool_started(step)
    hub.observe("s", {"type": "tool_result",
                      "payload": {"tool_id": "bash", "tool_input": {"command": "ls"},
                                  "result": "file.txt", "success": True,
                                  "agent_id": ROOT, "depth": 0}})
    hub.observe("s", {"type": "sub_agent",
                      "payload": {"action": "start", "agent_id": CHILD, "parent_id": ROOT,
                                  "depth": 1, "status": "running", "agent_type": "researcher"}})
    hub.observe("s", {"type": "agent_message_delta",
                      "payload": {"agent_id": CHILD, "depth": 1, "step": 0,
                                  "text": "child digging"}})
    return hub


def _texts(view: FleetDrillView) -> str:
    """Join the Text-renderable children (breadcrumb/footer/tool cards).

    Assistant turns render as opaque markdown visuals (not str-readable), so
    presence of the assistant turn is asserted structurally via :func:`_classes`.
    """
    return "\n".join(str(c.render()) for c in view.children)


def _classes(view: FleetDrillView) -> set[str]:
    out: set[str] = set()
    for child in view.children:
        out |= set(child.classes)
    return out


def test_open_swaps_transcript_for_root_in_place() -> None:
    """Drilling the root hides the live transcript and shows its history in place."""
    hub = _populated_hub()

    async def go() -> None:
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            ctrl = FleetDrillController(app=app, hub=hub, registry=app.messages)
            ctrl.open(ROOT)
            await pilot.pause()
            await pilot.pause()
            # In-place swap: the live transcript is hidden, the drill view shown.
            assert app.query_one("#transcript").display is False
            view = app.query_one("#fleet-drill", FleetDrillView)
            assert view.display is True
            assert ctrl.is_open
            text = _texts(view)
            assert "Fleet ▸ root" in text  # breadcrumb
            assert "bash" in text  # the tool card (drill-in DOES show tools)
            # The root's assistant turn is rendered in place (markdown visual).
            assert "t-assistant" in _classes(view)

    asyncio.run(go())


def test_footer_shows_per_agent_timeline() -> None:
    """The drill footer carries status · model · tools · tokens for the agent."""
    hub = AgentTranscriptHub()
    hub.set_active_session("s")
    hub.observe("s", {"type": "llm_call_start",
                      "payload": {"agent_id": ROOT, "depth": 0, "model": "openai/gpt-oss-120b"}})
    hub.observe("s", {"type": "llm_call_end",
                      "payload": {"agent_id": ROOT, "depth": 0,
                                  "cumulative_input_tokens": 1200,
                                  "cumulative_output_tokens": 300}})

    async def go() -> None:
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            ctrl = FleetDrillController(app=app, hub=hub, registry=app.messages)
            ctrl.open(ROOT)
            await pilot.pause()
            await pilot.pause()
            text = _texts(app.query_one("#fleet-drill", FleetDrillView))
            assert "gpt-oss-120b" in text  # model
            assert "in" in text and "out" in text  # token timeline
            assert "0 tools" in text

    asyncio.run(go())


def test_navigation_root_to_child_and_back_to_root() -> None:
    """Re-selecting re-targets the drill: root → child → root all work in place."""
    hub = _populated_hub()

    async def go() -> None:
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            ctrl = FleetDrillController(app=app, hub=hub, registry=app.messages)
            # root → child
            ctrl.open(ROOT)
            await pilot.pause()
            ctrl.open(CHILD)
            await pilot.pause()
            await pilot.pause()
            view = app.query_one("#fleet-drill", FleetDrillView)
            assert view.current_agent_id == CHILD
            text = _texts(view)
            assert "root / researcher" in text  # breadcrumb path to the child
            assert "t-assistant" in _classes(view)  # the child's turn shows
            # child → root (back to the root, still in place)
            ctrl.open(ROOT)
            await pilot.pause()
            await pilot.pause()
            assert view.current_agent_id == ROOT
            assert "Fleet ▸ root" in _texts(view)
            assert "bash" in _texts(view)  # the root's tool card is back

    asyncio.run(go())


def test_close_restores_live_transcript() -> None:
    """esc / close hides the drill view and restores the live transcript."""
    hub = _populated_hub()

    async def go() -> None:
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            ctrl = FleetDrillController(app=app, hub=hub, registry=app.messages)
            ctrl.open(ROOT)
            await pilot.pause()
            view = app.query_one("#fleet-drill", FleetDrillView)
            # The view's own escape action drives the close callback.
            view.action_close()
            await pilot.pause()
            assert ctrl.is_open is False
            assert app.query_one("#transcript").display is True
            assert view.display is False

    asyncio.run(go())
