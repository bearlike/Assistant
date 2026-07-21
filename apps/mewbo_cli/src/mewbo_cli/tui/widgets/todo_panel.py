#!/usr/bin/env python3
"""TodoPanel — the pinned Plan/Todo dock (sidebar facet).

Extracted from the old combined ``AgentPanel`` so the sidebar splits into clear
sections (Fleet · Plan · Context). This widget owns ONLY the plan surface: an
``N/M`` count header, a compact tri-state checklist (``✓`` done · ``→``
in-progress · ``•`` pending) when the producer supplies per-item state, and a
queue pill (``▸ k queued``) for messages queued while a turn runs.

It is fed by injected providers (a :class:`TodoState` source — the hub's
authoritative ``todos`` event via ``AgentTranscriptHub.root_todos`` — and a
queued-count source) so it is fully testable without a run, and driven from any
thread via :meth:`apply` (the controller marshals it through
``app.call_from_thread``). No plan ⇒ no items ⇒ the dock renders nothing (todos
are never fabricated).

One atomic widget. Colors come from theme CSS vars (``$success`` / ``$accent`` /
``$warning`` / ``$text-muted``) via Textual content markup; glyphs from
:data:`ICONS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from textual.reactive import reactive
from textual.widgets import Static

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.tui.widgets._markup import escape_markup as _escape

# The three todo lifecycle states (the documented tri-state contract). Exactly
# one item is ever ``in_progress`` at a time (enforced by the producer).
_TODO_DONE = "done"
_TODO_ACTIVE = "in_progress"
_TODO_PENDING = "pending"

# Checklist glyphs: the in-progress arrow + pending bullet are panel-local; the
# completed tick reuses ICONS.check so it tracks the shared theme.
_TODO_ACTIVE_MARKER = "→"
_TODO_BULLET = "•"

# Per-todo-state (glyph, theme-color): ✓ completed is de-emphasised success, →
# in-progress is warning (the eye lands on it), • pending is muted.
_TODO_GLYPH: dict[str, tuple[str, str]] = {
    _TODO_DONE: (ICONS.check, "$text-muted"),
    _TODO_ACTIVE: (_TODO_ACTIVE_MARKER, "$warning"),
    _TODO_PENDING: (_TODO_BULLET, "$text-muted"),
}


@dataclass(frozen=True)
class TodoItem:
    """One checklist row: a short label plus its tri-state lifecycle state."""

    label: str
    state: str = _TODO_PENDING


@dataclass
class TodoState:
    """Todo dock state: an ``N/M`` summary plus an optional per-item checklist."""

    total: int = 0
    done: int = 0
    current: str | None = None
    items: list[TodoItem] = field(default_factory=list)

    @property
    def has_items(self) -> bool:
        """Whether any todo items exist."""
        return self.total > 0 or bool(self.items)

    @classmethod
    def from_items(cls, items: list[TodoItem]) -> TodoState:
        """Build a state whose ``N/M`` summary is derived from ``items``.

        Keeps the pill count and the checklist in lockstep — ``done`` counts
        completed rows, ``current`` is the single in-progress label.
        """
        done = sum(1 for it in items if it.state == _TODO_DONE)
        current = next((it.label for it in items if it.state == _TODO_ACTIVE), None)
        return cls(total=len(items), done=done, current=current, items=list(items))


class TodoPanel(Static):
    """Sidebar dock rendering the tri-state plan checklist + queue pill."""

    DEFAULT_CSS = """
    TodoPanel {
        height: auto;
    }
    """

    #: Whether the dock is expanded (shows the current task even with no items).
    pills_expanded: reactive[bool] = reactive(False)

    def __init__(
        self,
        *,
        todo_provider: Callable[[], TodoState] | None = None,
        queue_count_provider: Callable[[], int] | None = None,
        id: str | None = None,  # noqa: A002 - Textual DOM id
    ) -> None:
        """Bind the optional todo/queue providers and forward ``id`` to Static."""
        super().__init__("", id=id)
        self._todo_provider = todo_provider
        self._queue_count_provider = queue_count_provider
        self._todo = TodoState()
        self._queued = 0

    def on_mount(self) -> None:
        """Render the initial dock once mounted."""
        self.apply()

    def apply(self) -> None:
        """Re-pull the providers and re-render the dock (thread-safe entry)."""
        self._todo = self._todo_provider() if self._todo_provider else TodoState()
        queued = self._queue_count_provider() if self._queue_count_provider else 0
        self._queued = max(0, int(queued))
        self._refresh_content()

    def toggle_pills(self) -> None:
        """Flip the dock between collapsed summary and expanded detail."""
        self.pills_expanded = not self.pills_expanded

    def watch_pills_expanded(self, _old: bool, _new: bool) -> None:
        """Re-render when the expansion toggles."""
        if self.is_mounted:
            self._refresh_content()

    # -- rendering --------------------------------------------------------

    def _refresh_content(self) -> None:
        lines = self._dock_lines()
        self.update("\n".join(lines) if lines else "[$text-muted]no plan[/]")

    def _dock_lines(self) -> list[str]:
        todo, queued = self._todo, self._queued
        out: list[str] = []

        if todo.has_items:
            label = f"{ICONS.check} {todo.done}/{todo.total} todo"
            line = f"[$success]{label}[/]"
            if todo.current:
                line += f" [$text-muted]· {_clip(todo.current, 28)}[/]"
            out.append(line)
            out.extend(self._item_lines(todo.items))

        if queued > 0:
            out.append(f"[$accent]▸[/] [$text-muted]{queued} queued[/]")

        if self.pills_expanded and todo.current and not todo.items:
            out.append(f"  [$text-muted]→ {_clip(todo.current, 32)}[/]")
        return out

    def _item_lines(self, items: list[TodoItem]) -> list[str]:
        lines: list[str] = []
        for item in items:
            glyph, color = _TODO_GLYPH.get(item.state, (_TODO_BULLET, "$text-muted"))
            lines.append(f"  [{color}]{glyph} {_clip(item.label, 30)}[/]")
        return lines


def _clip(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` chars with an ellipsis (markup-escaped)."""
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return _escape(text)


__all__ = ["TodoItem", "TodoPanel", "TodoState"]
