"""Extra tests for cli_dialogs.py — targeting uncovered branches."""

from __future__ import annotations

import sys

from mewbo_cli import cli_dialogs
from mewbo_cli.cli_dialogs import (
    DialogFactory,
    _confirm_fallback,
    _confirm_rich_panel,
    _prompt_text_fallback,
    _select_many_fallback,
    _select_one_fallback,
    _textual_enabled,
)
from rich.console import Console

# ---------------------------------------------------------------------------
# _textual_enabled
# ---------------------------------------------------------------------------


def test_textual_enabled_disabled_by_config(monkeypatch):
    """Returns False when cli.disable_textual is True in config."""
    monkeypatch.setattr(cli_dialogs, "get_config_value", lambda *a, **kw: True)
    assert _textual_enabled() is False


def test_textual_enabled_not_a_tty(monkeypatch):
    """Returns False when stdin/stdout are not TTYs."""
    monkeypatch.setattr(cli_dialogs, "get_config_value", lambda *a, **kw: False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert _textual_enabled() is False


def test_textual_enabled_stdout_not_tty(monkeypatch):
    """Returns False when stdout is not a TTY."""
    monkeypatch.setattr(cli_dialogs, "get_config_value", lambda *a, **kw: False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert _textual_enabled() is False


# ---------------------------------------------------------------------------
# DialogFactory.can_use_textual
# ---------------------------------------------------------------------------


def test_dialog_factory_can_use_textual_env_enabled(monkeypatch):
    """Delegates to _textual_enabled when force_textual is not set."""
    monkeypatch.setattr(cli_dialogs, "_textual_enabled", lambda: True)
    dialogs = DialogFactory()
    assert dialogs.can_use_textual() is True


def test_dialog_factory_can_use_textual_env_disabled(monkeypatch):
    """Returns False via _textual_enabled when TTY is absent."""
    monkeypatch.setattr(cli_dialogs, "_textual_enabled", lambda: False)
    dialogs = DialogFactory()
    assert dialogs.can_use_textual() is False


def test_dialog_factory_prefer_inline_without_prompt(monkeypatch):
    """prefer_inline=True has no effect when prompt_func is None."""
    monkeypatch.setattr(cli_dialogs, "_textual_enabled", lambda: True)
    dialogs = DialogFactory(prompt_func=None, prefer_inline=True)
    # prefer_inline gate: prompt_func is None → skip the gate → delegate to env
    assert dialogs.can_use_textual() is True


# ---------------------------------------------------------------------------
# DialogFactory.select_one — fallback path
# ---------------------------------------------------------------------------


def test_dialog_factory_select_one_fallback_empty():
    """Returns None for empty option list."""
    dialogs = DialogFactory(force_textual=False, prompt_func=lambda _: "")
    result = dialogs.select_one("Pick", [])
    assert result is None


def test_dialog_factory_select_one_fallback_by_index():
    """Falls back to _select_one_fallback; picks by index."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "1",
    )
    result = dialogs.select_one("Pick", ["alpha", "beta"])
    assert result == "alpha"


def test_dialog_factory_select_one_fallback_cancel():
    """Falls back and returns None on blank input."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "",
    )
    result = dialogs.select_one("Pick", ["a", "b"])
    assert result is None


# ---------------------------------------------------------------------------
# DialogFactory.select_many — fallback path
# ---------------------------------------------------------------------------


def test_dialog_factory_select_many_fallback_empty():
    """Returns None for empty options."""
    dialogs = DialogFactory(force_textual=False, prompt_func=lambda _: "")
    result = dialogs.select_many("Pick", [])
    assert result is None


def test_dialog_factory_select_many_fallback_picks():
    """Falls back and picks by comma-separated indices."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "1,3",
    )
    result = dialogs.select_many("Pick", ["a", "b", "c"])
    assert result == ["a", "c"]


def test_dialog_factory_select_many_fallback_cancel():
    """Falls back and returns None on blank input."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "",
    )
    result = dialogs.select_many("Pick", ["a", "b"])
    assert result is None


# ---------------------------------------------------------------------------
# DialogFactory.prompt_text — fallback path
# ---------------------------------------------------------------------------


