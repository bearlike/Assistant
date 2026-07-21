#!/usr/bin/env python3
"""TranscriptView — streaming-aware scrolling transcript.

Rewritten from a ``RichLog`` placeholder to a ``VerticalScroll`` container that
can mount child widgets (``DiffView``, ``Collapsible``) for rich tool output
while preserving the stable ``write_item`` / ``write_renderable`` public API
that :class:`~mewbo_cli.tui.app.MewboApp` calls.

Streaming support is via three methods:

- :meth:`begin_stream(stream_id, *, agent_label)` — open a live streaming slot.
- :meth:`append_stream(stream_id, delta)` — feed a text delta; the stable-prefix
  cache (:class:`~mewbo_cli.tui.transcript_render.StreamingMarkdown`) renders
  only the trailing partial on each call (the flicker fix).
- :meth:`end_stream(stream_id)` — finalise the slot, emit a single static item.

Tool calls that carry ``old_text`` + ``new_text`` (Edit/Write) get a
:class:`~mewbo_cli.cli_diffview.DiffView` mounted as a child widget in addition
to the static diffstat summary line.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from mewbo_cli.cli_diffview import DiffView
from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.seams import MessageRendererRegistry, TranscriptItem
from mewbo_cli.tui.transcript_render import (
    StreamingMarkdown,
    diff_envelope_for,
    is_shell_tool,
    register_transcript_renderers,
)


class TranscriptView(VerticalScroll):
    """Streaming-aware transcript container wired to a :class:`MessageRendererRegistry`.

    Replaces the ``RichLog`` placeholder from the Rich-based CLI.  Consuming code still
    calls :meth:`write_item` / :meth:`write_renderable` for non-streaming content;
    streaming assistant turns use :meth:`begin_stream` / :meth:`append_stream`
    / :meth:`end_stream`.

    ``DiffView`` child widgets are mounted automatically when a ``tool``
    :class:`~mewbo_cli.tui.seams.TranscriptItem` carries both ``old_text`` and
    ``new_text`` (Edit/Write tool calls).
    """

    # Expose a ``lines`` property so legacy tests that check ``len(tv.lines)``
    # continue to pass. We track lines written via the internal counter.
    #
    # Design language: vertical rhythm + per-kind accents driven
    # entirely by CSS classes (``t-<kind>``) tagged onto each mounted child.
    # Colours come from injected theme vars ($panel/$accent/$text-muted) — never
    # a hardcoded hex.  One blank line of margin separates every block; new user
    # turns gain extra top margin (turn boundary); tool calls indent under the
    # assistant inside a left-rail accent; sub-agent items indent deeper.
    DEFAULT_CSS = """
    TranscriptView {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    TranscriptView > Static, TranscriptView > DiffView {
        margin: 0 0 1 0;
        width: 1fr;
    }
    TranscriptView > .t-user {
        margin: 1 0 1 0;
        padding: 0 0 0 1;
        border-left: thick $primary;
    }
    TranscriptView > .t-tool {
        margin: 0 0 1 2;
        padding: 0 0 0 1;
        border-left: solid $panel;
    }
    TranscriptView > .t-bash {
        border-left: thick $success;
    }
    TranscriptView > .t-diff {
        margin: 0 0 1 2;
    }
    TranscriptView > .t-plan, TranscriptView > .t-plan_proposal {
        padding: 0 1;
        border-left: solid $accent;
    }
    TranscriptView > .t-notice {
        color: $text-muted;
    }
    TranscriptView > .t-agent {
        margin-left: 4;
    }
    /* A settled unit of work (completed/failed tool, finished turn) recedes:
       lower text-opacity so attention flows to what is still active. */
    TranscriptView > .t-settled {
        text-opacity: 65%;
    }
    """

    def __init__(
        self,
        registry: MessageRendererRegistry,
        *,
        palette: Palette = DEFAULT_PALETTE,
        id: str | None = None,  # noqa: A002
    ) -> None:
        """Initialise the transcript container.

        Args:
            registry: Routes :class:`~mewbo_cli.tui.seams.TranscriptItem`
                objects to kind-specific renderers.  Renderers registered via
                :func:`~mewbo_cli.tui.transcript_render.register_transcript_renderers`
                will be installed on this registry when this widget mounts.
            palette:  Injected semantic colour palette.
            id:       Optional Textual DOM id.
        """
        super().__init__(id=id)
        self._registry = registry
        self._palette = palette

        # Streaming state: stream_id → StreamingMarkdown + Static widget id.
        self._streams: dict[str, _StreamSlot] = {}

        # Mutable tool cards: card_id → Static widget, updated in place as
        # a tool settles (running → done/error). ``_tool_diffs`` guards a one-time
        # DiffView mount per card on settle.
        self._tool_cards: dict[str, Static] = {}
        self._tool_diffs: set[str] = set()

        # Track total lines written for legacy compatibility (tv.lines).
        self._line_count: int = 0

        # Live "working" activity indicator (the spinner shown while a turn/step
        # is running — the only feedback during a blocking run).
        self._activity: Static | None = None
        self._activity_timer: object | None = None
        self._activity_label: str = "Working"
        self._activity_start: float = 0.0
        self._activity_frame: int = 0
        # Optional live-label source: the throughput meter's rendered
        # phase/rate/stall, pulled every 0.1s tick so the label stays live even
        # when no new event arrives (a hung agent). When set it OWNS the label
        # (phase-scoped timers included); the spinner is still prepended.
        self._activity_label_provider: Callable[[], str] | None = None

    # ------------------------------------------------------------------
    # Textual lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """Install transcript renderers if not already registered.

        Uses the public ``has``-guard pattern so this registration is idempotent
        with both the controller's pre-mount call to ``register_transcript_renderers``
        and ``MewboApp.on_mount``'s own foundation renderers — whichever runs
        first wins for each kind, none clobber the others.
        """
        # Route through a staging registry to avoid touching seams._renderers.
        # Each kind is transferred to the shared registry only if absent.
        _staging: MessageRendererRegistry = MessageRendererRegistry()
        register_transcript_renderers(_staging, palette=self._palette)
        for kind in ("user", "assistant", "tool", "error"):
            if not self._registry.has(kind):
                # Forward via the staging registry's public render() path.
                # Capture kind + staging ref so the closure is not late-binding.
                def _make_forwarder(
                    s: MessageRendererRegistry, k: str
                ) -> Callable[[TranscriptItem], RenderableType]:
                    def _forward(item: TranscriptItem) -> RenderableType:
                        # Re-route the item through the staging registry by kind.
                        return s.render(TranscriptItem(k, item.payload))
                    return _forward

                self._registry.register(kind, _make_forwarder(_staging, kind))

    # ------------------------------------------------------------------
    # Public API (stable — app.py calls these)
    # ------------------------------------------------------------------

    @property
    def lines(self) -> list[str]:
        """Pseudo-line list for legacy compatibility (``len(tv.lines)``).

        Returns a list of empty strings whose length equals the number of
        content items written since mount.  The actual text lives in mounted
        ``Static`` children — this property exists solely so tests that check
        ``len(tv.lines) >= 1`` continue to pass without a ``RichLog``.
        """
        return [""] * self._line_count

    def write_item(self, item: TranscriptItem) -> None:
        """Render ``item`` via the registry and append to the transcript.

        For ``tool`` items carrying both ``old_text`` and ``new_text`` a
        :class:`~mewbo_cli.cli_diffview.DiffView` is mounted as a child widget
        after the static summary line.

        Args:
            item: Transcript entry keyed by ``kind``; rendered via the matching
                renderer or the generic fallback.
        """
        try:
            renderable = self._registry.render(item)
            self._mount_static(renderable, classes=kind_classes(item))

            # Mount a colored DiffView for diff-bearing tool calls: Edit/Write
            # carry old_text+new_text; file_edit_tool returns a unified-diff
            # ``kind: diff`` envelope — both reuse the ONE DiffView widget.
            if item.kind == "tool":
                diff_classes = "t-diff"
                if item.payload.get("agent_label"):
                    diff_classes = f"{diff_classes} t-agent"

                old_text = item.payload.get("old_text")
                new_text = item.payload.get("new_text")
                envelope = diff_envelope_for(item.payload)
                if old_text is not None and new_text is not None:
                    self._mount_diffview(
                        str(old_text), str(new_text), item.payload, classes=diff_classes
                    )
                elif envelope is not None:
                    diff_text, file_path = envelope
                    self._mount_diffview_from_unified(
                        diff_text, file_path, classes=diff_classes
                    )
        except Exception as exc:  # noqa: BLE001
            self._mount_static(
                Text(f"[transcript error: {exc}]", style=f"{self._palette.error}"),
                classes="t-error",
            )

    def write_renderable(self, renderable: RenderableType) -> None:
        """Append a raw Rich renderable to the transcript.

        Args:
            renderable: Any Rich renderable (e.g. :class:`rich.text.Text`,
                :class:`rich.panel.Panel`, markdown).
        """
        self._mount_static(renderable)

    # ------------------------------------------------------------------
    # Activity indicator (the "working" spinner)
    # ------------------------------------------------------------------

    def start_activity(self, label: str = "Working") -> None:
        """Show an animated ``<spinner> <label>… (Ns)`` activity line.

        Mounted at the foot of the transcript while a turn/step runs (the only
        feedback during a blocking run). Idempotent — a second call just updates
        the label. Never raises into the UI thread.
        """
        try:
            self._activity_label = label
            if self._activity is not None:
                self._render_activity()
                return
            self._activity_start = time.monotonic()
            self._activity_frame = 0
            widget = Static("", classes="t-activity")
            self.mount(widget)
            self._activity = widget
            self._activity_timer = self.set_interval(0.1, self._tick_activity)
            self._render_activity()
            self.scroll_end(animate=False)
        except Exception:  # noqa: BLE001 - the spinner must never break a turn
            self._activity = None

    def set_activity_label(self, label: str) -> None:
        """Update the activity label (e.g. ``Running shell``, ``Calling an MCP tool``)."""
        self._activity_label = label
        if self._activity is not None:
            self._render_activity()

    def set_activity_label_provider(self, provider: Callable[[], str] | None) -> None:
        """Install a live label source (the throughput meter) or clear it.

        When set, the provider is pulled on every 0.1s tick and OWNS the label
        text — phase-scoped timers, tok/s and stall included — so a hung agent
        visibly flips to ``stalled`` without a new event. Cleared with ``None``.
        """
        self._activity_label_provider = provider

    def stop_activity(self) -> None:
        """Remove the activity indicator and stop its animation timer."""
        timer = self._activity_timer
        self._activity_timer = None
        if timer is not None:
            try:
                timer.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if self._activity is not None:
            try:
                self._activity.remove()
            except Exception:  # noqa: BLE001
                pass
            self._activity = None

    def finish_activity(self, elapsed: float | None = None, outcome: str | None = None) -> None:
        """Collapse the live spinner into a SETTLED turn-summary line.

        Stops the animation and, instead of erasing the spinner, rewrites it as a
        muted turn-summary line so the finished turn recedes (lower weight) while
        the next turn's spinner mounts fresh below it. ``elapsed is None`` falls
        back to :meth:`stop_activity` (no summary).

        ``outcome`` is the turn's honest session-derived status (e.g.
        ``TurnEngine.last_turn_outcome()``) — ``None`` or ``"completed"`` (a
        command/skill dispatch with no query turn, or a clean finish) keeps the
        historical green ``✓ done · {N}s`` line; anything else renders its OWN
        glyph/tone from :data:`ICONS`, so a blocked or unmet-goal turn never
        reads as success.
        """
        timer = self._activity_timer
        self._activity_timer = None
        if timer is not None:
            try:
                timer.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if self._activity is None:
            return
        if elapsed is None:
            self.stop_activity()
            return
        line = Text()
        if outcome and outcome not in {"completed", "running"}:
            # Fall back to a neutral "?" / muted tone (mirroring the fleet
            # panel's own fallback) for a status this vocabulary doesn't name
            # yet — e.g. ``awaiting_approval`` — rather than mischaracterizing
            # an unmapped, possibly-benign outcome as a hard failure.
            glyph = ICONS.agent_state.get(outcome, "?")
            style_role = ICONS.agent_state_style.get(outcome, "muted")
            line.append(f"{glyph} ", style=getattr(self._palette, style_role))
            label = outcome.replace("_", " ")
            line.append(f"{label} · {elapsed:.1f}s", style=f"{self._palette.muted}")
        else:
            line.append(f"{ICONS.check} ", style=f"{self._palette.success}")
            line.append(f"done · {elapsed:.1f}s", style=f"{self._palette.muted}")
        try:
            self._activity.update(line)
            self._activity.add_class("t-settled")
        except Exception:  # noqa: BLE001
            pass
        # Release the handle WITHOUT removing the widget — it stays as the turn's
        # settled footer; the next turn's start_activity mounts a new spinner.
        self._activity = None

    def _tick_activity(self) -> None:
        self._activity_frame += 1
        self._render_activity()

    def _render_activity(self) -> None:
        if self._activity is None:
            return
        frame = ICONS.spinner[self._activity_frame % len(ICONS.spinner)]
        line = Text()
        line.append(f"{frame} ", style=f"bold {self._palette.accent}")
        provider = self._activity_label_provider
        if provider is not None:
            # The meter owns the whole label (phase + rate + its own timers).
            try:
                label = provider()
            except Exception:  # noqa: BLE001 — a provider miss falls back to elapsed
                label = None
            if label:
                line.append(label, style=f"{self._palette.accent}")
                self._activity.update(line)
                return
        elapsed = int(time.monotonic() - self._activity_start)
        line.append(f"{self._activity_label}… ", style=f"{self._palette.accent}")
        line.append(f"({elapsed}s)", style=f"{self._palette.muted}")
        self._activity.update(line)

    # ------------------------------------------------------------------
    # Streaming API
    # ------------------------------------------------------------------

    def begin_stream(self, stream_id: str, *, agent_label: str | None = None) -> None:
        """Open a live streaming slot for assistant text.

        If ``stream_id`` is already open this is a no-op (idempotent).

        Args:
            stream_id:   Opaque identifier for this stream (e.g. turn UUID).
            agent_label: Optional sub-agent label shown as a dim prefix.
        """
        if stream_id in self._streams:
            return

        label_text = f"  [{agent_label}]" if agent_label else ""
        # Mount a placeholder Static widget we will update in-place. The live
        # streaming slot carries the ``t-assistant`` class (and ``t-agent`` when
        # it is a sub-agent turn) so it shares the transcript design language.
        slot_classes = "t-assistant t-agent" if agent_label else "t-assistant"
        slot_widget = Static(
            "", id=f"stream-{stream_id[:16]}", markup=False, classes=slot_classes
        )
        self._append_widget(slot_widget)
        self._line_count += 1

        self._streams[stream_id] = _StreamSlot(
            widget=slot_widget,
            cache=StreamingMarkdown(),
            agent_label=label_text,
        )

    def append_stream(self, stream_id: str, delta: str) -> None:
        """Append a text delta to an open streaming slot.

        The stable-prefix cache (:class:`StreamingMarkdown`) is consulted on
        every delta call.  ``render()`` returns:

        - ``stable`` — the memoised render of the text up to the last block
          boundary (blank line / fence close).  This is a **cache hit** on
          most deltas, so no Rich re-render of the already-rendered text occurs
          (the flicker fix).
        - ``partial`` — a fresh render of the trailing incomplete block only.

        Both parts are composed into a ``Group`` and handed to ``Static.update``
        so the live widget displays accurate, fully rendered markdown on every
        delta rather than raw text.

        If ``stream_id`` is not open this is a no-op.

        Args:
            stream_id: The id passed to :meth:`begin_stream`.
            delta:     New text to append.
        """
        slot = self._streams.get(stream_id)
        if slot is None:
            return

        slot.cache.feed(delta)
        # Use the rendered parts — do NOT discard them and fall back to raw text.
        # stable is memoised (cache hit on most deltas); partial is always fresh.
        stable, partial = slot.cache.render(width=self.size.width or 80)

        parts: list[RenderableType] = []
        if slot.agent_label:
            parts.append(Text(slot.agent_label, style="dim italic"))
        parts.append(stable)
        if partial is not None:
            parts.append(partial)

        slot.widget.update(Group(*parts) if parts else Text(""))

    def end_stream(self, stream_id: str) -> None:
        """Finalise a streaming slot.

        Replaces the live preview widget with a static transcript item so the
        rendered markdown is committed and the stream slot is released.

        Args:
            stream_id: The id passed to :meth:`begin_stream`.
        """
        slot = self._streams.pop(stream_id, None)
        if slot is None:
            return

        # Build the final assistant item from accumulated text.
        final_text = slot.cache.text
        payload: dict[str, object] = {"text": final_text}
        if slot.agent_label:
            payload["agent_label"] = slot.agent_label.strip("[] ")

        final_item = TranscriptItem("assistant", payload)
        try:
            renderable = self._registry.render(final_item)
        except Exception:  # noqa: BLE001
            renderable = Text(final_text)

        # Update in-place (the widget is already mounted).
        slot.widget.update(renderable)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _append_widget(self, widget: Widget) -> None:
        """Mount ``widget`` at the foot — but ABOVE the live activity spinner.

        During a live turn content streams in while the activity spinner
        is mounted at the foot; mounting plainly would push new content *below*
        the spinner. Anchoring before ``self._activity`` keeps the spinner the
        last child so the transcript reads top-to-bottom in emission order.
        """
        if self._activity is not None:
            self.mount(widget, before=self._activity)
        else:
            self.mount(widget)

    def _mount_static(self, renderable: RenderableType, *, classes: str = "") -> None:
        """Mount a ``Static`` widget containing ``renderable`` with CSS ``classes``."""
        widget = Static(renderable, classes=classes or None)
        self._append_widget(widget)
        self._line_count += 1
        self.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Mutable tool card
    # ------------------------------------------------------------------

    def upsert_tool(self, card_id: str, item: TranscriptItem) -> None:
        """Mount-or-update a tool card keyed by ``card_id`` (the mutable card).

        On first call the card mounts as ``running`` (accent glyph, full weight);
        a later call with the same ``card_id`` updates it IN PLACE to the settled
        ``done``/``error`` state (muted weight via ``t-settled``) without breaking
        surrounding order. A settled Edit/Write card mounts a colored ``DiffView``
        once (mirroring :meth:`write_item`).
        """
        try:
            renderable = self._registry.render(item)
        except Exception as exc:  # noqa: BLE001
            renderable = Text(f"[tool render error: {exc}]", style=f"{self._palette.error}")
        settled = str(item.payload.get("status")) in ("done", "error")
        classes = kind_classes(item)
        if settled:
            classes = f"{classes} t-settled"

        widget = self._tool_cards.get(card_id)
        if widget is None:
            widget = Static(renderable, classes=classes or None)
            self._tool_cards[card_id] = widget
            self._append_widget(widget)
            self._line_count += 1
        else:
            widget.update(renderable)
            widget.set_classes(classes.split())

        if settled and card_id not in self._tool_diffs:
            old_text = item.payload.get("old_text")
            new_text = item.payload.get("new_text")
            envelope = diff_envelope_for(item.payload)
            diff_classes = "t-diff t-settled"
            if old_text is not None and new_text is not None:
                self._tool_diffs.add(card_id)
                self._mount_diffview(str(old_text), str(new_text), item.payload,
                                     classes=diff_classes)
            elif envelope is not None:
                self._tool_diffs.add(card_id)
                diff_text, file_path = envelope
                self._mount_diffview_from_unified(diff_text, file_path, classes=diff_classes)
        self.scroll_end(animate=False)

    def _mount_diffview(
        self, old_text: str, new_text: str, payload, *, classes: str = ""  # noqa: ANN001
    ) -> None:
        """Mount a :class:`~mewbo_cli.cli_diffview.DiffView` for an edit diff."""
        file_path = payload.get("file_path") if payload else None
        widget = DiffView(
            old_text,
            new_text,
            palette=self._palette,
            file_path=file_path,
            classes=classes or None,
        )
        self._append_widget(widget)
        self._line_count += 1
        self.scroll_end(animate=False)

    def _mount_diffview_from_unified(
        self, diff_text: str, file_path: str | None, *, classes: str = ""
    ) -> None:
        """Mount a :class:`DiffView` from a unified-diff string (``kind: diff``)."""
        widget = DiffView.from_unified_diff(
            diff_text,
            palette=self._palette,
            file_path=file_path,
            classes=classes or None,
        )
        self._append_widget(widget)
        self._line_count += 1
        self.scroll_end(animate=False)


def kind_classes(item: TranscriptItem) -> str:
    """Return the space-joined CSS classes for a mounted transcript child.

    Always includes ``t-<kind>`` (kind sanitised to a CSS-safe token).  Sub-agent
    items — those whose payload carries ``agent_label`` or a positive ``depth`` —
    additionally get ``t-agent`` so they indent deeper under their parent.
    """
    kind = str(item.kind).replace(" ", "_")
    classes = [f"t-{kind}"]
    payload = item.payload
    # Shell executions get a distinct rail so they read as a terminal block.
    if item.kind == "tool" and is_shell_tool(str(payload.get("tool_id", ""))):
        classes.append("t-bash")
    depth = payload.get("depth")
    is_sub_agent = bool(payload.get("agent_label")) or (
        isinstance(depth, int) and depth > 0
    )
    if is_sub_agent:
        classes.append("t-agent")
    return " ".join(classes)


class _StreamSlot:
    """Internal holder for an open streaming slot."""

    __slots__ = ("widget", "cache", "agent_label")

    def __init__(
        self,
        *,
        widget: Static,
        cache: StreamingMarkdown,
        agent_label: str,
    ) -> None:
        self.widget = widget
        self.cache = cache
        self.agent_label = agent_label


__all__ = ["TranscriptView", "kind_classes"]
