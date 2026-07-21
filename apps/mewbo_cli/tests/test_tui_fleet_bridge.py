#!/usr/bin/env python3
"""Tests for FleetBridge — run-event → faceted-sidebar refresh glue.

The old tool-call ``TodoTracker`` heuristic was retired (the plan dock
now renders the hub's authoritative ``todos`` event), so the bridge's tool hooks
are pure sidebar refreshes.
"""

from __future__ import annotations

from typing import Any

from mewbo_cli.tui.fleet_bridge import FleetBridge, make_fleet_hook_factory


class _FakePanel:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh_fleet(self) -> None:
        self.refreshes += 1


class _FakeTodoPanel:
    def __init__(self) -> None:
        self.applies = 0

    def apply(self) -> None:
        self.applies += 1


class _FakeDrill:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1


class _FakeApp:
    """Records call_from_thread invocations and runs them synchronously."""

    def __init__(self, *, with_widgets: bool = True) -> None:
        self.calls = 0
        if with_widgets:
            self._fleet_panel = _FakePanel()
            self._todo_panel = _FakeTodoPanel()
            self._drill_controller = _FakeDrill()

    def call_from_thread(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return fn(*args, **kwargs)


def test_agent_start_refreshes_the_fleet() -> None:
    app = _FakeApp()
    bridge = FleetBridge(app)
    bridge.on_agent_start(object())
    assert app._fleet_panel.refreshes == 1
    assert app._todo_panel.applies == 1
    assert app._drill_controller.refreshes == 1


def test_agent_stop_refreshes_the_fleet() -> None:
    app = _FakeApp()
    bridge = FleetBridge(app)
    bridge.on_agent_stop(object())
    assert app._fleet_panel.refreshes == 1


def test_tool_hooks_refresh_and_pass_through() -> None:
    app = _FakeApp()
    bridge = FleetBridge(app)
    step = object()
    assert bridge.pre_tool_use(step) is step  # passes the step through
    result = object()
    assert bridge.post_tool_use(step, result) is result
    # Two events → two fleet refreshes.
    assert app._fleet_panel.refreshes == 2


def test_no_app_is_noop() -> None:
    # Plain REPL / no sidebar: hooks must not raise.
    bridge = FleetBridge(None)
    bridge.on_agent_start(object())
    assert bridge.pre_tool_use(1) == 1
    assert bridge.post_tool_use(1, 2) == 2


def test_missing_widgets_is_noop() -> None:
    """An App without the sidebar widgets (compact / early) degrades cleanly."""
    app = _FakeApp(with_widgets=False)
    bridge = FleetBridge(app)
    bridge.on_agent_start(object())  # must not raise
    assert bridge.pre_tool_use(7) == 7


def test_hook_manager_registers_all_four() -> None:
    hm = FleetBridge(_FakeApp()).build_hook_manager()
    assert len(hm.on_agent_start) == 1
    assert len(hm.on_agent_stop) == 1
    assert len(hm.pre_tool_use) == 1
    assert len(hm.post_tool_use) == 1


def test_factory_builds_bridge_from_app_provider() -> None:
    app = _FakeApp()
    factory = make_fleet_hook_factory(lambda: app)
    hm = factory()
    hm.on_agent_start[0](object())
    assert app._fleet_panel.refreshes == 1


def test_factory_with_no_app_yields_safe_noop() -> None:
    factory = make_fleet_hook_factory(lambda: None)
    hm = factory()
    hm.on_agent_start[0](object())  # must not raise
    assert hm.pre_tool_use[0](42) == 42
