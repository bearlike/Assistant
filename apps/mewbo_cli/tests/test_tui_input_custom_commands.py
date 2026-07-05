#!/usr/bin/env python3
"""Tests for CustomCommandLoader (issue #155, epic #149)."""

from __future__ import annotations

from pathlib import Path

from mewbo_cli.tui.input.custom_commands import CustomCommandLoader


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_loads_frontmatter_and_body(tmp_path):
    proj = tmp_path / "proj"
    _write(
        proj / ".claude" / "commands" / "review.md",
        "---\n"
        "description: Review the diff\n"
        "argument-hint: <path>\n"
        "allowed-tools: Read, Grep\n"
        "---\n"
        "Please review $ARGUMENTS now.\n",
    )
    loader = CustomCommandLoader(cwd=str(proj), user_root=tmp_path / "nouser")
    commands = loader.load()
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd.name == "review"
    assert cmd.description == "Review the diff"
    assert cmd.argument_hint == "<path>"
    assert cmd.allowed_tools == ("Read", "Grep")
    assert cmd.source == "project"


def test_arguments_substitution(tmp_path):
    proj = tmp_path / "proj"
    _write(
        proj / ".claude" / "commands" / "echo.md",
        "---\ndescription: x\n---\nrun: $ARGUMENTS",
    )
    loader = CustomCommandLoader(cwd=str(proj), user_root=tmp_path / "nouser")
    cmd = loader.load()[0]
    assert cmd.render("a b c") == "run: a b c"
    assert cmd.render() == "run: "


def test_subdir_namespacing(tmp_path):
    proj = tmp_path / "proj"
    _write(
        proj / ".claude" / "commands" / "frontend" / "component.md",
        "---\ndescription: scaffold\n---\nbody",
    )
    loader = CustomCommandLoader(cwd=str(proj), user_root=tmp_path / "nouser")
    cmd = loader.load()[0]
    assert cmd.name == "frontend:component"


def test_project_overrides_user_on_clash(tmp_path):
    user_root = tmp_path / "user"
    _write(user_root / "dup.md", "---\ndescription: from user\n---\nU")
    proj = tmp_path / "proj"
    _write(
        proj / ".claude" / "commands" / "dup.md",
        "---\ndescription: from project\n---\nP",
    )
    loader = CustomCommandLoader(cwd=str(proj), user_root=user_root)
    commands = {c.name: c for c in loader.load()}
    assert commands["dup"].description == "from project"
    assert commands["dup"].source == "project"


def test_user_root_loaded(tmp_path):
    user_root = tmp_path / "user"
    _write(user_root / "global.md", "---\ndescription: g\n---\nbody")
    loader = CustomCommandLoader(cwd=str(tmp_path / "empty"), user_root=user_root)
    commands = loader.load()
    assert [c.name for c in commands] == ["global"]
    assert commands[0].source == "user"


def test_no_frontmatter_uses_whole_body(tmp_path):
    proj = tmp_path / "proj"
    _write(proj / ".claude" / "commands" / "plain.md", "just a body $ARGUMENTS")
    loader = CustomCommandLoader(cwd=str(proj), user_root=tmp_path / "nouser")
    cmd = loader.load()[0]
    assert cmd.description == ""
    assert cmd.render("x") == "just a body x"


def test_malformed_frontmatter_skips_gracefully(tmp_path):
    proj = tmp_path / "proj"
    # Invalid YAML in the frontmatter block — must not raise, body still loads.
    _write(
        proj / ".claude" / "commands" / "bad.md",
        "---\n: : not yaml : :\n---\nbody",
    )
    loader = CustomCommandLoader(cwd=str(proj), user_root=tmp_path / "nouser")
    commands = loader.load()
    assert len(commands) == 1
    assert commands[0].description == ""


def test_missing_roots_return_empty(tmp_path):
    loader = CustomCommandLoader(cwd=str(tmp_path / "nope"), user_root=tmp_path / "nope2")
    assert loader.load() == []


def test_allowed_tools_list_form(tmp_path):
    proj = tmp_path / "proj"
    _write(
        proj / ".claude" / "commands" / "list.md",
        "---\ndescription: x\nallowed-tools:\n  - Read\n  - Write\n---\nbody",
    )
    loader = CustomCommandLoader(cwd=str(proj), user_root=tmp_path / "nouser")
    cmd = loader.load()[0]
    assert cmd.allowed_tools == ("Read", "Write")
