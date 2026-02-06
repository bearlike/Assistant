"""Tests for Aider UI render helpers."""

from __future__ import annotations

from meeseeks_cli import aider_ui
from rich.markdown import Markdown


def test_render_diff_empty_returns_placeholder():
    """Render empty diff content with placeholder."""
    rendered = aider_ui.render_diff("")
    assert isinstance(rendered, Markdown)
    assert "empty diff" in rendered.markup


def test_render_file_payload_renders_code_block():
    """Render file payloads with a header and code fence."""
    rendered = aider_ui.render_file_payload("hello.txt", "hello\n")
    assert isinstance(rendered, Markdown)
    assert "```text" in rendered.markup


def test_render_dir_payload_handles_empty_entries():
    """Render empty directory entries with no-files message."""
    rendered = aider_ui.render_dir_payload("root", [])
    assert isinstance(rendered, Markdown)
    assert "no files" in rendered.markup.lower()


def test_render_shell_payload_renders_header():
    """Render shell payload with command header."""
    rendered = aider_ui.render_shell_payload(
        "echo hello",
        "hello\n",
        "",
        exit_code=0,
        duration_ms=10,
        cwd=".",
    )
    assert isinstance(rendered, Markdown)
    assert "$ echo hello" in rendered.markup
