"""Single source of truth for CLI glyphs and spinner frames.

Replace scattered literal glyphs with ``ICONS.<role>`` references.

Usage::

    from mewbo_cli.cli_icons import ICONS

    glyph = ICONS.agent_state["running"]   # "●"
    frame = ICONS.spinner[tick % len(ICONS.spinner)]
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field


@dataclass(frozen=True)
class _Icons:
    """Frozen namespace of CLI glyphs.

    All fields are either ``str`` or ``tuple[str, ...]``/``dict[str, str]``.
    """

    # General
    check: str = "✓"
    cross: str = "✗"
    error: str = "✗"
    tool_pending: str = "○"
    plan: str = "📋"  # proposed-plan card title
    refine: str = "✎"  # refine-the-plan affordance

    # Status-line glyphs (Nerd Font md — match the user statusLine convention).
    user: str = "\U000f0004"  # nf-md-account (󰀄) — user@host
    folder: str = "\U000f024b"  # nf-md-folder (󰉋) — working dir
    branch: str = "\U000f062c"  # nf-md-source_branch (󰘬) — replaces the weak ⎇
    stash: str = "\U000f03d7"  # nf-md-package_variant (󰏗)
    gauge: str = "\U000f04c5"  # nf-md-gauge (󰓅) — context-window meter

    # Braille spinner frames (clockwise sweep)
    spinner: tuple[str, ...] = (
        "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
    )

    # Agent lifecycle state → glyph (6 states; see apps/mewbo_cli/CLAUDE.md)
    # Wrapped in MappingProxyType so callers cannot mutate the shared singleton.
    agent_state: types.MappingProxyType[str, str] = field(
        default_factory=lambda: types.MappingProxyType({
            "submitted": "⏳",
            "running": "●",
            "completed": "✓",
            "failed": "✗",
            "cancelled": "⊘",
            "rejected": "⊘",
        })
    )


ICONS: _Icons = _Icons()
