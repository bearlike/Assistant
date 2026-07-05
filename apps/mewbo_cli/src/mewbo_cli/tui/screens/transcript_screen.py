#!/usr/bin/env python3
"""Full-transcript modal view (``ctrl+o``) for the active session (#157).

The inline transcript shows the conversational surface; this modal exposes the
*full* record for the active session — tool inputs + outputs, assistant /
thinking text, and the per-message model — loaded from the session store via
``SessionRuntime.load_events``. A stored event whose result carries a unified
diff is rendered with the ONE reusable :class:`~mewbo_cli.cli_diffview.DiffView`
rather than raw text, so the approval modal and this view share one diff
renderer.

It is read-only: ``escape``/``q``/``ctrl+o`` dismisses. Colours come from the
active theme (CSS vars + the injected ``Palette`` for the DiffView).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mewbo_core.types import EventRecord
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from mewbo_cli.cli_diffview import DiffView
from mewbo_cli.cli_theme import Palette, auto_palette

EventLoader = Callable[[], list[EventRecord]]
"""Returns the active session's events (``runtime.load_events(session_id)``)."""

#: Event kinds rendered as plain text blocks (kind → display label).
_TEXT_KINDS: dict[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "agent_message": "assistant",
    "plan_proposed": "plan",
}

_MAX_RESULT_CHARS = 4000


def _payload(event: EventRecord) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _diff_text(result: Any) -> str | None:
    """Return a unified-diff string if ``result`` is a diff payload, else ``None``."""
    if isinstance(result, dict) and str(result.get("kind", "")).strip().lower() == "diff":
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


class TranscriptScreen(ModalScreen):
    """Read-only full-transcript modal for the active session."""

    CSS = """
    TranscriptScreen { align: center middle; }
    #transcript-dialog {
        width: 95%;
        height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #transcript-title { text-style: bold; }
    #transcript-hint { color: $text-muted; }
    #transcript-body { height: 1fr; }
    .ts-empty { color: $text-muted; padding: 1 0; }
    .ts-meta { color: $text-muted; }
    .ts-tool { color: $secondary; }
    """

    BINDINGS = [
        Binding("escape,q,ctrl+o", "cancel", "Close"),
    ]

    def __init__(
        self,
        loader: EventLoader,
        *,
        palette: Palette | None = None,
    ) -> None:
        """Bind the event loader and the diff palette (auto when omitted)."""
        super().__init__()
        self._loader = loader
        self._palette = palette or auto_palette()

    def compose(self) -> ComposeResult:
        """Lay out the title/hint and a scrollable body inside the centered box."""
        try:
            events = list(self._loader())
        except Exception:
            events = []
        with Vertical(id="transcript-dialog"):
            yield Label("Transcript", id="transcript-title")
            yield Label("Esc / Ctrl+O to close", id="transcript-hint")
            with VerticalScroll(id="transcript-body"):
                if not events:
                    yield Label("Transcript is empty.", classes="ts-empty")
                else:
                    yield from self._render_events(events)

    def _render_events(self, events: list[EventRecord]):
        """Yield one widget per renderable event (text / tool / diff)."""
        for event in events:
            etype = str(event.get("type") or "")
            payload = _payload(event)
            if etype in _TEXT_KINDS:
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                model = str(payload.get("model") or "")
                head = _TEXT_KINDS[etype]
                label = f"{head} · {model}" if model else head
                yield Label(label, classes="ts-meta")
                yield Static(Text(text))
            elif etype == "tool_result":
                yield from self._render_tool(payload)

    def _render_tool(self, payload: dict[str, Any]):
        """Yield the header + body widgets for one ``tool_result`` event."""
        tool_id = str(payload.get("tool_id") or "tool")
        operation = str(payload.get("operation") or "")
        model = str(payload.get("model") or "")
        ok = payload.get("success", True)
        mark = "✓" if ok else "✗"
        label = f"{mark} {tool_id}"
        if operation:
            label = f"{label}:{operation}"
        if model:
            label = f"{label}  ·  {model}"
        yield Label(label, classes="ts-tool")
        result = payload.get("result")
        # Prefer the ONE reusable DiffView when the event carries both sides
        # (old/new text); fall back to tinting a pre-computed unified diff,
        # which DiffView cannot reconstruct two sides from.
        if isinstance(result, dict):
            old = result.get("old_text") or result.get("old")
            new = result.get("new_text") or result.get("new")
            if isinstance(old, str) and isinstance(new, str):
                path = result.get("path")
                yield DiffView(
                    old,
                    new,
                    palette=self._palette,
                    file_path=str(path) if isinstance(path, str) else None,
                )
                return
        diff = _diff_text(result)
        if diff is not None:
            yield self._diff_static(diff)
            return
        preview = self._preview(result)
        if preview:
            yield Static(Text(preview, style="dim"))

    @staticmethod
    def _preview(result: Any) -> str:
        if result is None:
            return ""
        text = result if isinstance(result, str) else repr(result)
        text = text.strip()
        if len(text) > _MAX_RESULT_CHARS:
            text = text[:_MAX_RESULT_CHARS] + "…"
        return text

    def _diff_static(self, diff: str) -> Static:
        """Render a raw unified-diff string with palette-driven add/del tinting.

        ``DiffView`` reconstructs split/unified from old/new text; a stored event
        only carries the already-computed unified diff, so we render the hunk
        text directly with the same add/del semantics rather than
        reverse-engineering two sides. Colours come from the injected
        :class:`Palette` (``diff_add``/``diff_del``/``diff_eq``) — same source
        DiffView uses, never hardcoded hex/names. Keeps the ONE DiffView for the
        approval path (old/new in hand) while honouring the diff payload here.
        """
        add = self._palette.diff_add
        delete = self._palette.diff_del
        eq = self._palette.diff_eq
        body = Text()
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                body.append(line + "\n", style=add)
            elif line.startswith("-") and not line.startswith("---"):
                body.append(line + "\n", style=delete)
            elif line.startswith("@@"):
                body.append(line + "\n", style=self._palette.accent)
            else:
                body.append(line + "\n", style=eq)
        return Static(body)

    def action_cancel(self) -> None:
        """Dismiss the read-only transcript view."""
        self.dismiss(None)


__all__ = ["EventLoader", "TranscriptScreen"]
