#!/usr/bin/env python3
"""Reusable Textual dialogs for the CLI."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from meeseeks_core.config import get_config_value
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Label, OptionList, SelectionList

_DOTTED_BOX = box.Box(
    ".:.:" "\n: ::" "\n.:.:" "\n: ::" "\n.:.:" "\n.:.:" "\n: ::" "\n.:.:",
    ascii=True,
)


def _textual_enabled() -> bool:
    if get_config_value("cli", "disable_textual", default=False):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass(frozen=True)
class DialogFactory:
    """Factory for lightweight dialogs with optional fallback prompts."""

    console: Console | None = None
    prompt_func: Callable[[str], str] | None = None
    force_textual: bool | None = None
    prefer_inline: bool = False

    def can_use_textual(self) -> bool:
        """Return True when Textual dialogs are allowed."""
        if self.force_textual is not None:
            return self.force_textual
        if self.prefer_inline and self.prompt_func is not None:
            return False
        return _textual_enabled()

    def select_one(
        self,
        title: str,
        options: Sequence[str],
        subtitle: str | None = None,
    ) -> str | None:
        """Select a single option using Textual or a prompt fallback."""
        if not options:
            return None
        if self.can_use_textual():
            dialog = _SingleSelectDialog(title, options, subtitle=subtitle)
            return dialog.run(inline=True)
        return _select_one_fallback(self.console, self.prompt_func, title, options)

    def select_many(
        self,
        title: str,
        options: Sequence[str],
        subtitle: str | None = None,
        preselected: Iterable[str] | None = None,
    ) -> list[str] | None:
        """Select multiple options using Textual or a prompt fallback."""
        if not options:
            return None
        if self.can_use_textual():
            dialog = _MultiSelectDialog(
                title,
                options,
                subtitle=subtitle,
                preselected=preselected,
            )
            return dialog.run(inline=True)
        return _select_many_fallback(
            self.console,
            self.prompt_func,
            title,
            options,
            preselected=preselected,
        )

    def prompt_text(
        self,
        title: str,
        message: str,
        placeholder: str | None = None,
        default: str | None = None,
        allow_empty: bool = False,
    ) -> str | None:
        """Prompt for free-form text using Textual or a prompt fallback."""
        if self.can_use_textual():
            dialog = _TextInputDialog(
                title,
                message,
                placeholder=placeholder,
                default=default,
                allow_empty=allow_empty,
            )
            return dialog.run(inline=True)
        return _prompt_text_fallback(
            self.console,
            self.prompt_func,
            message,
            default=default,
            allow_empty=allow_empty,
        )

    def confirm(
        self,
        title: str,
        message: str,
        default: bool = False,
    ) -> bool | None:
        """Confirm a prompt using Textual or a prompt fallback."""
        if self.can_use_textual():
            dialog = _ConfirmDialog(title, message, default=default)
            return dialog.run(inline=True)
        return _confirm_fallback(self.console, self.prompt_func, message, default=default)


def _select_one_fallback(
    console: Console | None,
    prompt_func: Callable[[str], str] | None,
    title: str,
    options: Sequence[str],
) -> str | None:
    if prompt_func is None:
        return None
    if console is not None:
        console.print(f"{title}:")
        for index, option in enumerate(options, start=1):
            console.print(f"  {index}. {option}")
        console.print("Enter index or id (blank to cancel).")
    choice = prompt_func("Select option: ").strip()
    if not choice:
        return None
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(options):
            return options[index - 1]
        return None
    return choice if choice in options else None


def _select_many_fallback(
    console: Console | None,
    prompt_func: Callable[[str], str] | None,
    title: str,
    options: Sequence[str],
    preselected: Iterable[str] | None = None,
) -> list[str] | None:
    if prompt_func is None:
        return None
    selected = set(preselected or [])
    if console is not None:
        console.print(f"{title}:")
        for index, option in enumerate(options, start=1):
            marker = "*" if option in selected else " "
            console.print(f"  [{marker}] {index}. {option}")
        console.print("Enter comma-separated indices or ids (blank to cancel).")
    choice = prompt_func("Select options: ").strip()
    if not choice:
        return None
    picks = [item.strip() for item in choice.split(",") if item.strip()]
    resolved: list[str] = []
    for pick in picks:
        if pick.isdigit():
            index = int(pick)
            if 1 <= index <= len(options):
                resolved.append(options[index - 1])
            continue
        if pick in options:
            resolved.append(pick)
    return resolved


def _prompt_text_fallback(
    console: Console | None,
    prompt_func: Callable[[str], str] | None,
    message: str,
    default: str | None = None,
    allow_empty: bool = False,
) -> str | None:
    if prompt_func is None:
        return None
    if console is not None:
        console.print(message)
    prompt = "Enter value"
    if default:
        prompt = f"{prompt} [{default}]"
    prompt = f"{prompt}: "
    value = prompt_func(prompt).strip()
    if not value:
        value = default or ""
    if not value and not allow_empty:
        return None
    return value


def _confirm_fallback(
    console: Console | None,
    prompt_func: Callable[[str], str] | None,
    message: str,
    default: bool = False,
) -> bool | None:
    if prompt_func is None:
        return None
    if console is not None:
        console.print(message)
    suffix = "Y/n" if default else "y/N"
    choice = prompt_func(f"{suffix}: ").strip().lower()
    if not choice:
        return default
    return choice in {"y", "yes"}


def _confirm_aider(
    message: str,
    *,
    default: bool = False,
    subject: str | None = None,
    prompt_func: Callable[[str], str] | None = None,
) -> bool | None:
    try:
        from meeseeks_tools.vendor.aider.io import InputOutput
    except Exception:
        return None

    io = InputOutput(pretty=True, fancy_input=False)
    default_char = "y" if default else "n"

    if prompt_func is None:
        return io.confirm_ask(message, default=default_char, subject=subject)

    import builtins

    original_input = builtins.input

    def _fake_input(*args: object, **_kwargs: object) -> str:
        prompt = str(args[0]) if args else ""
        return prompt_func(prompt)

    try:
        builtins.input = _fake_input
        return io.confirm_ask(message, default=default_char, subject=subject)
    finally:
        builtins.input = original_input


def _clear_console_lines(console: Console, line_count: int) -> None:
    if line_count <= 0 or not console.is_terminal:
        return
    try:
        for _ in range(line_count):
            sys.stdout.write("\x1b[1A\x1b[2K")
        sys.stdout.flush()
    except Exception:
        return


def _confirm_rich_panel(
    console: Console | None,
    prompt_func: Callable[[str], str] | None,
    message: str,
    *,
    subject: str | None = None,
    default: bool = False,
    allow_always: bool = False,
    allow_session: bool = False,
) -> str | None:
    if prompt_func is None or console is None:
        return None

    body = Text()
    body.append(message, style="bold")
    if subject:
        body.append("\n")
        body.append(subject, style="dim")

    panel = Panel(
        body,
        title="Tool approval",
        border_style="cyan",
        padding=(1, 2),
        box=_DOTTED_BOX,
    )

    try:
        line_count = len(console.render_lines(panel, console.options))
    except Exception:
        line_count = 0

    console.print(panel)
    suffix = "Y/n" if default else "y/N"
    if allow_always:
        suffix = f"{suffix}/a"
    if allow_session:
        suffix = f"{suffix}/s"
    prompt = f"{suffix}: "
    try:
        response = prompt_func(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if not response:
        response = "y" if default else "n"

    if response in {"a", "always"} and allow_always:
        decision = "always"
    elif response in {"s", "session"} and allow_session:
        decision = "session"
    elif response in {"y", "yes"}:
        decision = "yes"
    else:
        decision = "no"

    _clear_console_lines(console, line_count + 1)
    return decision


class _BaseDialog(App):
    CSS = """
    Screen { align: center middle; }
    #dialog {
        width: 80%;
        max-width: 80;
        border: solid $primary;
        padding: 1 2;
    }
    #title { text-style: bold; }
    #subtitle { color: $text-muted; }
    OptionList, SelectionList { height: auto; max-height: 16; }
    """

    BINDINGS = [
        ("escape,q", "cancel", "Cancel"),
        ("enter", "accept", "Accept"),
    ]

    def action_cancel(self) -> None:
        self.exit(None)

    def action_accept(self) -> None:
        return


class _SingleSelectDialog(_BaseDialog):
    def __init__(self, title: str, options: Sequence[str], subtitle: str | None) -> None:
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._options = list(options)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            if self._subtitle:
                yield Label(self._subtitle, id="subtitle")
            yield OptionList(*self._options, id="options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.exit(self._options[event.option_index])

    def action_accept(self) -> None:
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        if index is None:
            return
        self.exit(self._options[index])


class _MultiSelectDialog(_BaseDialog):
    def __init__(
        self,
        title: str,
        options: Sequence[str],
        subtitle: str | None,
        preselected: Iterable[str] | None,
    ) -> None:
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._options = list(options)
        self._preselected = set(preselected or [])

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            if self._subtitle:
                yield Label(self._subtitle, id="subtitle")
            selections = [(option, option, option in self._preselected) for option in self._options]
            yield SelectionList(*selections, id="options")

    def action_accept(self) -> None:
        selection_list = self.query_one(SelectionList)
        self.exit(list(selection_list.selected))


class _TextInputDialog(_BaseDialog):
    def __init__(
        self,
        title: str,
        message: str,
        placeholder: str | None,
        default: str | None,
        allow_empty: bool,
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._placeholder = placeholder or ""
        self._default = default or ""
        self._allow_empty = allow_empty

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._message, id="subtitle")
            yield Input(
                value=self._default,
                placeholder=self._placeholder,
                id="input",
            )

    def action_accept(self) -> None:
        value = self.query_one(Input).value.strip()
        if not value and not self._allow_empty:
            return
        self.exit(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_accept()


class _ConfirmDialog(_BaseDialog):
    def __init__(self, title: str, message: str, default: bool) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._message, id="subtitle")
            options = ["Yes", "No"] if self._default else ["No", "Yes"]
            yield OptionList(*options, id="options")

    def action_accept(self) -> None:
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        if index is None:
            return
        selection = option_list.get_option_at_index(index).prompt
        self.exit(str(selection).lower().startswith("y"))
