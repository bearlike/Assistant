"""LSP server manager — lazy startup, per-session lifecycle.

Uses ``pygls.lsp.client.BaseLanguageClient`` for all protocol handling.
We only manage lifecycle and map file extensions to servers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from lsprotocol import types
from meeseeks_core.common import get_logger
from pygls.lsp.client import BaseLanguageClient

from meeseeks_tools.integration.lsp.servers import (
    _EXTENSION_LANGUAGE_MAP,
    ServerDef,
    available_servers,
)

logger = get_logger(__name__)


class LSPServerManager:
    """Per-session language server manager.

    Servers are started lazily on first request for a matching file type.
    All servers are shut down via :meth:`shutdown_all` on session end.
    """

    def __init__(self, cwd: str, config: Any | None = None) -> None:  # noqa: D107
        self._cwd = cwd
        self._clients: dict[str, BaseLanguageClient] = {}
        self._diagnostics: dict[str, list[types.Diagnostic]] = {}  # uri → diags
        self._failed: set[str] = set()  # server IDs that failed to start

        # Build extension map from available servers
        overrides = getattr(config, "servers", {}) if config else {}
        self._servers = available_servers(overrides)
        self._extension_map: dict[str, ServerDef] = {}
        for sdef in self._servers:
            for ext in sdef.extensions:
                self._extension_map.setdefault(ext, sdef)

        if self._servers:
            logger.info(
                "LSP servers available: {}",
                ", ".join(s.id for s in self._servers),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def server_for_file(self, file_path: str) -> ServerDef | None:
        """Return the server definition for *file_path*, or ``None``."""
        ext = Path(file_path).suffix.lower()
        return self._extension_map.get(ext)

    async def ensure_server(self, file_path: str) -> BaseLanguageClient | None:
        """Start the appropriate server for *file_path* if not running.

        Returns ``None`` if no server is available or the server failed to
        start previously.
        """
        sdef = self.server_for_file(file_path)
        if sdef is None or sdef.id in self._failed:
            return None

        if sdef.id in self._clients:
            return self._clients[sdef.id]

        return await self._start_server(sdef)

    def get_cached_diagnostics(self, file_path: str) -> list[types.Diagnostic]:
        """Return diagnostics pushed by the server for *file_path*."""
        uri = Path(file_path).resolve().as_uri()
        return self._diagnostics.get(uri, [])

    async def open_file(self, client: BaseLanguageClient, file_path: str) -> None:
        """Notify the server that a file has been opened."""
        resolved = Path(file_path).resolve()
        uri = resolved.as_uri()
        sdef = self.server_for_file(file_path)
        lang_id = sdef.language_id if sdef else _EXTENSION_LANGUAGE_MAP.get(
            resolved.suffix.lower(), "plaintext"
        )
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        client.text_document_did_open(
            types.DidOpenTextDocumentParams(
                text_document=types.TextDocumentItem(
                    uri=uri,
                    language_id=lang_id,
                    version=1,
                    text=text,
                )
            )
        )

    async def shutdown_all(self) -> None:
        """Shut down all running language servers."""
        for sid, client in list(self._clients.items()):
            try:
                await asyncio.wait_for(client.shutdown_async(None), timeout=5)
                client.exit(None)
                await client.stop()
            except Exception as exc:
                logger.debug("Error shutting down LSP server '{}': {}", sid, exc)
        self._clients.clear()
        self._diagnostics.clear()
        logger.debug("All LSP servers shut down")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _start_server(self, sdef: ServerDef) -> BaseLanguageClient | None:
        """Spawn a language server and perform the LSP initialize handshake."""
        try:
            client = BaseLanguageClient(
                name=f"meeseeks-{sdef.id}",
                version="0.1.0",
            )

            # Register handlers before starting
            @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
            def _on_diagnostics(params: types.PublishDiagnosticsParams) -> None:
                self._diagnostics[params.uri] = list(params.diagnostics)

            @client.feature(types.WINDOW_LOG_MESSAGE)
            def _on_log_message(params: types.LogMessageParams) -> None:
                pass  # Suppress noisy log messages from servers

            await client.start_io(sdef.command[0], *sdef.command[1:])

            # Find workspace root
            root_path = self._find_root(sdef)
            root_uri = Path(root_path).as_uri()

            await client.initialize_async(
                types.InitializeParams(
                    process_id=os.getpid(),
                    root_uri=root_uri,
                    capabilities=types.ClientCapabilities(
                        text_document=types.TextDocumentClientCapabilities(
                            publish_diagnostics=types.PublishDiagnosticsClientCapabilities(),
                            definition=types.DefinitionClientCapabilities(),
                            references=types.ReferenceClientCapabilities(),
                            hover=types.HoverClientCapabilities(),
                        ),
                    ),
                )
            )
            client.initialized(types.InitializedParams())

            self._clients[sdef.id] = client
            logger.info("Started LSP server '{}' (root: {})", sdef.id, root_path)
            return client

        except Exception as exc:
            logger.warning("Failed to start LSP server '{}': {}", sdef.id, exc)
            self._failed.add(sdef.id)
            return None

    def _find_root(self, sdef: ServerDef) -> str:
        """Walk up from CWD to find a directory containing a root marker."""
        current = Path(self._cwd).resolve()
        for _ in range(20):  # max depth
            for marker in sdef.root_markers:
                if (current / marker).exists():
                    return str(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        return self._cwd
