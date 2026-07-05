#!/usr/bin/env python3
"""Header widget for the Mewbo TUI (issue #150).

Provides three public symbols:

- :class:`HeaderContext` — frozen dataclass carrying all data needed to render
  the CLI header (moved verbatim from ``cli_master.py`` so later refactors can
  import from here instead).
- :class:`HeaderView` — pure, side-effect-free renderer. ``render(width)``
  dispatches to wide / normal / tiny variants and returns a Rich
  :class:`~rich.console.Group` (content only; no background style baked in so
  the caller / Textual CSS can apply theming).
- :class:`HeaderWidget` — thin :class:`~textual.widgets.Static` wrapper that
  re-renders on mount and on resize so the header tracks terminal width.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.text import Text
from textual import events
from textual.widgets import Static

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderContext:
    """Structured data needed to render the CLI header."""

    title: str
    version: str
    status_label: str
    status_color: str
    model: str
    session_id: str
    base_url: str
    langfuse_enabled: bool
    langfuse_reason: str | None
    builtin_enabled: int
    builtin_disabled: int
    external_enabled: int
    external_disabled: int
    skill_count: int = 0
    # Honest local-vs-remote transcript indicator (Gitea #171): "local-only" when
    # the CLI is fully local, "remote: <base_url>" when session sync is opted in.
    transcript_sink: str = "local-only"


# ---------------------------------------------------------------------------
# Module-private helpers (mirror cli_master.py helpers)
# ---------------------------------------------------------------------------


def _truncate_middle(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    keep = max_len - 3
    head = max(1, keep // 2)
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"


def _short_model(model: str, max_len: int = 28) -> str:
    return _truncate_middle(model, max_len)


def _short_url(base_url: str, max_len: int = 36) -> str:
    return _truncate_middle(base_url, max_len)


def _format_model(model: str, max_len: int) -> Text:
    shortened = _short_model(model, max_len)
    if "/" not in shortened:
        return Text(shortened, style="bright_white")
    provider, name = shortened.split("/", 1)
    text = Text()
    text.append(provider, style="cyan")
    text.append("/", style="dim")
    text.append(name, style="bright_white")
    return text


def _brand_line(ctx: HeaderContext, width: int) -> Text:
    title = f"■ {ctx.title} v{ctx.version}"
    status = f"o {ctx.status_label}"
    spacing = max(1, width - len(title) - len(status))
    line = Text()
    line.append(title, style="bold bright_cyan")
    line.append(" " * spacing)
    line.append(status, style=f"bold {ctx.status_color}")
    return line


def _kv_line(label: str, value: Text | str, label_width: int) -> Text:
    line = Text()
    line.append(label.ljust(label_width), style="dim")
    line.append(" ")
    if isinstance(value, Text):
        line.append_text(value)
    else:
        line.append(value)
    return line


def _langfuse_value(ctx: HeaderContext) -> Text:
    status = Text()
    status.append("o ", style="green" if ctx.langfuse_enabled else "red")
    status.append("on" if ctx.langfuse_enabled else "off", style="dim")
    return status


def _tools_value(ctx: HeaderContext) -> Text:
    text = Text()
    label_builtin = "built-in"
    label_external = "external"

    text.append(f"{label_builtin} ", style="dim")
    text.append("o", style="green")
    text.append(f" {ctx.builtin_enabled}", style="dim")
    text.append(" (", style="dim")
    text.append("o", style="red")
    text.append(f" {ctx.builtin_disabled}", style="dim")
    text.append(") ", style="dim")

    text.append("• ", style="dim")
    text.append(f"{label_external} ", style="dim")
    text.append("o", style="green")
    text.append(f" {ctx.external_enabled}", style="dim")
    text.append(" (", style="dim")
    text.append("o", style="red")
    text.append(f" {ctx.external_disabled}", style="dim")
    text.append(")", style="dim")
    return text


def _sink_value(ctx: HeaderContext) -> Text:
    """Render the transcript-sink indicator: dim for local-only, accented remote."""
    sink = ctx.transcript_sink or "local-only"
    text = Text()
    if sink.lower().startswith("remote"):
        head, _, url = sink.partition(":")
        text.append(head.strip(), style="yellow")
        if url.strip():
            text.append(" ", style="dim")
            text.append(_short_url(url.strip(), 40), style="bright_white")
    else:
        text.append(sink, style="dim")
    return text


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class HeaderView:
    """Pure, side-effect-free header renderer.

    Construct with a :class:`HeaderContext`, then call :meth:`render` with the
    available pixel width to get back a Rich renderable (a
    :class:`~rich.console.Group`).  The caller is responsible for any
    background styling; this class returns content only.

    Width dispatch mirrors the legacy ``render_header`` dispatcher:
    - width >= 100 → wide  (all 6 KV rows + horizontal rules)
    - width >= 70  → normal (4–5 KV rows + rules)
    - else         → tiny  (single inline line + 2 detail lines)
    """

    def __init__(self, ctx: HeaderContext) -> None:
        """Bind the :class:`HeaderContext` used for all subsequent renders."""
        self._ctx = ctx

    def render(self, width: int) -> RenderableType:
        """Return a Rich renderable for ``width`` columns."""
        if width >= 100:
            return self._wide(width)
        if width >= 70:
            return self._normal(width)
        return self._tiny()

    # --- wide ---------------------------------------------------------------

    def _wide(self, width: int) -> RenderableType:
        ctx = self._ctx
        parts: list[RenderableType] = [
            Text(""),  # blank line above rule
            Rule(style="dim"),
            _brand_line(ctx, width),
        ]

        skills_text = Text()
        style = "dim" if ctx.skill_count else "red dim"
        skills_text.append(f"{ctx.skill_count} available", style=style)

        fields: list[tuple[str, Text | str]] = [
            ("model", _format_model(ctx.model, 40)),
            ("session", ctx.session_id or "(not set)"),
            ("base", _short_url(ctx.base_url, 60) if ctx.base_url else "(not set)"),
            ("sink", _sink_value(ctx)),
            ("langfuse", _langfuse_value(ctx)),
            ("tools", _tools_value(ctx)),
            ("skills", skills_text),
        ]
        label_width = max(len(label) for label, _ in fields)
        for label, value in fields:
            parts.append(_kv_line(label, value, label_width))

        parts.append(Rule(style="dim"))
        parts.append(Text(""))  # blank line below rule
        return Group(*parts)

    # --- normal -------------------------------------------------------------

    def _normal(self, width: int) -> RenderableType:
        ctx = self._ctx
        parts: list[RenderableType] = [
            Text(""),
            Rule(style="dim"),
            _brand_line(ctx, width),
        ]

        fields: list[tuple[str, Text | str]] = [
            ("model", _format_model(ctx.model, 34)),
            ("session", ctx.session_id or "(not set)"),
            ("sink", _sink_value(ctx)),
            ("langfuse", _langfuse_value(ctx)),
            ("tools", _tools_value(ctx)),
        ]
        if ctx.base_url and width >= 85:
            fields.append(("base", _short_url(ctx.base_url, 40)))
        label_width = max(len(label) for label, _ in fields)
        for label, value in fields:
            parts.append(_kv_line(label, value, label_width))

        parts.append(Rule(style="dim"))
        parts.append(Text(""))
        return Group(*parts)

    # --- tiny ---------------------------------------------------------------

    def _tiny(self) -> RenderableType:
        ctx = self._ctx
        model = _format_model(ctx.model, 22)
        line = Text("- ", style="dim")
        line.append(f"■ {ctx.title} v{ctx.version}", style="bold bright_cyan")
        line.append(" ")
        line.append("o", style=ctx.status_color)
        line.append(f" {ctx.status_label} ", style="dim")
        line.append_text(model)

        detail = Text("  Langfuse: ", style="dim")
        detail.append("o", style="green" if ctx.langfuse_enabled else "red")
        detail.append(" on" if ctx.langfuse_enabled else " off", style="dim")

        tools_line = Text("  Tools: ", style="dim")
        tools_line.append_text(_tools_value(ctx))

        return Group(Text(""), line, detail, tools_line)


# ---------------------------------------------------------------------------
# Textual widget
# ---------------------------------------------------------------------------


class HeaderWidget(Static):
    """Thin Textual wrapper around :class:`HeaderView`.

    Re-renders whenever the widget mounts or the terminal is resized so the
    header always matches the available column count.
    """

    def __init__(self, ctx: HeaderContext, *, id: str | None = None, **kwargs: Any) -> None:
        """Bind the :class:`HeaderContext` and forward ``id`` to Static."""
        super().__init__("", id=id, **kwargs)
        self._ctx = ctx

    def _refresh_content(self) -> None:
        width = self.size.width or 80
        self.update(HeaderView(self._ctx).render(width))

    def on_mount(self) -> None:
        """Render the header at the current terminal width after mount."""
        self._refresh_content()

    def on_resize(self, event: events.Resize) -> None:
        """Re-render at the new width when the terminal is resized."""
        self._refresh_content()


__all__ = [
    "HeaderContext",
    "HeaderView",
    "HeaderWidget",
]
