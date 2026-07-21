#!/usr/bin/env python3
"""FleetPanel — the selectable hypervisor fleet.

Replaces the read-only fleet tree (the old ``AgentPanel``, which dumped each
sub-agent's tool-call *names*). Every agent in the hypervisor tree — root plus
every sub-agent / parallel agent — is a SELECTABLE row whose content is a
compact one-glance summary, never a tool-name dump::

    <glyph> <label/type> · <model> · <N tools> · <elapsed> · <in→out tokens>

The state glyph is ``●`` running · ``✓`` completed · ``✗`` failed · ``⚠`` blocked ·
``◎`` goal not met (the documented state map in :data:`ICONS` — the hypervisor's
6-state lifecycle for a sub-agent row, plus the two honest session-outcome states
a ROOT row's completion can resolve to). All of it is read off the
:class:`~mewbo_cli.tui.agent_transcript_hub.AgentTranscriptHub` rollups via the
injected ``rows_provider`` — nothing is recomputed here except *elapsed* (the one
time-relative facet, frozen at ``stopped_at``).

Selecting a row hands its ``agent_id`` to the injected ``on_select`` callback,
which drives the in-place transcript drill-in
(:mod:`mewbo_cli.tui.widgets.fleet_drill`). The panel itself is a thin
``OptionList`` so keyboard navigation, highlight and selection are Textual
built-ins — no bespoke cursor logic.

One atomic widget. Colors come from the injected :class:`Palette`; glyphs from
:data:`ICONS`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from rich.text import Text
from textual.timer import Timer
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.agent_transcript_hub import FleetRow
from mewbo_cli.tui.status.throughput_meter import Phase, ThroughputMeter

# Refresh cadence while agents run — keeps elapsed / token facets live without
# burning a tick on an idle fleet (demand-driven: paused when nothing runs).
_REFRESH_INTERVAL = 0.5


class FleetPanel(OptionList):
    """Selectable fleet list rendering one summary row per hypervisor agent."""

    DEFAULT_CSS = """
    FleetPanel {
        height: auto;
        max-height: 18;
        border: none;
        padding: 0;
        background: transparent;
    }
    FleetPanel:focus {
        border: none;
    }
    """

    def __init__(
        self,
        *,
        rows_provider: Callable[[], list[FleetRow]],
        on_select: Callable[[str], None] | None = None,
        palette: Palette = DEFAULT_PALETTE,
        clock: Callable[[], float] = time.monotonic,
        id: str | None = None,  # noqa: A002 - Textual DOM id
    ) -> None:
        """Bind the hub ``rows_provider``, the selection callback and palette."""
        super().__init__(id=id)
        self._rows_provider = rows_provider
        self._on_select = on_select
        self._palette = palette
        self._clock = clock
        self._refresh_timer: Timer | None = None
        self._had_running = False

    # -- lifecycle --------------------------------------------------------

    def on_mount(self) -> None:
        """Render the initial fleet and arm the (paused) refresh timer."""
        self._refresh_timer = self.set_interval(
            _REFRESH_INTERVAL, self._on_tick, pause=True
        )
        self.refresh_fleet()

    # -- thread-safe update ----------------------------------------------

    def refresh_fleet(self) -> None:
        """Rebuild the option rows from the live hub rollups (UI thread).

        Preserves the highlighted agent across the rebuild so a selection isn't
        lost as the fleet grows. Never raises — a render push must not break a
        run (the controller marshals this via ``call_from_thread``).
        """
        try:
            rows = list(self._rows_provider())
        except Exception:  # noqa: BLE001 - a provider error degrades to empty
            rows = []
        keep = self._highlighted_agent_id()
        self.clear_options()
        if not rows:
            self.add_option(Option(Text("idle", style=self._palette.muted), disabled=True))
            self._gate_timer(rows)
            return
        for row, prefix in _tree_order(rows):
            self.add_option(Option(self._row_text(row, prefix), id=row.agent_id))
        if keep is not None:
            self._highlight_agent_id(keep)
        self._maybe_bell(rows)
        self._gate_timer(rows)

    # -- selection --------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Hand the chosen agent id to the injected drill-in callback."""
        agent_id = event.option.id
        if agent_id and self._on_select is not None:
            self._on_select(agent_id)

    @property
    def selected_agent_id(self) -> str | None:
        """The currently-highlighted agent id (``None`` when nothing is)."""
        return self._highlighted_agent_id()

    # -- internals --------------------------------------------------------

    def _highlighted_agent_id(self) -> str | None:
        idx = self.highlighted
        if idx is None:
            return None
        try:
            return self.get_option_at_index(idx).id
        except Exception:  # noqa: BLE001
            return None

    def _highlight_agent_id(self, agent_id: str) -> None:
        try:
            self.highlighted = self.get_option_index(agent_id)
        except Exception:  # noqa: BLE001 - id no longer present after a rebuild
            pass

    def _on_tick(self) -> None:
        """Re-pull rows so elapsed / token facets stay live (never raises)."""
        try:
            self.refresh_fleet()
        except Exception:  # noqa: BLE001 - a refresh tick must never crash the App
            pass

    def _gate_timer(self, rows: list[FleetRow]) -> None:
        """Run the refresh timer only while an agent is running (no idle burn)."""
        timer = self._refresh_timer
        if timer is None:
            return
        if any(r.status == "running" for r in rows):
            timer.resume()
        else:
            timer.pause()

    def _maybe_bell(self, rows: list[FleetRow]) -> None:
        """Ring the terminal bell once when a running fleet fully drains."""
        running = any(r.status == "running" for r in rows)
        if running:
            self._had_running = True
            return
        if self._had_running and rows:
            self._had_running = False
            try:
                self.app.bell()
            except Exception:  # noqa: BLE001
                pass

    def _row_text(self, row: FleetRow, prefix: str) -> Text:
        """Compose one fleet summary row (NO tool-name dump — counts only)."""
        glyph = ICONS.agent_state.get(row.status, "?")
        glyph_style = getattr(self._palette, ICONS.agent_state_style.get(row.status, "muted"))
        label_style = f"bold {self._palette.accent}" if row.is_root else "bold"

        text = Text()
        text.append(f"{glyph} ", style=glyph_style)
        if prefix:
            text.append(prefix, style=self._palette.muted)
        text.append(row.label, style=label_style)

        facets: list[str] = []
        if row.model:
            facets.append(row.model.rsplit("/", 1)[-1])
        facets.append(f"{row.tool_count} tools")
        elapsed = self._elapsed(row)
        if elapsed:
            facets.append(elapsed)
        if row.tokens > 0:
            facets.append(f"{fmt_tokens(row.input_tokens)}→{fmt_tokens(row.output_tokens)}")
        if facets:
            text.append("  " + " · ".join(facets), style=self._palette.muted)
        # Live throughput facet — chiefly a STALL (the sub-agent-hang case);
        # rendered in the warning color so a hung agent draws the eye.
        if row.throughput is not None:
            facet = ThroughputMeter.format_facet(row.throughput)
            if facet:
                style = (
                    self._palette.warning
                    if row.throughput.phase is Phase.STALLED
                    else self._palette.muted
                )
                text.append(f"  {facet}", style=style)
        return text

    def _elapsed(self, row: FleetRow) -> str:
        """Elapsed since the agent started (frozen at ``stopped_at``)."""
        if not row.started_at:
            return ""
        end = row.stopped_at if row.stopped_at is not None else self._clock()
        return _fmt_elapsed(max(0.0, end - row.started_at))


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _tree_order(rows: list[FleetRow]) -> list[tuple[FleetRow, str]]:
    """Order rows depth-first with ``├─``/``└─`` connector prefixes.

    Roots are agents whose parent is absent from the visible set (covers the
    depth-0 root and any orphan). The result is flat (the OptionList is flat)
    but reads as a tree via the prefix string.
    """
    children: dict[str | None, list[FleetRow]] = {}
    for row in rows:
        children.setdefault(row.parent_id, []).append(row)
    ids = {r.agent_id for r in rows}
    roots = [r for r in rows if r.parent_id is None or r.parent_id not in ids]

    out: list[tuple[FleetRow, str]] = []

    def walk(nodes: list[FleetRow], prefix: str, *, top: bool) -> None:
        for i, node in enumerate(nodes):
            is_last = i == len(nodes) - 1
            connector = "" if top else ("└─ " if is_last else "├─ ")
            out.append((node, prefix + connector))
            kids = children.get(node.agent_id, [])
            if kids:
                walk(kids, prefix + ("" if top else ("   " if is_last else "│  ")), top=False)

    walk(roots, "", top=True)
    return out


def _fmt_elapsed(seconds: float) -> str:
    """Format seconds as ``42s`` or ``3m2s``."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}m{secs}s"


def fmt_tokens(count: int) -> str:
    """Format a token count compactly: ``842`` / ``12.3K`` / ``1.4M``.

    Shared by the fleet panel and the drill-in footer (imported there).
    """
    count = max(0, count)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


__all__ = ["FleetPanel", "fmt_tokens"]
