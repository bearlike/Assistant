#!/usr/bin/env python3
"""Aider-derived render helpers for the CLI."""

from __future__ import annotations

from typing import Any

from rich.console import RenderableType
from rich.markdown import Markdown

NoInsetMarkdown: type[Markdown] | None = None
try:
    from meeseeks_tools.vendor.aider.mdstream import NoInsetMarkdown as _NoInsetMarkdown
except Exception:  # pragma: no cover - optional dependency
    _NoInsetMarkdown = None
else:
    NoInsetMarkdown = _NoInsetMarkdown


def render_markdown(text: str, *, style: str | None = None) -> RenderableType:
    """Render markdown using Aider's renderer when available."""
    if NoInsetMarkdown is None:
        return Markdown(text)
    return NoInsetMarkdown(
        text,
        style=style or "",
        code_theme="ansi_dark",
        inline_code_lexer="text",
    )


def render_diff(diff_text: str) -> RenderableType:
    """Render a unified diff as markdown."""
    if not diff_text.strip():
        return render_markdown("(empty diff)")
    fenced = f"```diff\n{diff_text.rstrip()}\n```"
    return render_markdown(fenced)


def render_file_payload(path: str, text: str) -> RenderableType:
    """Render file contents as a markdown code block."""
    header = f"**{path}**"
    fenced = f"```text\n{text.rstrip()}\n```"
    return render_markdown(f"{header}\n\n{fenced}")


def render_dir_payload(path: str, entries: list[str]) -> RenderableType:
    """Render directory entries as a markdown list."""
    header = f"**{path}**"
    if not entries:
        return render_markdown(f"{header}\n\n(no files)")
    items = "\n".join(f"- {entry}" for entry in entries)
    return render_markdown(f"{header}\n\n{items}")


def render_json_payload(payload: dict[str, Any]) -> RenderableType:
    """Render JSON payloads as a fenced code block."""
    return render_markdown(f"```json\n{payload}\n```")


def render_shell_payload(
    command: str | None,
    stdout: str | None,
    stderr: str | None,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    cwd: str | None = None,
) -> RenderableType:
    """Render shell execution output using markdown."""
    header_lines: list[str] = []
    if command:
        header_lines.append(f"$ {command}")
    if cwd:
        header_lines.append(f"cwd: {cwd}")
    if exit_code is not None:
        header_lines.append(f"exit_code: {exit_code}")
    if duration_ms is not None:
        header_lines.append(f"duration_ms: {duration_ms}")
    header = "\n".join(header_lines) or "Shell command"
    body_parts: list[str] = []
    if stdout:
        body_parts.append(f"```text\n{stdout.rstrip()}\n```")
    if stderr:
        body_parts.append(f"```text\n{stderr.rstrip()}\n```")
    body = "\n\n".join(body_parts)
    if body:
        return render_markdown(f"{header}\n\n{body}")
    return render_markdown(header)