def test_dialog_factory_prompt_text_fallback_value():
    """Falls back; returns user-typed value."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "hello",
    )
    result = dialogs.prompt_text("Title", "Enter value")
    assert result == "hello"


def test_dialog_factory_prompt_text_fallback_empty_not_allowed():
    """Falls back; returns None when empty input and allow_empty=False."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "",
    )
    result = dialogs.prompt_text("Title", "Enter value", allow_empty=False)
    assert result is None


def test_dialog_factory_prompt_text_fallback_empty_allowed():
    """Falls back; returns empty string when allow_empty=True."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "",
    )
    result = dialogs.prompt_text("Title", "Enter value", allow_empty=True)
    assert result == ""


# ---------------------------------------------------------------------------
# DialogFactory.confirm — fallback path
# ---------------------------------------------------------------------------


def test_dialog_factory_confirm_fallback_yes():
    """Falls back and returns True for 'y' input."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "y",
    )
    result = dialogs.confirm("Title", "Are you sure?")
    assert result is True


def test_dialog_factory_confirm_fallback_no():
    """Falls back and returns False for 'n' input."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "n",
    )
    result = dialogs.confirm("Title", "Are you sure?", default=True)
    assert result is False


def test_dialog_factory_confirm_fallback_default():
    """Falls back and uses default on blank input."""
    dialogs = DialogFactory(
        console=Console(record=True),
        force_textual=False,
        prompt_func=lambda _: "",
    )
    result = dialogs.confirm("Title", "Are you sure?", default=True)
    assert result is True


# ---------------------------------------------------------------------------
# _select_one_fallback — edge cases
# ---------------------------------------------------------------------------


def test_select_one_fallback_no_prompt_func():
    """Returns None when prompt_func is None."""
    result = _select_one_fallback(None, None, "Pick", ["a", "b"])
    assert result is None


def test_select_one_fallback_no_console():
    """Works without a console (prompt_func only)."""
    result = _select_one_fallback(None, lambda _: "1", "Pick", ["x"])
    assert result == "x"


def test_select_one_fallback_out_of_range():
    """Returns None for out-of-range index."""
    result = _select_one_fallback(None, lambda _: "99", "Pick", ["a", "b"])
    assert result is None


def test_select_one_fallback_invalid_id():
    """Returns None for unrecognized string input."""
    result = _select_one_fallback(None, lambda _: "unknown-opt", "Pick", ["a", "b"])
    assert result is None


def test_select_one_fallback_by_name():
    """Returns matching option by name string."""
    result = _select_one_fallback(None, lambda _: "beta", "Pick", ["alpha", "beta"])
    assert result == "beta"


# ---------------------------------------------------------------------------
# _select_many_fallback — edge cases
# ---------------------------------------------------------------------------


def test_select_many_fallback_no_prompt_func():
    """Returns None when prompt_func is None."""
    result = _select_many_fallback(None, None, "Pick", ["a", "b"])
    assert result is None


def test_select_many_fallback_no_console():
    """Works without a console."""
    result = _select_many_fallback(None, lambda _: "1", "Pick", ["a", "b"])
    assert result == ["a"]


def test_select_many_fallback_preselected():
    """Shows preselected marker in output."""
    console = Console(record=True)
    result = _select_many_fallback(
        console, lambda _: "1,2", "Pick", ["a", "b", "c"], preselected=["b"]
    )
    output = console.export_text()
    assert "[*]" in output  # preselected marker
    assert result == ["a", "b"]


def test_select_many_fallback_by_name():
    """Resolves named picks in addition to indexed picks."""
    result = _select_many_fallback(None, lambda _: "gamma", "Pick", ["alpha", "beta", "gamma"])
    assert result == ["gamma"]


def test_select_many_fallback_out_of_range_skipped():
    """Out-of-range indices are silently skipped."""
    result = _select_many_fallback(None, lambda _: "1,99", "Pick", ["a", "b"])
    assert result == ["a"]


def test_select_many_fallback_cancel():
    """Blank input returns None."""
    result = _select_many_fallback(None, lambda _: "", "Pick", ["a", "b"])
    assert result is None


# ---------------------------------------------------------------------------
# _prompt_text_fallback — edge cases
# ---------------------------------------------------------------------------


def test_prompt_text_fallback_no_prompt_func():
    """Returns None when prompt_func is None."""
    result = _prompt_text_fallback(None, None, "Enter value")
    assert result is None


def test_prompt_text_fallback_with_default_in_prompt():
    """Includes default value in prompt string."""
    prompts: list[str] = []
    result = _prompt_text_fallback(
        None, lambda p: prompts.append(p) or "", "Enter value", default="foo"
    )
    assert any("foo" in p for p in prompts)
    assert result == "foo"


def test_prompt_text_fallback_allow_empty_returns_empty_string():
    """Returns empty string when allow_empty=True and user enters blank."""
    result = _prompt_text_fallback(None, lambda _: "", "Enter", allow_empty=True)
    assert result == ""


def test_prompt_text_fallback_strips_whitespace():
    """Strips leading/trailing whitespace from input."""
    result = _prompt_text_fallback(None, lambda _: "  hi  ", "Enter")
    assert result == "hi"


# ---------------------------------------------------------------------------
# _confirm_fallback — edge cases
# ---------------------------------------------------------------------------


def test_confirm_fallback_no_prompt_func():
    """Returns None when prompt_func is None."""
    result = _confirm_fallback(None, None, "Are you sure?")
    assert result is None


def test_confirm_fallback_yes_keyword():
    """Returns True for 'yes' input."""
    result = _confirm_fallback(None, lambda _: "yes", "Confirm?")
    assert result is True


def test_confirm_fallback_default_false_no_input():
    """Returns False when default=False and input is blank."""
    result = _confirm_fallback(None, lambda _: "", "Confirm?", default=False)
    assert result is False


def test_confirm_fallback_with_console():
    """Prints message to console before prompting."""
    console = Console(record=True)
    result = _confirm_fallback(console, lambda _: "y", "Please confirm")
    assert result is True
    assert "Please confirm" in console.export_text()


# ---------------------------------------------------------------------------
# _confirm_rich_panel — edge cases
# ---------------------------------------------------------------------------


def test_confirm_rich_panel_no_subject(monkeypatch):
    """Works without a subject line."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "y")
    result = _confirm_rich_panel(console, "Approve?", default=False)
    assert result == "yes"


