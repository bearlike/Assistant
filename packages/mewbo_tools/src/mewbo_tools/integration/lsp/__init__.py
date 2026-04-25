"""Native LSP integration for Mewbo.

Provides a single ``lsp_tool`` that the agent can optionally invoke for
code diagnostics, go-to-definition, find-references, and hover info.
Language servers are spawned lazily and scoped per-session.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from mewbo_tools.integration.lsp.manager import LSPServerManager

try:
    from pygls.lsp.client import BaseLanguageClient  # noqa: F401

    LSP_AVAILABLE = True
except ImportError:
    LSP_AVAILABLE = False

T = TypeVar("T")

# ------------------------------------------------------------------
# Persistent event loop for LSP I/O (background daemon thread)
# ------------------------------------------------------------------

_lsp_loop: asyncio.AbstractEventLoop | None = None
_lsp_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _get_lsp_loop() -> asyncio.AbstractEventLoop:
    """Return the shared LSP event loop, creating it on first call."""
    global _lsp_loop, _lsp_thread
    with _loop_lock:
        if _lsp_loop is None or _lsp_loop.is_closed():
            _lsp_loop = asyncio.new_event_loop()
            _lsp_thread = threading.Thread(
                target=_lsp_loop.run_forever,
                daemon=True,
                name="lsp-event-loop",
            )
            _lsp_thread.start()
    return _lsp_loop


def run_lsp_async(coro: Coroutine[object, object, T], *, timeout: float = 30) -> T:
    """Run an async coroutine on the persistent LSP event loop.

    This bridges the sync tool code to the async pygls client without
    creating/destroying event loops on each call.
    """
    loop = _get_lsp_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


# ------------------------------------------------------------------
# Per-session manager singleton
# ------------------------------------------------------------------

_managers: dict[str, LSPServerManager] = {}


def get_lsp_manager(cwd: str) -> LSPServerManager:
    """Return (or create) the LSP manager for *cwd*."""
    if cwd not in _managers:
        from mewbo_core.config import get_config

        from mewbo_tools.integration.lsp.manager import LSPServerManager

        lsp_cfg = getattr(get_config().agent, "lsp", None)
        _managers[cwd] = LSPServerManager(cwd=cwd, config=lsp_cfg)
    return _managers[cwd]


def get_passive_diagnostics(file_path: str, cwd: str) -> str | None:
    """Return formatted diagnostics for *file_path*, or ``None``.

    Called by the tool-use loop after file edits to provide passive
    feedback to the LLM.  Returns ``None`` if LSP is unavailable or
    no errors/warnings were found.
    """
    if not LSP_AVAILABLE or not _managers:
        return None
    manager = _managers.get(cwd)
    if manager is None:
        return None
    sdef = manager.server_for_file(file_path)
    if sdef is None:
        return None
    # Only proceed if this server is already running (don't start one
    # just for passive feedback — that would slow down edits).
    if sdef.id not in manager._clients:
        return None
    try:
        client = manager._clients[sdef.id]
        # Notify the server about the changed file
        run_lsp_async(manager.open_file(client, file_path))
        # Brief pause for the server to re-analyze
        import time

        time.sleep(1.5)
        diags = manager.get_cached_diagnostics(file_path)
        # Only surface errors and warnings
        from lsprotocol.types import DiagnosticSeverity

        errors = [
            d
            for d in diags
            if d.severity in (DiagnosticSeverity.Error, DiagnosticSeverity.Warning, None)
        ]
        if not errors:
            return None
        # Format concisely
        from mewbo_tools.integration.lsp.tool import LSPTool

        return LSPTool._format_diagnostics(file_path, diags, manager)
    except Exception:
        return None


async def shutdown_lsp_managers() -> None:
    """Shut down all LSP managers.  Called on session end."""
    for mgr in list(_managers.values()):
        await mgr.shutdown_all()
    _managers.clear()
    # Stop the background loop
    global _lsp_loop, _lsp_thread
    with _loop_lock:
        if _lsp_loop is not None and not _lsp_loop.is_closed():
            _lsp_loop.call_soon_threadsafe(_lsp_loop.stop)
            _lsp_loop = None
            _lsp_thread = None
