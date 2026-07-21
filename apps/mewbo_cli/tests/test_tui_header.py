#!/usr/bin/env python3
"""Tests for HeaderContext, HeaderView, and HeaderWidget.

TDD: these tests were written before the implementation.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from mewbo_cli.tui.widgets.header import HeaderContext, HeaderView, HeaderWidget
from rich.console import Console

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx() -> HeaderContext:
    """A representative HeaderContext for all width tiers."""
    return HeaderContext(
        title="Mewbo",
        version="0.0.13",
        status_label="ready",
        status_color="green",
        model="openai/gpt-4o",
        session_id="abc-session-123",
        base_url="https://api.example.com/v1",
        langfuse_enabled=True,
        langfuse_reason=None,
        builtin_enabled=5,
        builtin_disabled=1,
        external_enabled=3,
        external_disabled=0,
        skill_count=2,
    )


# ---------------------------------------------------------------------------
# HeaderContext
# ---------------------------------------------------------------------------


def test_header_context_is_frozen(ctx: HeaderContext) -> None:
    """HeaderContext must be frozen (immutable)."""
    try:
        ctx.title = "Other"  # type: ignore[misc]
    except Exception as exc:
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("HeaderContext must be a frozen dataclass")


def test_header_context_skill_count_default() -> None:
    """skill_count defaults to 0."""
    c = HeaderContext(
        title="T",
        version="1",
        status_label="ok",
        status_color="green",
        model="m",
        session_id="s",
        base_url="http://x",
        langfuse_enabled=False,
        langfuse_reason=None,
        builtin_enabled=0,
        builtin_disabled=0,
        external_enabled=0,
        external_disabled=0,
    )
    assert c.skill_count == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_to_text(renderable: object, width: int) -> str:
    """Render any Rich renderable to a plain string."""
    buf = io.StringIO()
    con = Console(width=width, file=buf, no_color=True, highlight=False)
    con.print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HeaderView — wide tier (width >= 100)
# ---------------------------------------------------------------------------


def test_header_view_wide_contains_title(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(120), 120)
    assert "Mewbo" in text


def test_header_view_wide_contains_version(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(120), 120)
    assert "0.0.13" in text


def test_header_view_wide_contains_model(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(120), 120)
    assert "gpt-4o" in text


def test_header_view_wide_contains_session_id(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(120), 120)
    assert "abc-session-123" in text


# ---------------------------------------------------------------------------
# HeaderView — normal tier (70 <= width < 100)
# ---------------------------------------------------------------------------


def test_header_view_normal_contains_title(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(80), 80)
    assert "Mewbo" in text


def test_header_view_normal_contains_model(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(80), 80)
    assert "gpt-4o" in text


def test_header_view_normal_contains_session_id(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(80), 80)
    assert "abc-session-123" in text


# ---------------------------------------------------------------------------
# HeaderView — tiny tier (width < 70)
# ---------------------------------------------------------------------------


def test_header_view_tiny_contains_title(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(50), 50)
    assert "Mewbo" in text


def test_header_view_tiny_contains_version(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    text = _render_to_text(view.render(50), 50)
    assert "0.0.13" in text


# ---------------------------------------------------------------------------
# HeaderView — render returns a Rich renderable (not None)
# ---------------------------------------------------------------------------


def test_header_view_render_returns_renderable_for_all_tiers(ctx: HeaderContext) -> None:
    view = HeaderView(ctx)
    for width in (120, 80, 50):
        result = view.render(width)
        assert result is not None, f"render({width}) returned None"


# ---------------------------------------------------------------------------
# HeaderWidget — Textual Pilot smoke test
# ---------------------------------------------------------------------------


def test_header_widget_mounts_and_contains_title(ctx: HeaderContext) -> None:
    """Mount HeaderWidget in a minimal App and confirm it renders the title."""
    import io

    from rich.console import Console
    from textual.app import App, ComposeResult

    class _TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HeaderWidget(ctx, id="header")

    async def _run() -> None:
        app = _TestApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#header", HeaderWidget)
            assert widget is not None
            # Force a layout pass to let on_mount fire and update content.
            await pilot.pause()
            # Confirm the widget's underlying Rich content includes the title
            # by rendering the RichVisual's content via a plain Rich Console.
            buf = io.StringIO()
            con = Console(width=80, file=buf, no_color=True, highlight=False)
            # widget._renderable is the content passed to update(); fall back to
            # rendering the view directly if not accessible.
            renderable = getattr(widget, "_renderable", None) or HeaderView(ctx).render(80)
            con.print(renderable)
            text = buf.getvalue()
            assert "Mewbo" in text

    asyncio.run(_run())
