#!/usr/bin/env python3
"""Tests for the declarative keymap + user-override loader (#157)."""

from __future__ import annotations

import json
from pathlib import Path

from mewbo_cli.tui.keybindings import (
    DEFAULT_BINDINGS,
    KeyBinding,
    KeybindingConfig,
)


def test_defaults_own_the_three_global_keys() -> None:
    keys = {b.action: b.key for b in DEFAULT_BINDINGS}
    assert keys == {
        "open_transcript": "ctrl+o",
        "open_sessions": "ctrl+s",
        "clear_redraw": "ctrl+l",
    }


def test_no_overrides_returns_defaults() -> None:
    cfg = KeybindingConfig(path=None)
    assert [b.key for b in cfg.bindings()] == ["ctrl+o", "ctrl+s", "ctrl+l"]


def test_override_repoints_known_action(tmp_path: Path) -> None:
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps({"open_transcript": "ctrl+t"}))
    cfg = KeybindingConfig(path=target)
    by_action = {b.action: b.key for b in cfg.bindings()}
    assert by_action["open_transcript"] == "ctrl+t"
    # untouched actions keep their default
    assert by_action["open_sessions"] == "ctrl+s"


def test_override_for_unknown_action_is_ignored(tmp_path: Path) -> None:
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps({"does_not_exist": "ctrl+z"}))
    cfg = KeybindingConfig(path=target)
    actions = {b.action for b in cfg.bindings()}
    assert "does_not_exist" not in actions
    assert actions == {"open_transcript", "open_sessions", "clear_redraw"}


def test_bad_json_is_skipped_not_raised(tmp_path: Path) -> None:
    target = tmp_path / "keybindings.json"
    target.write_text("{ this is not json")
    cfg = KeybindingConfig(path=target)
    # degrades to defaults, never raises
    assert [b.key for b in cfg.bindings()] == ["ctrl+o", "ctrl+s", "ctrl+l"]


def test_non_object_json_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "keybindings.json"
    target.write_text(json.dumps(["ctrl+o"]))
    cfg = KeybindingConfig(path=target)
    assert [b.key for b in cfg.bindings()] == ["ctrl+o", "ctrl+s", "ctrl+l"]


def test_missing_file_is_fine(tmp_path: Path) -> None:
    cfg = KeybindingConfig(path=tmp_path / "absent.json")
    assert [b.action for b in cfg.bindings()] == [
        "open_transcript",
        "open_sessions",
        "clear_redraw",
    ]


def test_describe_lists_keys_and_override_path(tmp_path: Path) -> None:
    target = tmp_path / "keybindings.json"
    cfg = KeybindingConfig(path=target)
    text = cfg.describe()
    assert "ctrl+o" in text
    assert "Transcript" in text
    assert str(target) in text
    assert "open_transcript" in text  # action names listed


def test_custom_defaults_respected() -> None:
    custom = (KeyBinding("open_transcript", "f1", "Help"),)
    cfg = KeybindingConfig(defaults=custom, path=None)
    bindings = cfg.bindings()
    assert len(bindings) == 1
    assert bindings[0].key == "f1"
