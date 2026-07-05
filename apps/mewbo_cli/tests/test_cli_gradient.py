"""Tests for cli_gradient: gradient_text interpolation."""

from __future__ import annotations

from mewbo_cli.cli_gradient import gradient_text
from rich.text import Text


def test_gradient_text_returns_rich_text() -> None:
    result = gradient_text("hello", ["#ff0000", "#0000ff"])
    assert isinstance(result, Text)


def test_gradient_text_length_matches_input() -> None:
    text = "hello world"
    result = gradient_text(text, ["#ff0000", "#00ff00"])
    assert len(result) == len(text)


def test_gradient_text_empty_string() -> None:
    result = gradient_text("", ["#ff0000", "#0000ff"])
    assert isinstance(result, Text)
    assert len(result) == 0


def test_gradient_text_single_char() -> None:
    result = gradient_text("A", ["#ff0000", "#0000ff"])
    assert isinstance(result, Text)
    assert len(result) == 1


def test_gradient_text_single_color_solid() -> None:
    """With one color stop, every character should be colored."""
    result = gradient_text("abc", ["#ff0000"])
    assert isinstance(result, Text)
    assert len(result) == 3
    # All characters should have spans with a color set
    assert len(result._spans) == 3


def test_gradient_text_style_applied() -> None:
    """Extra style parameter is reflected in the result."""
    result = gradient_text("hi", ["#ffffff", "#000000"], style="bold")
    assert isinstance(result, Text)
    assert len(result) == 2


def test_gradient_text_unicode_safe() -> None:
    """Unicode characters should be handled correctly."""
    text = "日本語"
    result = gradient_text(text, ["#ff0000", "#0000ff"])
    assert isinstance(result, Text)
    assert len(result) == len(text)
