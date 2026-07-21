#!/usr/bin/env python3
"""Set the terminal window/tab title via an OSC escape sequence.

``OSC 2 ; <text> BEL`` sets the window title on every mainstream terminal
emulator. Textual owns the alternate screen, so we write the raw sequence
straight to the controlling TTY (stdout) — Textual never strips or buffers an
OSC the way it manages SGR/cursor control, and emitting it out-of-band keeps the
App's render pipeline untouched.

The single public entry point :func:`set_terminal_title` is best-effort: a
non-TTY stdout (pipe / CI) or any write error is swallowed, so a status surface
that wants a window title never has to guard the call itself.
"""

from __future__ import annotations

import sys
from typing import IO

# OSC 2 (set window title) … string terminator BEL. Kept as constants so the
# escape bytes live in exactly one place.
_OSC = "\033]2;"
_BEL = "\007"

#: Window titles longer than this are truncated with an ellipsis — terminal
#: tabs are narrow and an over-long title just gets clipped by the emulator.
_MAX_TITLE = 80


def _sanitize(text: str) -> str:
    """Strip control characters and collapse whitespace for a one-line title.

    The OSC string is terminated by control bytes, so any control character in
    ``text`` (newline, BEL, ESC) would corrupt the sequence. We drop them and
    squeeze runs of whitespace to single spaces, then bound the length.
    """
    cleaned = "".join(ch for ch in text if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_TITLE:
        cleaned = cleaned[: _MAX_TITLE - 1].rstrip() + "…"
    return cleaned


def set_terminal_title(text: str, *, stream: IO[str] | None = None) -> bool:
    """Set the terminal title to ``text`` via ``OSC 2``; return whether it wrote.

    Best-effort and never raises: a non-TTY stream, an empty title, or any write
    error returns ``False`` instead of propagating. ``stream`` defaults to
    ``sys.stdout`` (injectable for tests).
    """
    out = stream if stream is not None else sys.stdout
    title = _sanitize(text)
    if not title:
        return False
    try:
        if not out.isatty():
            return False
    except Exception:
        # A stream without a usable ``isatty`` (e.g. a StringIO under test) is
        # treated as writable so tests can assert the emitted sequence.
        pass
    try:
        out.write(f"{_OSC}{title}{_BEL}")
        out.flush()
    except Exception:
        return False
    return True


__all__ = ["set_terminal_title"]
