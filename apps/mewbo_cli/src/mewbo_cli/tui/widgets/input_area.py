#!/usr/bin/env python3
"""InputArea — rich sigil-dispatched prompt input (issue #155, epic #149).

ONE input field, sigil-dispatched at the caret:

- ``@`` → project files (caret-anchored, tiered name-priority ranking via
  :class:`~mewbo_cli.tui.input.completion.CompletionEngine`, reusing the shared
  :class:`~mewbo_tools.integration.file_catalog.FileCatalog`).
- ``/`` → commands + skills + custom/MCP commands (fuzzy, match-highlighted,
  argument hints), recognised anywhere in the line.
- ``!`` → bash (recognised; no completion, the engine treats the line as shell).

A caret-anchored, dynamically-sized completion overlay (an ``OptionList``)
renders the ranked candidates above the field. ``up``/``down`` move the
selection; ``tab``/``enter`` accept; ``shift+up``/``shift+down`` insert the
highlighted candidate WITHOUT dismissing the overlay (fast browsing);
``escape`` dismisses it.

The widget subclasses :class:`~textual.widgets.Input` so the host App's
``Input.Submitted`` contract is untouched (the constructor signature
``InputArea(gateway, *, id=..., placeholder=...)`` is preserved — see #150).
It also owns three orthogonal behaviours the controller wires in via the
installer (see :mod:`mewbo_cli.tui.input.palette`):

- **Queue while busy.** :meth:`set_busy` flips the busy flag; while busy,
  ``Enter`` queues the line instead of submitting (the controller drains it via
  :meth:`drain_next` when the turn finishes). ``escape`` pulls the most-recent
  queued line back into the box. :meth:`queued_count` is read by the #156 pill.
- **History.** ``up``/``down`` on an empty/edited line walk
  :class:`~mewbo_cli.tui.input.history.PromptHistory`; ``ctrl+r`` opens a
  reverse-search overlay (binding lives here, so it is active when focused).
- **Completion.** The :class:`CompletionEngine` is injected post-mount via
  :meth:`set_completion_engine`; until then the widget degrades to the
  foundation gateway suggester (inline ghost suggestion).
"""

from __future__ import annotations

import os
from collections.abc import Callable

from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.suggester import Suggester
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from mewbo_cli.tui.input.completion import Completion, CompletionEngine
from mewbo_cli.tui.input.history import PromptHistory
from mewbo_cli.tui.seams import InputGateway

# Cap the visible overlay rows; the engine already caps candidates.
_MAX_OVERLAY_ROWS = 10


class _GatewaySuggester(Suggester):
    """Inline ghost-suggestion adapter over :class:`InputGateway` (#150 fallback).

    Used only until the richer :class:`CompletionEngine` is injected — it offers
    a single inline completion (the first gateway result that extends the value).
    """

    def __init__(self, gateway: InputGateway) -> None:
        super().__init__(use_cache=True, case_sensitive=False)
        self._gateway = gateway

    async def get_suggestion(self, value: str) -> str | None:
        for candidate in self._gateway.complete(value):
            if candidate.casefold().startswith(value):
                return candidate
        return None


