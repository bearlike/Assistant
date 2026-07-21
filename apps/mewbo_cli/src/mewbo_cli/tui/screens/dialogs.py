#!/usr/bin/env python3
"""In-app modal pickers for the Mewbo TUI.

``ModalScreen`` equivalents of the four ``DialogFactory`` primitives
(``select_one`` / ``select_many`` / ``prompt_text`` / ``confirm``). They run
inside the ONE ``MewboApp`` loop — pushed with ``app.push_screen(modal,
callback)`` — instead of spinning up a second blocking Textual ``App`` the way
``DialogFactory`` must for the plain fallback. ``DialogFactory`` stays intact for
the no-TTY path; these are the in-app path, sharing its key contract
(``escape``/``q`` cancels → result ``None``, ``enter`` accepts) so behaviour is
identical across surfaces.

Each modal returns its result via ``dismiss(value)``; the caller passes a
callback to ``push_screen``. They are deliberately thin — colours come from the
active theme's CSS vars (``$primary``…), never hardcoded hex.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, SelectionList

_MODAL_CSS = """
SelectDialogScreen, MultiSelectDialogScreen, TextPromptScreen, ConfirmScreen {
    align: center middle;
}
#dialog {
    width: 80%;
    max-width: 80;
    height: auto;
    max-height: 80%;
    border: solid $primary;
    background: $surface;
    padding: 1 2;
}
#title { text-style: bold; }
#subtitle { color: $text-muted; }
OptionList, SelectionList { height: auto; max-height: 16; }
"""


class _BaseDialogScreen(ModalScreen):
    """Shared key contract: ``escape``/``q`` cancels, ``enter`` accepts."""

    CSS = _MODAL_CSS
    BINDINGS = [
        Binding("escape,q", "cancel", "Cancel"),
        # priority so ``enter`` accepts even when a focused list (e.g.
        # SelectionList, which binds no ``enter``) would otherwise swallow it.
        Binding("enter", "accept", "Accept", show=False, priority=True),
    ]

    def action_cancel(self) -> None:
        """Dismiss with the cancel sentinel (``None``)."""
        self.dismiss(None)

    def action_accept(self) -> None:  # pragma: no cover - overridden
        """Accept the current selection (overridden per dialog)."""
        return


class SelectDialogScreen(_BaseDialogScreen):
    """Single-select picker → the chosen option string, or ``None``."""

    def __init__(
        self,
        title: str,
        options: Sequence[str],
        *,
        subtitle: str | None = None,
    ) -> None:
        """Store the title/options for the modal."""
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._options = list(options)

    def compose(self) -> ComposeResult:
        """Lay out the title, optional subtitle and the option list."""
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            if self._subtitle:
                yield Label(self._subtitle, id="subtitle")
            yield OptionList(*self._options, id="options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Mouse/enter selection on a row dismisses with that option."""
        self.dismiss(self._options[event.option_index])

    def action_accept(self) -> None:
        """Dismiss with the highlighted option (no-op when none)."""
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        if index is None:
            return
        self.dismiss(self._options[index])


class MultiSelectDialogScreen(_BaseDialogScreen):
    """Multi-select picker → the chosen option strings, or ``None``."""

    def __init__(
        self,
        title: str,
        options: Sequence[str],
        *,
        subtitle: str | None = None,
        preselected: Iterable[str] | None = None,
    ) -> None:
        """Store the title/options and any preselected entries."""
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._options = list(options)
        self._preselected = set(preselected or [])

    def compose(self) -> ComposeResult:
        """Lay out the title, optional subtitle and the selection list."""
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            if self._subtitle:
                yield Label(self._subtitle, id="subtitle")
            selections = [(opt, opt, opt in self._preselected) for opt in self._options]
            yield SelectionList(*selections, id="options")

    def action_accept(self) -> None:
        """Dismiss with the list of selected options."""
        selection_list = self.query_one(SelectionList)
        self.dismiss(list(selection_list.selected))


class TextPromptScreen(_BaseDialogScreen):
    """Free-text prompt → the entered string, or ``None``."""

    def __init__(
        self,
        title: str,
        message: str,
        *,
        placeholder: str | None = None,
        default: str | None = None,
        allow_empty: bool = False,
    ) -> None:
        """Store the prompt copy and input defaults."""
        super().__init__()
        self._title = title
        self._message = message
        self._placeholder = placeholder or ""
        self._default = default or ""
        self._allow_empty = allow_empty

    def compose(self) -> ComposeResult:
        """Lay out the title, message and the text input."""
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._message, id="subtitle")
            yield Input(value=self._default, placeholder=self._placeholder, id="input")

    def action_accept(self) -> None:
        """Dismiss with the stripped value (no-op on empty unless allowed)."""
        value = self.query_one(Input).value.strip()
        if not value and not self._allow_empty:
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter inside the input accepts."""
        self.action_accept()


class ConfirmScreen(_BaseDialogScreen):
    """Yes/No confirm → a bool, or ``None`` when cancelled."""

    def __init__(self, title: str, message: str, *, default: bool = False) -> None:
        """Store the prompt copy and the default highlighted answer."""
        super().__init__()
        self._title = title
        self._message = message
        self._default = default

    def compose(self) -> ComposeResult:
        """Lay out the title, message and the Yes/No option list."""
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._message, id="subtitle")
            options = ["Yes", "No"] if self._default else ["No", "Yes"]
            yield OptionList(*options, id="options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """A row selection dismisses with its boolean."""
        prompt = self.query_one(OptionList).get_option_at_index(event.option_index).prompt
        self.dismiss(str(prompt).lower().startswith("y"))

    def action_accept(self) -> None:
        """Dismiss with the highlighted Yes/No as a bool."""
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        if index is None:
            return
        prompt = option_list.get_option_at_index(index).prompt
        self.dismiss(str(prompt).lower().startswith("y"))


__all__ = [
    "ConfirmScreen",
    "MultiSelectDialogScreen",
    "SelectDialogScreen",
    "TextPromptScreen",
]
