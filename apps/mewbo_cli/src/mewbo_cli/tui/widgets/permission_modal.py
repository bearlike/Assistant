#!/usr/bin/env python3
"""Textual approval modal for the Mewbo TUI.

:class:`PermissionModal` is a :class:`~textual.screen.ModalScreen` that
presents a tool-call approval request to the user.  It embeds a
:class:`~mewbo_cli.cli_diffview.DiffView` for edit/write approvals, shows
the raw command for bash/execute calls, and falls back to a key/value summary
for all other tool types.

Dismiss values (passed back to the caller via ``push_screen_wait``)::

    "allow_once"    — approve this call only
    "allow_session" — approve all calls of this tool+action for the session
    "deny"          — deny (default); esc also produces this

Single-letter direct actions:
    a   → allow_once
    s   → allow_session
    d   → deny
    esc → deny  (safe default)

Arrow/tab cycle buttons; enter confirms the focused button.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from mewbo_cli.cli_diffview import DiffView
from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.permission_service import RiskTier

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep


# ---------------------------------------------------------------------------
# Helpers — extract modal content from tool_input
# ---------------------------------------------------------------------------


def _extract_diff_content(
    tool_input: str | dict[str, Any],
) -> tuple[str, str, str | None] | None:
    """Try to extract (old_text, new_text, file_path) for a DiffView.

    Returns ``None`` if the input doesn't look like an edit/write operation.
    """
    if not isinstance(tool_input, dict):
        return None

    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    old = tool_input.get("old_string") or tool_input.get("old_text") or tool_input.get("old") or ""
    new = (
        tool_input.get("new_string")
        or tool_input.get("new_text")
        or tool_input.get("new")
        or tool_input.get("content")
        or ""
    )
    if not isinstance(old, str):
        old = str(old)
    if not isinstance(new, str):
        new = str(new)

    # Only return a diff if there's something to diff
    if new:
        return old, new, file_path or None
    return None


def _extract_command(tool_input: str | dict[str, Any]) -> str | None:
    """Try to extract a shell command string from ``tool_input``."""
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("script")
        return str(cmd) if cmd else None
    return None


def _build_summary(tool_input: str | dict[str, Any], max_lines: int = 20) -> str:
    """Build a human-readable summary for generic tool inputs."""
    if isinstance(tool_input, str):
        lines = tool_input.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"… ({len(lines) - max_lines} more lines)"]
        return "\n".join(lines)
    if isinstance(tool_input, dict):
        items: list[str] = []
        for k, v in list(tool_input.items())[:max_lines]:
            val = str(v)
            if len(val) > 120:
                val = val[:117] + "…"
            items.append(f"{k}: {val}")
        return "\n".join(items)
    return repr(tool_input)


# ---------------------------------------------------------------------------
# Risk tier → badge style
# ---------------------------------------------------------------------------

_TIER_LABEL: dict[RiskTier, str] = {
    RiskTier.READ: "READ",
    RiskTier.WRITE: "WRITE",
    RiskTier.EXEC: "EXEC",
}

_TIER_PALETTE_ATTR: dict[RiskTier, str] = {
    RiskTier.READ: "success",
    RiskTier.WRITE: "warning",
    RiskTier.EXEC: "error",
}


# ---------------------------------------------------------------------------
# PermissionModal
# ---------------------------------------------------------------------------


class PermissionModal(ModalScreen[str]):
    """Approval modal for one :class:`~mewbo_core.classes.ActionStep`.

    Construct with an ``ActionStep`` and an optional ``palette``; mount into
    the App via ``push_screen_wait`` to block the worker thread until the user
    responds.

    Dismiss value is one of ``"allow_once"`` / ``"allow_session"`` / ``"deny"``.
    Pressing ``esc`` dismisses with ``"deny"`` (safe default).

    Args:
        step:    The :class:`~mewbo_core.classes.ActionStep` awaiting approval.
        palette: Semantic colour palette (DI; defaults to :data:`DEFAULT_PALETTE`).
        id:      Optional Textual DOM id.
        classes: Optional Textual CSS classes.
    """

    DEFAULT_CSS = """
    PermissionModal {
        align: center middle;
    }
    #modal-container {
        width: 80%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $panel;
        padding: 1 2;
    }
    #modal-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #risk-badge {
        margin-bottom: 1;
    }
    #modal-body {
        height: auto;
        max-height: 30;
        overflow-y: auto;
        margin-bottom: 1;
    }
    #modal-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #modal-buttons Button {
        margin: 0 1;
        min-width: 18;
    }
    """

    BINDINGS = [
        Binding("a", "allow_once", "Allow once", show=True),
        Binding("s", "allow_session", "Allow session", show=True),
        Binding("d", "deny_action", "Deny", show=True),
        Binding("escape", "deny_action", "Deny", show=True),
    ]

    def __init__(
        self,
        step: ActionStep,
        *,
        palette: Palette | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        """Bind the step and palette."""
        super().__init__(id=id, classes=classes)
        self._step = step
        self._palette = palette or DEFAULT_PALETTE

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Lay out the modal: title, risk badge, content body, and buttons."""
        step = self._step
        tier = RiskTier.classify(step)
        tier_label = _TIER_LABEL[tier]
        tier_color_attr = _TIER_PALETTE_ATTR[tier]
        tier_color = getattr(self._palette, tier_color_attr)

        tool_label = step.tool_id
        if step.operation:
            tool_label = f"{tool_label}:{step.operation}"

        title_text = f"{ICONS.tool_pending} Approve tool call: {tool_label}"
        badge_text = f"[{tier_color}]▌ {tier_label}[/{tier_color}]"

        with Vertical(id="modal-container"):
            yield Static(title_text, id="modal-title")
            yield Static(badge_text, id="risk-badge", markup=True)

            # Body: diff, command, or summary
            diff_content = _extract_diff_content(step.tool_input)
            if diff_content is not None:
                old_text, new_text, file_path = diff_content
                yield DiffView(
                    old_text,
                    new_text,
                    palette=self._palette,
                    file_path=file_path,
                    layout="auto",
                    id="modal-body",
                )
            else:
                command = _extract_command(step.tool_input)
                if command is not None and tier == RiskTier.EXEC:
                    body_text = f"$ {command}"
                else:
                    body_text = _build_summary(step.tool_input)
                yield Static(body_text, id="modal-body")

            # Hint line — step.objective is LLM-sourced; escape it before embedding
            # in markup to prevent injection of Rich tags (e.g. [bold red]…[/bold red]).
            hint = (
                f"[dim]objective:[/dim] {markup_escape(str(step.objective))}"
                if step.objective
                else "[dim]Press [bold]a[/bold]=allow once  [bold]s[/bold]=session  "
                "[bold]d[/bold]/[bold]esc[/bold]=deny[/dim]"
            )
            yield Static(hint, markup=True)

            # Buttons (default focus on Deny for EXEC, Allow for READ)
            with Horizontal(id="modal-buttons"):
                yield Button("(a) Allow once", id="btn-allow", variant="success")
                yield Button("(s) Allow session", id="btn-session", variant="warning")
                yield Button("(d) Deny", id="btn-deny", variant="error")

    def on_mount(self) -> None:
        """Focus the safe default button after mounting."""
        tier = RiskTier.classify(self._step)
        # For destructive/exec operations default focus is Deny (safe)
        # For read operations default focus is Allow
        if tier == RiskTier.READ:
            self.query_one("#btn-allow").focus()
        else:
            self.query_one("#btn-deny").focus()

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to dismiss actions."""
        if event.button.id == "btn-allow":
            self.action_allow_once()
        elif event.button.id == "btn-session":
            self.action_allow_session()
        else:
            self.action_deny_action()

    # ------------------------------------------------------------------
    # Textual actions (key bindings)
    # ------------------------------------------------------------------

    def action_allow_once(self) -> None:
        """Dismiss with 'allow_once'."""
        self.dismiss("allow_once")

    def action_allow_session(self) -> None:
        """Dismiss with 'allow_session'."""
        self.dismiss("allow_session")

    def action_deny_action(self) -> None:
        """Dismiss with 'deny' (safe default for esc)."""
        self.dismiss("deny")


__all__ = ["PermissionModal"]
