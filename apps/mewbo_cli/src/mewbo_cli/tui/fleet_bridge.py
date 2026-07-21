#!/usr/bin/env python3
"""FleetBridge — refresh the faceted sidebar from live run events.

Integration glue (controller-owned, not a Wave-2 child): a per-run
:class:`~mewbo_core.hooks.HookManager` whose agent-lifecycle + tool hooks poke
the UI to re-pull the live fleet from the
:class:`~mewbo_cli.tui.agent_transcript_hub.AgentTranscriptHub`. The hub (a bus
observer) is the single source of fleet rollups AND the authoritative live todos
(the ``todos`` event); the bridge's only job here is to marshal a *refresh*
onto the UI thread (``app.call_from_thread``) whenever a run event lands, so the
:class:`~mewbo_cli.tui.widgets.fleet_panel.FleetPanel`, the plan dock and any open
drill view re-render promptly. (The panel also self-ticks while agents run, so
token/elapsed liveness never depends solely on a hook.)

The plan/todo dock is fed by the hub's authoritative ``todos`` event (an agent
calls ``update_todos``); the old tool-call heuristic (a ``TodoTracker`` that
re-projected every raw step) was retired, so these tool hooks now only
marshal a refresh — they never fabricate progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.hooks import HookManager

if TYPE_CHECKING:
    from collections.abc import Callable


class FleetBridge:
    """Refresh the faceted sidebar (fleet · plan · drill) on each run event.

    Atomic class (state + hooks + DI). ``app`` may be ``None`` (plain REPL / no
    sidebar) in which case every hook is a safe no-op. The fleet rollups live in
    the hub; this bridge only marshals a UI refresh onto the App thread.
    """

    def __init__(
        self,
        app: Any,
    ) -> None:
        """Bind the mounted app.

        ``app`` may be ``None`` (plain REPL / no sidebar) — every hook is then a
        safe no-op. The plan dock's todos come from the hub's authoritative
        ``todos`` event, not from these hooks.
        """
        self._app = app

    # -- hooks ------------------------------------------------------------

    def on_agent_start(self, handle: Any) -> None:
        """A sub-agent spawned — refresh the fleet."""
        self._push()

    def on_agent_stop(self, handle: Any) -> None:
        """A sub-agent reached a terminal state — refresh the fleet."""
        self._push()

    def pre_tool_use(self, action_step: Any) -> Any:
        """Refresh the sidebar on a tool start; pass the step through."""
        self._push()
        return action_step

    def post_tool_use(self, action_step: Any, result: Any) -> Any:
        """Refresh the sidebar on a tool result; pass the result through."""
        self._push()
        return result

    # -- internals --------------------------------------------------------

    def _push(self) -> None:
        """Marshal a sidebar refresh onto the UI thread (never breaks a run)."""
        app = self._app
        if app is None:
            return
        fleet = getattr(app, "_fleet_panel", None)
        todo_panel = getattr(app, "_todo_panel", None)
        drill = getattr(app, "_drill_controller", None)
        try:
            if fleet is not None:
                app.call_from_thread(fleet.refresh_fleet)
            if todo_panel is not None:
                app.call_from_thread(todo_panel.apply)
            if drill is not None:
                app.call_from_thread(drill.refresh)
        except Exception:  # noqa: BLE001 - a render push must never break a run
            pass

    def build_hook_manager(self) -> HookManager:
        """Build a :class:`HookManager` wired to this bridge's hooks."""
        return HookManager(
            on_agent_start=[self.on_agent_start],
            on_agent_stop=[self.on_agent_stop],
            pre_tool_use=[self.pre_tool_use],
            post_tool_use=[self.post_tool_use],
        )


def make_fleet_hook_factory(
    app_provider: Callable[[], Any],
) -> Callable[[], HookManager]:
    """Return a ``hook_factory`` that builds a fresh :class:`FleetBridge` per run.

    ``app_provider`` is resolved lazily (the App only exists post-mount); the
    bridge reads the sidebar widgets off the App (``_fleet_panel`` /
    ``_todo_panel`` / ``_drill_controller``) at refresh time. A missing app
    yields a bridge whose hooks are no-ops.
    """

    def _factory() -> HookManager:
        return FleetBridge(app_provider()).build_hook_manager()

    return _factory


__all__ = ["FleetBridge", "make_fleet_hook_factory"]
