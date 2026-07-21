#!/usr/bin/env python3
"""Cheap LLM session auto-titling for the CLI.

After the first turn we want a short navigable title (it feeds the ``/resume``
switcher and the OSC terminal-title write). The engine ALREADY has a
title seam — :func:`mewbo_core.title_generator.generate_session_title` (a one-shot
3-7 word model call) plus the store's ``save_title``/``load_title`` — so this is
a thin orchestrator over that seam, NOT a new LLM client.

:class:`AutoTitler` is an atomic class (DI on the runtime/store): call
:meth:`maybe_title` after the first turn. It is idempotent (skips when a title
already exists, so it never fights the orchestrator's own background titler) and
non-blocking (runs the model call on a daemon thread); the produced title is
delivered to an optional callback so the App can update the header / terminal
title without this class importing any UI.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from mewbo_core.common import get_logger
from mewbo_core.session_store import SessionStoreBase

logger = get_logger(name="mewbo.cli.autotitle")

TitleCallback = Callable[[str], None]
"""Receives the freshly-produced title (UI-agnostic — the App wires the sink)."""


class AutoTitler:
    """Produce + persist a session title once, off the UI thread (DI).

    Construct with the session store (the title's home) and an optional sink
    callback invoked with the produced title. :meth:`maybe_title` returns
    immediately; the title appears via the store + callback when the background
    model call finishes. Failures are logged, never raised — the
    first-user-message fallback in ``summarize_session`` remains the safety net.
    """

    def __init__(
        self,
        *,
        store: SessionStoreBase,
        on_title: TitleCallback | None = None,
    ) -> None:
        """Bind the session store and the optional title sink."""
        self._store = store
        self._on_title = on_title

    def has_title(self, session_id: str) -> bool:
        """Return whether the session already has a stored title."""
        return self._store.load_title(session_id) is not None

    def maybe_title(self, session_id: str) -> bool:
        """Kick off title generation for ``session_id`` if it has none.

        Returns ``True`` when a generation thread was started, ``False`` when a
        title already exists (idempotent — safe to call after every turn).
        """
        if self.has_title(session_id):
            return False
        threading.Thread(
            target=self._run,
            args=(session_id,),
            name=f"cli-autotitle-{session_id[:8]}",
            daemon=True,
        ).start()
        return True

    def _run(self, session_id: str) -> None:
        """Worker body: generate, persist, and notify (best-effort)."""
        try:
            from mewbo_core.title_generator import generate_session_title

            # Re-check inside the worker: the orchestrator's own background
            # titler may have won the race between maybe_title and here.
            if self._store.load_title(session_id) is not None:
                return
            events = self._store.load_transcript(session_id)
            title = asyncio.run(generate_session_title(events))
            if not title:
                return
            self._store.save_title(session_id, title)
            self._store.append_event(
                session_id, {"type": "title_update", "payload": {"title": title}}
            )
            if self._on_title is not None:
                try:
                    self._on_title(title)
                except Exception as exc:  # noqa: BLE001 - sink must never break titling
                    logger.warning("Title sink failed: {}: {}", type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001 - titling is best-effort
            logger.warning("Auto-title failed: {}: {}", type(exc).__name__, exc)


__all__ = ["AutoTitler", "TitleCallback"]