class InputArea(Input):
    """Single-line sigil-dispatched prompt input with overlay completion."""

    # ctrl+r reverse history search lives on the widget's own BINDINGS so it is
    # active whenever the input is focused — no controller/app wiring needed.
    BINDINGS = [
        Binding("ctrl+r", "reverse_search", "History search", show=False),
    ]

    # Use built-in semantic roles ($surface/$panel/$accent) so the widget mounts
    # cleanly even in a bare host App with no theme registered; the injected
    # Palette (#151) overrides these roles, so the overlay stays theme-aware.
    DEFAULT_CSS = """
    InputArea { height: auto; }
    .input--completion {
        layer: overlay;
        max-height: 12;
        width: auto;
        max-width: 80%;
        background: $surface;
        border: round $panel;
        display: none;
    }
    .input--completion.visible { display: block; }
    """

    def __init__(
        self,
        gateway: InputGateway,
        *,
        id: str | None = None,  # noqa: A002
        placeholder: str = "Type a message, /command, @file, or !bash…",
    ) -> None:
        """Bind the gateway + initialise state (engine injected post-mount).

        Args:
            gateway: Foundation completion gateway (inline fallback suggester).
            id: Optional Textual DOM id.
            placeholder: Hint text shown when empty.
        """
        super().__init__(
            placeholder=placeholder,
            suggester=_GatewaySuggester(gateway),
            id=id,
        )
        self._gateway = gateway
        self._engine: CompletionEngine | None = None
        self._history: PromptHistory = PromptHistory()
        self._overlay: OptionList | None = None
        self._candidates: list[Completion] = []
        # queue + busy state
        self._busy = False
        self._queue: list[str] = []
        # history navigation cursor (None = not navigating)
        self._hist_index: int | None = None
        self._hist_stash: str = ""
        # The last value WE assigned (history/completion/queue). Input.Changed is
        # posted asynchronously, so a transient flag is unreliable; instead the
        # handler compares the changed value against this to tell our writes from
        # user typing (a user edit makes value diverge from this).
        self._programmatic_value: str | None = None
        # reverse-search state
        self._rsearch_active = False
        self._rsearch_query = ""

    # -- injection (post-mount, by the installer) -----------------------

    def set_completion_engine(self, engine: CompletionEngine) -> None:
        """Inject the rich completion engine (#155 installer)."""
        self._engine = engine

    def set_history(self, history: PromptHistory) -> None:
        """Inject a shared :class:`PromptHistory` (else a default is used)."""
        self._history = history

    # -- queue / busy (controller drains via the app turn loop) ---------

    def set_busy(self, busy: bool) -> None:
        """Mark a turn in progress; while busy, ``Enter`` queues the line."""
        self._busy = busy

    @property
    def busy(self) -> bool:
        """Whether a turn is currently in progress."""
        return self._busy

    def queued_count(self) -> int:
        """Number of queued follow-up messages (read by the #156 queue pill)."""
        return len(self._queue)

    def enqueue(self, text: str) -> None:
        """Queue ``text`` as a follow-up to run after the current turn."""
        if text.strip():
            self._queue.append(text)

    def drain_next(self) -> str | None:
        """Pop the oldest queued message (FIFO), or ``None`` if the queue is empty."""
        if self._queue:
            return self._queue.pop(0)
        return None

    # -- programmatic value writes --------------------------------------

    def _set_value(self, value: str, *, cursor: int | None = None) -> None:
        """Assign ``value`` ourselves, recording it so Input.Changed isn't seen as typing.

        Every internal edit (history nav, completion accept, reverse-search,
        queue pull/clear) goes through here. Because ``Input.Changed`` fires
        asynchronously, :meth:`_on_changed` compares against ``_programmatic_value``
        rather than a transient flag — so it keeps history navigation alive across
        our own writes while still resetting it the moment the user types (I2).
        ``cursor`` defaults to end-of-line.
        """
        self._programmatic_value = value
        self.value = value
        self.cursor_position = len(value) if cursor is None else cursor

    # -- composition ----------------------------------------------------

    def on_mount(self) -> None:
        """Mount the completion overlay as a sibling overlay widget."""
        overlay = OptionList(classes="input--completion")
        overlay.can_focus = False
        self._overlay = overlay
        # Mount into the screen overlay layer so it floats above the input.
        try:
            self.screen.mount(overlay)
        except Exception:
            self._overlay = None
            return
        # The DEFAULT_CSS `.input--completion { display: none }` is scoped to this
        # widget's own subtree; since the overlay is mounted on the SCREEN it lives
        # outside that scope and the rule never applies — leaving an empty bordered
        # box visible whenever there are no candidates. Drive `display` imperatively
        # (in lockstep with the `.visible` class) so the overlay is hidden unless it
        # actually has rows to show.
        overlay.display = False

    # -- completion overlay ---------------------------------------------

    @on(Input.Changed)
    def _on_changed(self, event: Input.Changed) -> None:
        """Recompute completions; user typing also exits history navigation (I2)."""
        if event.value != self._programmatic_value:
            # The change wasn't one of our own writes → the user typed. Invalidate
            # the history-browse cursor so the next up/down starts fresh from this
            # draft instead of jumping from the stale index.
            self._hist_index = None
        self._refresh_completions()

    def watch_selection(self, old: object, new: object) -> None:
        """Re-evaluate the overlay when the caret moves (I1).

        ``cursor_position`` is derived from the ``selection`` reactive; cursor
        moves via bindings (left/right/home/end) fire no ``Input.Changed``, so
        without this an open overlay would keep stale candidates after the caret
        leaves the token. Only acts while an overlay is showing (and not mid
        reverse-search) so it adds no work on a quiet input.
        """
        del old, new
        if self._rsearch_active or not self._overlay_visible:
            return
        self._refresh_completions()

    def _refresh_completions(self) -> None:
        if self._engine is None or self._overlay is None:
            return
        candidates = self._engine.complete(self.value, self.cursor_position)
        self._candidates = candidates
        overlay = self._overlay
        overlay.clear_options()
        if not candidates:
            overlay.remove_class("visible")
            overlay.display = False
            return
        for cand in candidates[:_MAX_OVERLAY_ROWS * 5]:
            overlay.add_option(Option(self._render_candidate(cand)))
        overlay.add_class("visible")
        overlay.display = True
        if overlay.option_count:
            overlay.highlighted = 0

    @staticmethod
    def _render_candidate(cand: Completion) -> Text:
        """One overlay row: highlighted match + dim kind/detail/arg-hint."""
        text = Text()
        display = cand.display
        match = set(cand.match_indices)
        for idx, ch in enumerate(display):
            text.append(ch, style="bold" if idx in match else "")
        if cand.argument_hint:
            text.append(f"  {cand.argument_hint}", style="italic dim")
        elif cand.detail:
            text.append(f"  {cand.detail}", style="dim")
        elif cand.kind and cand.kind != "file":
            text.append(f"  {cand.kind}", style="dim")
        return text

    @property
    def _overlay_visible(self) -> bool:
        return self._overlay is not None and self._overlay.has_class("visible")

    def _hide_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.remove_class("visible")
            self._overlay.display = False
        self._candidates = []

    def _accept_completion(self, *, keep_open: bool) -> None:
        """Splice the highlighted candidate into the line at the active token."""
        if not self._overlay_visible or self._overlay is None:
            return
        index = self._overlay.highlighted or 0
        if index >= len(self._candidates):
            return
        cand = self._candidates[index]
        token = (
            self._engine.active_token(self.value, self.cursor_position)
            if self._engine is not None
            else None
        )
        if token is None:
            return
        replacement = cand.replacement
        suffix = "" if keep_open else " "
        new_value = self.value[: token.start] + replacement + suffix + self.value[token.end :]
        new_cursor = token.start + len(replacement) + len(suffix)
        self._set_value(new_value, cursor=new_cursor)
        if keep_open:
            self._refresh_completions()
        else:
            self._hide_overlay()

    # -- key handling ---------------------------------------------------

    async def _on_key(self, event: events.Key) -> None:
        """Intercept overlay navigation, history nav, and reverse-search keys."""
        if self._rsearch_active:
            if await self._handle_rsearch_key(event):
                return
        if self._overlay_visible:
            if self._handle_overlay_key(event):
                return
        else:
            if self._handle_history_key(event):
                return
        await super()._on_key(event)

    def _handle_overlay_key(self, event: events.Key) -> bool:
        """Overlay navigation/accept; returns True if the key was consumed."""
        overlay = self._overlay
        if overlay is None:
            return False
        key = event.key
        if key == "down":
            overlay.action_cursor_down()
            event.stop()
            event.prevent_default()
            return True
        if key == "up":
            overlay.action_cursor_up()
            event.stop()
            event.prevent_default()
            return True
        if key in ("shift+down", "shift+up"):
            if key == "shift+down":
                overlay.action_cursor_down()
            else:
                overlay.action_cursor_up()
            self._accept_completion(keep_open=True)
            event.stop()
            event.prevent_default()
            return True
        if key in ("tab", "enter"):
            self._accept_completion(keep_open=False)
            event.stop()
            event.prevent_default()
            return True
        if key == "escape":
            self._hide_overlay()
            event.stop()
            event.prevent_default()
            return True
        return False

    def _handle_history_key(self, event: events.Key) -> bool:
        """History nav (up/down) + esc queue-pull; True if consumed."""
        key = event.key
        if key == "up":  # walk toward OLDER entries
            return self._history_step(back=True, event=event)
        if key == "down":  # walk back toward NEWER / the draft
            return self._history_step(back=False, event=event)
        if key == "escape" and self._queue:
            # esc pulls the most-recent queued message back into the box.
            self._set_value(self._queue.pop())
            event.stop()
            event.prevent_default()
            return True
        return False

    def _history_step(self, *, back: bool, event: events.Key) -> bool:
        """Walk history: ``recent`` is newest→oldest, so ``back`` increments.

        Index ``None`` = editing the live draft; index ``i`` = ``recent[i]``.
        Going forward past the newest entry restores the stashed draft.
        """
        recent = self._history.recent()
        if not recent:
            return False
        if self._hist_index is None:
            if not back:  # already on the draft; nothing newer to show
                return False
            self._hist_stash = self.value
            self._hist_index = 0
        else:
            self._hist_index += 1 if back else -1
        if self._hist_index < 0:
            # Stepped forward past the newest entry → restore the draft.
            self._hist_index = None
            self._set_value(self._hist_stash)
            event.stop()
            event.prevent_default()
            return True
        if self._hist_index >= len(recent):
            self._hist_index = len(recent) - 1  # clamp at the oldest
        self._set_value(recent[self._hist_index])
        event.stop()
        event.prevent_default()
        return True

    # -- reverse search (ctrl+r) ----------------------------------------

    def action_reverse_search(self) -> None:
        """Enter reverse-search mode (``ctrl+r``)."""
        self._rsearch_active = True
        self._rsearch_query = ""
        self._show_rsearch()

    def _show_rsearch(self) -> None:
        if self._overlay is None:
            return
        matches = self._history.search(self._rsearch_query, limit=_MAX_OVERLAY_ROWS)
        self._candidates = [
            Completion(display=m, replacement=m, kind="history") for m in matches
        ]
        overlay = self._overlay
        overlay.clear_options()
        header = Text(f"(reverse-i-search)`{self._rsearch_query}': ", style="dim")
        overlay.add_option(Option(header, disabled=True))
        for cand in self._candidates:
            overlay.add_option(Option(Text(cand.display)))
        overlay.add_class("visible")
        overlay.display = True
        if self._candidates:
            overlay.highlighted = 1  # skip the disabled header row

    async def _handle_rsearch_key(self, event: events.Key) -> bool:
        key = event.key
        if key == "escape":
            self._end_rsearch()
            event.stop()
            event.prevent_default()
            return True
        if key == "enter":
            self._accept_rsearch()
            event.stop()
            event.prevent_default()
            return True
        if key in ("ctrl+r", "up", "down"):
            self._step_rsearch(1 if key in ("ctrl+r", "down") else -1)
            event.stop()
            event.prevent_default()
            return True
        if key == "backspace":
            self._rsearch_query = self._rsearch_query[:-1]
            self._show_rsearch()
            event.stop()
            event.prevent_default()
            return True
        if event.character and event.is_printable:
            self._rsearch_query += event.character
            self._show_rsearch()
            event.stop()
            event.prevent_default()
            return True
        return False

    def _step_rsearch(self, direction: int) -> None:
        if self._overlay is None or not self._candidates:
            return
        cur = self._overlay.highlighted or 1
        new = max(1, min(cur + direction, len(self._candidates)))
        self._overlay.highlighted = new

    def _accept_rsearch(self) -> None:
        if self._overlay is not None and self._candidates:
            index = (self._overlay.highlighted or 1) - 1
            if 0 <= index < len(self._candidates):
                self._set_value(self._candidates[index].display)
        self._end_rsearch()

    def _end_rsearch(self) -> None:
        self._rsearch_active = False
        self._rsearch_query = ""
        self._hide_overlay()

    # -- submission -----------------------------------------------------

    @on(Input.Submitted)
    def _on_submitted(self, event: Input.Submitted) -> None:
        """Record history; while busy, queue instead of submitting.

        The host App also listens for ``Input.Submitted`` to run the turn. When
        busy we swallow the event (queue the line) so the App never starts a
        second concurrent turn; the controller drains the queue via
        :meth:`drain_next` when the turn finishes.
        """
        text = event.value
        self._hist_index = None
        if not text.strip():
            return
        self._history.append(text)
        if self._busy:
            self.enqueue(text)
            self._set_value("")
            event.stop()  # don't let the App start a turn while one is running


def default_files(cwd_provider: Callable[[], str] = os.getcwd) -> Callable[[], list[str]]:
    """A cached project-file provider for the completion engine (public helper).

    Kept here (not in the engine) so the engine stays I/O-free; the #155
    installer imports this to build the engine's ``files_provider``. Public so
    that cross-module use (``palette.py``) is an explicit, supported contract.
    """
    from mewbo_tools.integration.file_catalog import FileCatalog

    cache: dict[str, list[str]] = {}

    def _provide() -> list[str]:
        try:
            cwd = cwd_provider()
        except Exception:
            return []
        if cwd not in cache:
            try:
                cache[cwd] = FileCatalog(cwd).list_files(limit=5000)
            except Exception:
                cache[cwd] = []
        return cache[cwd]

    return _provide


__all__ = ["InputArea", "default_files"]
