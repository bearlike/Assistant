"""Tests for CLI dialog fallbacks."""

from __future__ import annotations

from rich.console import Console
from truss_cli import cli_dialogs


def test_dialog_factory_prefers_prompt_when_inline(monkeypatch):
    """Disable Textual dialogs when inline prompting is preferred."""
    monkeypatch.setattr(cli_dialogs, "_textual_enabled", lambda: True)
    dialogs = cli_dialogs.DialogFactory(prompt_func=lambda _: "1", prefer_inline=True)
    assert dialogs.can_use_textual() is False


def test_dialog_factory_force_textual(monkeypatch):
    """Force Textual dialogs when requested."""
    monkeypatch.setattr(cli_dialogs, "_textual_enabled", lambda: False)
    dialogs = cli_dialogs.DialogFactory(prompt_func=lambda _: "1", force_textual=True)
    assert dialogs.can_use_textual() is True


def test_select_one_fallback_by_index():
    """Select an option by numeric index."""
    console = Console(record=True)
    choice = cli_dialogs._select_one_fallback(
        console,
        lambda _: "2",
        "Pick",
        ["one", "two"],
    )
    assert choice == "two"


def test_select_many_fallback_by_index_and_id():
    """Select multiple options using mixed picks."""
    console = Console(record=True)
    choice = cli_dialogs._select_many_fallback(
        console,
        lambda _: "1, beta",
        "Pick",
        ["alpha", "beta", "gamma"],
    )
    assert choice == ["alpha", "beta"]


def test_prompt_text_fallback_defaults():
    """Return default values when input is empty."""
    console = Console(record=True)
    value = cli_dialogs._prompt_text_fallback(
        console,
        lambda _: "",
        "Enter value",
        default="hello",
    )
    assert value == "hello"


def test_prompt_text_fallback_rejects_empty_when_not_allowed():
    """Reject empty input when no default is provided."""
    console = Console(record=True)
    value = cli_dialogs._prompt_text_fallback(
        console,
        lambda _: "",
        "Enter value",
        default=None,
        allow_empty=False,
    )
    assert value is None


def test_confirm_fallback_respects_default():
    """Use default when no input is provided."""
    console = Console(record=True)
    result = cli_dialogs._confirm_fallback(console, lambda _: "", "Confirm?", default=True)
    assert result is True


def test_confirm_fallback_negative_response():
    """Handle explicit negative responses."""
    console = Console(record=True)
    result = cli_dialogs._confirm_fallback(console, lambda _: "n", "Confirm?", default=True)
    assert result is False


def test_confirm_rich_panel_accepts_yes(monkeypatch):
    """Return yes when prompt input is affirmative."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "y")
    result = cli_dialogs._confirm_rich_panel(
        console,
        "Approve tool use?",
        subject="tool:action",
        default=False,
    )
    assert result == "yes"


def test_confirm_rich_panel_allows_always(monkeypatch):
    """Return always when prompt input selects auto-approve."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "a")
    result = cli_dialogs._confirm_rich_panel(
        console,
        "Approve tool use?",
        subject="tool:action",
        default=False,
        allow_always=True,
    )
    assert result == "always"


def test_confirm_rich_panel_allows_session(monkeypatch):
    """Return session when prompt input selects session-wide approval."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "s")
    result = cli_dialogs._confirm_rich_panel(
        console,
        "Approve tool use?",
        subject="tool:action",
        default=False,
        allow_session=True,
    )
    assert result == "session"
