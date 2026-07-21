#!/usr/bin/env python3
"""Full-Textual TUI for the Mewbo CLI.

The foundation ships ``MewboApp`` — one ``textual.App`` that replaces
the former Rich-``Live`` / prompt_toolkit / termios mix — plus the four stable
extension *seams* that Wave-2 children mount into without editing shared
wiring. See :mod:`mewbo_cli.tui.seams` for the seam contract.
"""

from __future__ import annotations

from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
    TranscriptItem,
)

__all__ = [
    "InputGateway",
    "MessageRendererRegistry",
    "PermissionGateway",
    "SidebarSlotRegistry",
    "TranscriptItem",
]
