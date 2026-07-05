#!/usr/bin/env python3
"""``/resume`` session switcher modal (#157).

A ``ModalScreen`` over the sessions in the shared ``SessionRuntime``: each row
shows busy state, title and last-activity so the user can pick one to resume.
Resuming retains the model + worktree because the CLI is in-process — switching
``state.session_id`` re-points the existing runtime at the chosen session; the
model override and cwd live on ``CliState``/process and are untouched.

The modal returns the chosen ``session_id`` via ``dismiss`` (or ``None`` on
cancel); the caller (the ``/resume`` command / ``ctrl+s`` action) applies it to
``CliState`` and refreshes the header. Sessions are read via
``SessionRuntime.list_sessions``/``summarize_session`` — the single read surface
— so the switcher never re-implements session enumeration.
"""

from __future__ import annotations

from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

SessionLister = Callable[[], list[dict[str, object]]]
"""Returns session summary dicts (``SessionRuntime.list_sessions``)."""


def _format_row(summary: dict[str, object]) -> str:
    """Render one session summary as a single switcher row.

    ``● running`` / ``◌ idle`` busy marker · title · last-activity timestamp.
    Pure formatting so it is unit-testable without mounting the screen.
    """
    running = bool(summary.get("running"))
    busy = "●" if running else "◌"
    status = str(summary.get("status") or ("running" if running else "idle"))
    title = str(summary.get("title") or "")
    created = str(summary.get("created_at") or "")
    when = created.replace("T", " ")[:19] if created else "—"
    return f"{busy} {status:<14} {title}  ·  {when}"


class ResumeScreen(ModalScreen):
    """Modal session switcher → the chosen ``session_id`` (or ``None``)."""

    CSS = """
    ResumeScreen { align: center middle; }
    #resume-dialog {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #resume-title { text-style: bold; }
    #resume-hint { color: $text-muted; }
    #resume-empty { color: $text-muted; padding: 1 0; }
    OptionList { height: auto; max-height: 20; }
    """

    BINDINGS = [
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "accept", "Resume", show=False),
    ]

    def __init__(
        self,
        lister: SessionLister,
        *,
        current_session_id: str | None = None,
    ) -> None:
        """Bind the session lister and the currently-active session id."""
        super().__init__()
        self._lister = lister
        self._current = current_session_id
        self._session_ids: list[str] = []

    def compose(self) -> ComposeResult:
        """Lay out the title, hint and the session option list (or an empty note)."""
        try:
            summaries = list(self._lister())
        except Exception:
            summaries = []
        with Vertical(id="resume-dialog"):
            yield Label("Resume session", id="resume-title")
            yield Label("↑/↓ to choose · Enter to resume · Esc to cancel", id="resume-hint")
            if not summaries:
                yield Label("No sessions to resume.", id="resume-empty")
                return
            options: list[Option] = []
            for summary in summaries:
                sid = str(summary.get("session_id") or "")
                if not sid:
                    continue
                self._session_ids.append(sid)
                label = _format_row(summary)
                if sid == self._current:
                    label = f"{label}  (current)"
                options.append(Option(label, id=sid))
            yield OptionList(*options, id="resume-options")

    def action_cancel(self) -> None:
        """Dismiss without choosing a session."""
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """A row selection dismisses with that session id."""
        self.dismiss(self._resolve(event.option_index))

    def action_accept(self) -> None:
        """Dismiss with the highlighted session id (no-op when none)."""
        if not self._session_ids:
            self.dismiss(None)
            return
        try:
            option_list = self.query_one(OptionList)
        except Exception:
            self.dismiss(None)
            return
        index = option_list.highlighted
        if index is None:
            return
        self.dismiss(self._resolve(index))

    def _resolve(self, index: int) -> str | None:
        if 0 <= index < len(self._session_ids):
            return self._session_ids[index]
        return None


__all__ = ["ResumeScreen", "SessionLister"]
