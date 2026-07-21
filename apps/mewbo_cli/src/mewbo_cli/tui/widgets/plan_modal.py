#!/usr/bin/env python3
"""Plan-approval modal for the Mewbo TUI.

:class:`PlanApprovalModal` is a :class:`~textual.screen.ModalScreen` that
presents a proposed plan for a single interactive decision — mirroring the
:class:`~mewbo_cli.tui.widgets.permission_modal.PermissionModal` resolver
pattern (``push_screen_wait`` blocks the approval worker until the user
chooses). It is the App's ONE approval surface for plan mode; there are no
plan slash commands.

Dismiss values (passed back via ``push_screen_wait``)::

    "approve" — approve the plan and execute it (plan → act)
    "refine"  — keep planning; the user will send refinement feedback (default)
    "reject"  — abandon the proposal

Single-letter direct actions:
    a   → approve
    k   → refine  (keep planning)
    r   → reject
    esc → refine  (safe default — nothing destructive happens)
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from mewbo_cli.aider_ui import render_markdown
from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette


class PlanApprovalModal(ModalScreen[str]):
    """Approve / refine / reject decision for a proposed plan.

    Construct with the plan markdown + revision; mount via ``push_screen_wait``
    to block the worker thread until the user decides. Dismiss value is one of
    ``"approve"`` / ``"refine"`` / ``"reject"``; ``esc`` dismisses with
    ``"refine"`` (safe default — keep planning).
    """

    DEFAULT_CSS = """
    PlanApprovalModal {
        align: center middle;
    }
    #plan-modal-container {
        width: 80%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $panel;
        padding: 1 2;
    }
    #plan-modal-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #plan-modal-body {
        height: auto;
        max-height: 30;
        overflow-y: auto;
        margin-bottom: 1;
    }
    #plan-modal-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #plan-modal-buttons Button {
        margin: 0 1;
        min-width: 20;
    }
    """

    BINDINGS = [
        Binding("a", "approve", "Approve", show=True),
        Binding("k", "refine", "Keep planning", show=True),
        Binding("r", "reject", "Reject", show=True),
        Binding("escape", "refine", "Keep planning", show=True),
    ]

    def __init__(
        self,
        plan_markdown: str,
        revision: int = 0,
        *,
        palette: Palette | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        """Bind the plan content, revision and palette."""
        super().__init__(id=id, classes=classes)
        self._plan_markdown = plan_markdown
        self._revision = revision
        self._palette = palette or DEFAULT_PALETTE

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Lay out the modal: title, scrollable plan body, and the 3 choices."""
        title = f"{ICONS.plan} Proposed plan"
        if self._revision:
            title = f"{title}  ·  revision {self._revision}"

        with Vertical(id="plan-modal-container"):
            yield Static(title, id="plan-modal-title")
            body = self._plan_markdown.strip()
            with VerticalScroll(id="plan-modal-body"):
                yield Static(
                    render_markdown(body) if body else "(empty plan)",
                )
            yield Static(
                "[dim]Press [bold]a[/bold]=approve & execute  "
                "[bold]k[/bold]/[bold]esc[/bold]=keep planning  "
                "[bold]r[/bold]=reject[/dim]",
                markup=True,
            )
            with Horizontal(id="plan-modal-buttons"):
                yield Button("(a) Approve & execute", id="btn-approve", variant="success")
                yield Button("(k) Keep planning", id="btn-refine", variant="primary")
                yield Button("(r) Reject", id="btn-reject", variant="error")

    def on_mount(self) -> None:
        """Focus the safe default (keep planning) after mounting."""
        self.query_one("#btn-refine").focus()

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to dismiss actions."""
        if event.button.id == "btn-approve":
            self.action_approve()
        elif event.button.id == "btn-reject":
            self.action_reject()
        else:
            self.action_refine()

    def action_approve(self) -> None:
        """Dismiss with 'approve'."""
        self.dismiss("approve")

    def action_refine(self) -> None:
        """Dismiss with 'refine' (safe default for esc — keep planning)."""
        self.dismiss("refine")

    def action_reject(self) -> None:
        """Dismiss with 'reject'."""
        self.dismiss("reject")


__all__ = ["PlanApprovalModal"]
