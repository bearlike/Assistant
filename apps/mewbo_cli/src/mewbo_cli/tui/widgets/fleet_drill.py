#!/usr/bin/env python3
"""FleetDrillView + FleetDrillController — in-place fleet drill-in (#161).

Selecting a fleet row swaps the MAIN transcript region (in place — NOT a
separate fullscreen screen) for the chosen agent's transcript, read from the
:class:`~mewbo_cli.tui.agent_transcript_hub.AgentTranscriptHub`: live if the
agent is still running, full history once it's done. A breadcrumb shows the path
(``root / researcher``); ``esc`` / backspace returns to the live root transcript.

Navigation to the root AND any child/sub-agent is first-class: the fleet panel
stays visible as the persistent navigator, so selecting another row re-targets
the drill view (child→child and child→root both just work), and ``esc`` exits.

A per-agent footer carries the timeline of the run — tokens in/out, tool-call
count, model, elapsed and status — so the drill-in answers "how long did this
take, and what did it cost".

The transcript items are rendered through the SAME
:class:`~mewbo_cli.tui.seams.MessageRendererRegistry` the live root uses, and
settled tool cards reuse Phase 1's ``.t-settled`` dimming — no duplicated
rendering.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub, FleetRow
from mewbo_cli.tui.seams import MessageRendererRegistry, TranscriptItem
from mewbo_cli.tui.widgets.fleet_panel import fmt_tokens  # the shared token formatter
from mewbo_cli.tui.widgets.transcript import kind_classes  # the shared class logic


class FleetDrillView(VerticalScroll):
    """In-place panel rendering one agent's transcript + breadcrumb + footer."""

    DEFAULT_CSS = """
    FleetDrillView {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    FleetDrillView > Static { margin: 0 0 1 0; width: 1fr; }
    FleetDrillView > .drill-breadcrumb {
        margin: 0 0 1 0;
        padding: 0 0 0 1;
        border-left: thick $accent;
    }
    FleetDrillView > .drill-footer {
        margin: 1 0 0 0;
        padding: 0 0 0 1;
        border-left: solid $panel;
        color: $text-muted;
    }
    FleetDrillView > .t-tool { margin: 0 0 1 2; padding: 0 0 0 1; border-left: solid $panel; }
    FleetDrillView > .t-bash { border-left: thick $success; }
    FleetDrillView > .t-settled { text-opacity: 65%; }
    """

    BINDINGS = [
        Binding("escape", "close", "Back to chat"),
        Binding("backspace", "close", "Back to chat", show=False),
    ]

    def __init__(
        self,
        *,
        registry: MessageRendererRegistry,
        hub: AgentTranscriptHub,
        on_close: Callable[[], None],
        palette: Palette = DEFAULT_PALETTE,
        id: str | None = None,  # noqa: A002 - Textual DOM id
    ) -> None:
        """Bind the renderer registry, the hub, the close callback and palette."""
        super().__init__(id=id)
        self._registry = registry
        self._hub = hub
        self._on_close = on_close
        self._palette = palette
        self._current: str | None = None

    @property
    def current_agent_id(self) -> str | None:
        """The agent currently drilled into (``None`` when nothing is shown)."""
        return self._current

    def action_close(self) -> None:
        """``esc`` / backspace — hand control back to the live transcript."""
        self._on_close()

    def render_agent(self, agent_id: str) -> None:
        """(Re)render ``agent_id``'s transcript, breadcrumb and footer in place."""
        self._current = agent_id
        # An EXCLUSIVE worker serialises remounts: a rapid re-target (root→child)
        # cancels the prior remount so removal+mount never interleave into a
        # half-stale view. Falls back to a direct call outside a running App.
        try:
            self.run_worker(
                self._remount(agent_id), group="drill-remount", exclusive=True
            )
        except Exception:  # noqa: BLE001 - no worker context → render synchronously
            self._build_into_self(agent_id)

    async def _remount(self, agent_id: str) -> None:
        """Clear then mount the rebuilt content (awaited, so order is exact)."""
        await self.remove_children()
        widgets = self._build_widgets(agent_id)
        await self.mount(*widgets)
        self.scroll_home(animate=False)

    def _build_into_self(self, agent_id: str) -> None:
        self.remove_children()
        self.mount(*self._build_widgets(agent_id))

    def _build_widgets(self, agent_id: str) -> list[Static]:
        rows = {r.agent_id: r for r in self._hub.fleet_rows()}
        row = rows.get(agent_id)
        items = self._hub.items_for(agent_id)
        widgets: list[Static] = [
            Static(self._breadcrumb(agent_id, rows), classes="drill-breadcrumb")
        ]
        for item in items:
            widgets.append(Static(self._render_item(item), classes=_item_classes(item)))
        if not items:
            widgets.append(Static(Text("(no activity yet)", style=self._palette.muted)))
        widgets.append(Static(self._footer(row), classes="drill-footer"))
        return widgets

    # -- rendering --------------------------------------------------------

    def _render_item(self, item: TranscriptItem) -> RenderableType:
        try:
            return self._registry.render(item)
        except Exception as exc:  # noqa: BLE001
            return Text(f"[render error: {exc}]", style=self._palette.error)

    def _breadcrumb(self, agent_id: str, rows: dict[str, FleetRow]) -> Text:
        """``Fleet ▸ root / researcher`` — the path to the drilled agent."""
        path: list[str] = []
        seen: set[str] = set()
        cur: str | None = agent_id
        while cur is not None and cur in rows and cur not in seen:
            seen.add(cur)
            path.append(rows[cur].label)
            cur = rows[cur].parent_id
        path.reverse()
        crumb = " / ".join(path) if path else agent_id[:8]
        text = Text()
        text.append("Fleet ▸ ", style=self._palette.muted)
        text.append(crumb, style=f"bold {self._palette.accent}")
        text.append("    esc to return", style=self._palette.muted)
        return text

    def _footer(self, row: FleetRow | None) -> Text:
        """The per-agent timeline footer: status · model · tools · elapsed · tokens."""
        if row is None:
            return Text("—", style=self._palette.muted)
        glyph = ICONS.agent_state.get(row.status, "?")
        text = Text()
        text.append(f"{glyph} {row.status}", style=self._palette.muted)
        facets: list[str] = []
        if row.model:
            facets.append(row.model.rsplit("/", 1)[-1])
        facets.append(f"{row.tool_count} tools")
        elapsed = _elapsed(row)
        if elapsed:
            facets.append(elapsed)
        facets.append(f"{fmt_tokens(row.input_tokens)} in → {fmt_tokens(row.output_tokens)} out")
        text.append("   " + " · ".join(facets), style=self._palette.muted)
        return text


