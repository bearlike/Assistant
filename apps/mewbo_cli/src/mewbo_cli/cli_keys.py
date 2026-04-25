#!/usr/bin/env python3
"""Lightweight keystroke capture for use during Rich Live rendering.

Provides a context manager that puts stdin into cbreak mode and runs a
daemon reader thread, dispatching single-key presses to registered
callbacks.  Uses only stdlib modules (``os``, ``select``, ``sys``,
``termios``, ``threading``, ``tty``).

The :meth:`pause` / :meth:`resume` methods temporarily restore canonical
(line-buffered) mode so that ``console.input()`` — used by the approval
dialog — can read full lines.

No-ops silently when stdin is not a TTY.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import threading
import tty
from collections.abc import Callable
from types import TracebackType
from typing import Any


class KeyListener:
    r"""Capture single keystrokes during Rich Live rendering.

    Usage::

        listener = KeyListener()
        listener.bind("\x0f", on_ctrl_o)     # Ctrl+O

        with listener:
            # cbreak mode active, reader thread running
            run_long_task()
        # terminal restored on exit
    """

    def __init__(self) -> None:
        """Initialise with empty bindings and stopped state."""
        self._bindings: dict[str, Callable[[], None]] = {}
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._resumed = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_settings: list[Any] | None = None

    # ------------------------------------------------------------------
    # Configuration (call before __enter__)
    # ------------------------------------------------------------------

    def bind(self, key: str, callback: Callable[[], None]) -> None:
        """Register *key* → *callback*.  *key* is a single character."""
        self._bindings[key] = callback

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> KeyListener:  # noqa: D105
        if not sys.stdin.isatty():
            return self
        fd = sys.stdin.fileno()
        self._fd = fd
        self._old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._stop.clear()
        self._paused.clear()
        self._resumed.set()  # start in "running" state
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

    def __exit__(  # noqa: D105
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        # Unblock the reader if it is paused.
        self._resumed.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._old_settings is not None and self._fd is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
        self._fd = None
        self._old_settings = None

    # ------------------------------------------------------------------
    # Pause / resume (for approval prompt interaction)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Restore canonical mode so ``console.input()`` can read lines."""
        if self._fd is None or self._old_settings is None:
            return
        self._resumed.clear()
        self._paused.set()
        # Wait for reader to acknowledge pause (≤ select timeout).
        # We don't strictly need to sync, but a short sleep avoids a
        # race where the reader calls os.read after we restore termios.
        threading.Event().wait(0.2)
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)

    def resume(self) -> None:
        """Re-enter cbreak mode after an approval prompt."""
        if self._fd is None:
            return
        tty.setcbreak(self._fd)
        self._paused.clear()
        self._resumed.set()

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader(self) -> None:
        """Daemon loop: poll stdin and dispatch bound keys."""
        assert self._fd is not None
        while not self._stop.is_set():
            # If paused, block until resumed (or stopped).
            if self._paused.is_set():
                self._resumed.wait(timeout=0.5)
                continue

            try:
                rlist, _, _ = select.select([self._fd], [], [], 0.15)
            except (ValueError, OSError):
                # fd closed or invalid — exit cleanly.
                break

            if not rlist:
                continue

            try:
                raw = os.read(self._fd, 1)
            except OSError:
                break

            if not raw:
                break  # EOF

            ch = raw.decode("utf-8", errors="ignore")
            cb = self._bindings.get(ch)
            if cb is not None:
                try:
                    cb()
                except Exception:  # noqa: BLE001 — don't crash reader on bad callback
                    pass


__all__ = ["KeyListener"]
