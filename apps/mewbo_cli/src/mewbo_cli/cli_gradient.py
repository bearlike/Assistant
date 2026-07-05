"""Per-character color gradient for Rich Text output.

Reserved for splash screens and dialog titles only — do NOT use for body text
or streaming output (per-character coloring is expensive and causes flicker).

Usage::

    from mewbo_cli.cli_gradient import gradient_text

    title = gradient_text("Mewbo", ["#7c6af7", "#a78bfa", "#c026d3"])
    console.print(title)
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.color import Color
from rich.style import Style
from rich.text import Text


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Parse ``#rrggbb`` or ``#rgb`` → ``(r, g, b)`` in 0-255."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return r, g, b


def _lerp_color(
    stops: list[tuple[int, int, int]],
    t: float,
) -> tuple[int, int, int]:
    """Linear interpolation across multiple color stops.

    Args:
        stops: List of ``(r, g, b)`` tuples; at least one element.
        t: Position in [0, 1].

    Returns:
        Interpolated ``(r, g, b)``.
    """
    if len(stops) == 1:
        return stops[0]
    # Map t into the segment index
    n_segments = len(stops) - 1
    scaled = t * n_segments
    lo = min(int(scaled), n_segments - 1)
    hi = lo + 1
    local_t = scaled - lo
    r = round(stops[lo][0] + (stops[hi][0] - stops[lo][0]) * local_t)
    g = round(stops[lo][1] + (stops[hi][1] - stops[lo][1]) * local_t)
    b = round(stops[lo][2] + (stops[hi][2] - stops[lo][2]) * local_t)
    return r, g, b


def gradient_text(
    text: str,
    colors: Sequence[str],
    *,
    style: str = "",
) -> Text:
    """Return a Rich ``Text`` object with per-character color interpolation.

    Args:
        text: The source string (may be empty or contain unicode).
        colors: Ordered list of hex color stops (≥1).  With a single stop the
            result is solid-colored.
        style: Extra Rich style string applied to every character (e.g. ``"bold"``).

    Returns:
        A :class:`rich.text.Text` with one span per character.

    Note:
        Intended for splash screens and dialog titles only.
    """
    if not text:
        return Text()

    stops = [_hex_to_rgb(c) for c in colors]
    n = len(text)
    rich_text = Text()

    for i, char in enumerate(text):
        # t in [0, 1]; for a single character always 0.0
        t = i / (n - 1) if n > 1 else 0.0
        r, g, b = _lerp_color(stops, t)
        color = Color.from_rgb(r, g, b)
        char_style = Style(color=color)
        if style:
            char_style = char_style + Style.parse(style)
        rich_text.append(char, style=char_style)

    return rich_text