class FleetDrillController:
    """Drive the in-place drill swap between the live transcript and a fleet agent.

    Atomic class (state + DI). Mounted lazily into ``#body`` before ``#sidebar``;
    :meth:`open` hides the live ``#transcript`` and shows the drill view for an
    agent, :meth:`close` restores the live transcript. The fleet panel stays
    visible throughout, so re-selecting any row re-targets the drill (root↔child
    navigation) without leaving the in-place view.
    """

    def __init__(
        self,
        *,
        app: object,
        hub: AgentTranscriptHub,
        registry: MessageRendererRegistry,
        palette: Palette = DEFAULT_PALETTE,
    ) -> None:
        """Bind the mounted App, the hub, the renderer registry and palette."""
        self._app = app
        self._hub = hub
        self._registry = registry
        self._palette = palette
        self._view: FleetDrillView | None = None

    @property
    def is_open(self) -> bool:
        """Whether the drill view is currently shown."""
        view = self._view
        return view is not None and bool(getattr(view, "display", False))

    def open(self, agent_id: str) -> None:
        """Show ``agent_id``'s transcript in place of the live transcript."""
        try:
            view = self._ensure_view()
            transcript = self._app.query_one("#transcript")  # type: ignore[attr-defined]
            transcript.display = False
            view.display = True
            view.render_agent(agent_id)
            view.focus()
        except Exception:  # noqa: BLE001 - a drill failure must never break the App
            pass

    def close(self) -> None:
        """Hide the drill view and restore the live transcript."""
        try:
            if self._view is not None:
                self._view.display = False
            transcript = self._app.query_one("#transcript")  # type: ignore[attr-defined]
            transcript.display = True
        except Exception:  # noqa: BLE001
            pass

    def refresh(self) -> None:
        """Re-render the open drill view (liveness for a still-running agent)."""
        view = self._view
        if view is not None and self.is_open and view.current_agent_id:
            try:
                view.render_agent(view.current_agent_id)
            except Exception:  # noqa: BLE001
                pass

    def _ensure_view(self) -> FleetDrillView:
        if self._view is not None:
            return self._view
        view = FleetDrillView(
            registry=self._registry,
            hub=self._hub,
            on_close=self.close,
            palette=self._palette,
            id="fleet-drill",
        )
        view.display = False
        body = self._app.query_one("#body")  # type: ignore[attr-defined]
        try:
            sidebar = self._app.query_one("#sidebar")  # type: ignore[attr-defined]
            body.mount(view, before=sidebar)
        except Exception:  # noqa: BLE001 - no sidebar (compact) → append to body
            body.mount(view)
        self._view = view
        return view


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _item_classes(item: TranscriptItem) -> str:
    """Kind classes (reused from the transcript) plus ``t-settled`` for done tools."""
    classes = kind_classes(item)
    if item.kind == "tool" and str(item.payload.get("status")) in ("done", "error"):
        classes = f"{classes} t-settled"
    return classes


def _elapsed(row: FleetRow) -> str:
    if not row.started_at or row.stopped_at is None:
        return ""
    seconds = max(0.0, row.stopped_at - row.started_at)
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60}s"


__all__ = ["FleetDrillController", "FleetDrillView"]