def test_confirm_rich_panel_default_yes_on_blank(monkeypatch):
    """Returns 'yes' when default=True and input is blank."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "")
    result = _confirm_rich_panel(console, "Approve?", default=True)
    assert result == "yes"


def test_confirm_rich_panel_default_no_on_blank(monkeypatch):
    """Returns 'no' when default=False and input is blank."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "")
    result = _confirm_rich_panel(console, "Approve?", default=False)
    assert result == "no"


def test_confirm_rich_panel_always_without_flag(monkeypatch):
    """'a' input does NOT return 'always' when allow_always=False."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "a")
    result = _confirm_rich_panel(console, "Approve?", allow_always=False)
    assert result == "no"


def test_confirm_rich_panel_session_without_flag(monkeypatch):
    """'s' input does NOT return 'session' when allow_session=False."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "s")
    result = _confirm_rich_panel(console, "Approve?", allow_session=False)
    assert result == "no"


def test_confirm_rich_panel_eof_treated_as_no(monkeypatch):
    """EOFError during input is treated as 'no'."""
    console = Console(record=True)

    def _raise(*a, **kw):
        raise EOFError

    monkeypatch.setattr(console, "input", _raise)
    result = _confirm_rich_panel(console, "Approve?", default=False)
    assert result == "no"


def test_confirm_rich_panel_keyboard_interrupt_treated_as_no(monkeypatch):
    """KeyboardInterrupt during input is treated as 'no'."""
    console = Console(record=True)

    def _raise(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(console, "input", _raise)
    result = _confirm_rich_panel(console, "Approve?", default=False)
    assert result == "no"


def test_confirm_rich_panel_full_word_always(monkeypatch):
    """'always' input returns 'always' when allow_always=True."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "always")
    result = _confirm_rich_panel(console, "Approve?", allow_always=True)
    assert result == "always"


def test_confirm_rich_panel_full_word_session(monkeypatch):
    """'session' input returns 'session' when allow_session=True."""
    console = Console(record=True)
    monkeypatch.setattr(console, "input", lambda _prompt="", **kw: "session")
    result = _confirm_rich_panel(console, "Approve?", allow_session=True)
    assert result == "session"
