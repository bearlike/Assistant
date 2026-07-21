#!/usr/bin/env python3
"""Session UX feature package for the Mewbo TUI.

Holds the per-turn workspace checkpointer (:mod:`.rewind`), the cheap
auto-titler (:mod:`.autotitle`) and the post-mount installer (:mod:`.install`)
that wires the global keys (``ctrl+o``/``ctrl+s``/``ctrl+l``) + screens onto the
mounted ``MewboApp`` without editing ``app.py``.
"""

from __future__ import annotations

__all__: list[str] = []
