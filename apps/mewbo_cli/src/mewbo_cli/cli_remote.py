#!/usr/bin/env python3
"""CLI local-first remote seam.

The terminal CLI is a strictly-local engine: it runs ``SessionRuntime`` →
``ToolUseLoop`` in-process and stores sessions as local JSONL. This module makes
that locality **user-controlled** without relocating the engine — everything here
is opt-in behind the CLI-only ``cli.remote`` config block and reuses existing
seams (the ``SessionEventBus`` observer, the ``extra_mcp_servers`` registry seam,
the ``TraceProvenance`` context facet). Nothing runs the engine remotely; only
(a) a fire-and-forget transcript mirror and (b) the Mewbo product tools (which
ride the existing Mewbo MCP server) reach the network.

Three units, all pure/atomic:

- :class:`RemoteTranscriptSync` — the ONE atomic mirror class: a bus observer
  (``(session_id, event)`` — the ``on_event`` contract) that fire-and-forget
  POSTs each event to the remote REST API. **Local JSONL stays authoritative.**
- :func:`mewbo_mcp_server_config` — synthesize the Mewbo MCP server entry fed
  into the existing ``_load_mcp_config`` / MCP pool via ``extra_mcp_servers``.
- :func:`sink_label` / :func:`sink_facet` — the honest local-vs-synced strings
  for the header indicator and the provenance facet.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.request
from collections.abc import Callable
from typing import Any

from mewbo_core.common import get_logger

logging = get_logger(name="mewbo.cli.remote")

# Coalesce a burst of appended events (streaming deltas) into one POST rather
# than one request per token. Bounded so a hung remote never grows memory.
_BATCH_MAX = 128
_QUEUE_MAXSIZE = 4096
_POST_TIMEOUT_SECONDS = 5.0

# Type alias for the on_event / bus-observer callback shape.
Poster = Callable[[str, dict[str, Any]], None]


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``$VAR`` references in a config string (never raises)."""
    try:
        return os.path.expandvars(value)
    except Exception:  # noqa: BLE001 — a malformed value degrades to itself
        return value


def sink_facet(*, enabled: bool) -> str:
    """The low-cardinality provenance facet value: ``synced`` vs ``local-only``."""
    return "synced" if enabled else "local-only"


def sink_label(*, enabled: bool, base_url: str) -> str:
    """The honest header/status indicator: ``remote: <base_url>`` vs ``local-only``."""
    return f"remote: {base_url.rstrip('/')}" if enabled else "local-only"


def mewbo_mcp_server_config(base_url: str, token: str) -> dict[str, Any]:
    """Synthesize the Mewbo MCP server entry for the CLI's ``extra_mcp_servers``.

    The Mewbo MCP server speaks streamable-HTTP, is mounted at ``/mcp`` on the
    deployment, and forwards the caller's ``Bearer`` token to the REST API as
    ``X-API-Key`` (token pass-through). Feeding this dict through the existing
    MCP pool surfaces ``ask_wiki`` / ``search`` / ``structured_query`` + the
    wiki-graph read tools in the CLI registry, executing remotely.
    """
    base = base_url.rstrip("/")
    return {
        "transport": "streamable_http",
        "url": f"{base}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }


class RemoteTranscriptSync:
    """Opt-in fire-and-forget mirror of local session events to a remote API.

    Registered ONCE as a :class:`~mewbo_core.session_event_bus.SessionEventBus`
    observer — the CLI's ``on_event`` choke-point (the API bridges the same bus
    to ``HookManager.run_on_event``; the CLI registers sinks on the bus directly,
    the identical ``(session_id, event)`` contract). State: the remote ``base_url``
    + ``token`` + the live active ``session_id`` (via an injected provider so the
    mirror follows session switches). The bus fires on the append hot path, so
    ``__call__`` only enqueues (never blocks, never raises) and a daemon worker
    drains + POSTs. **The local JSONL transcript stays authoritative** — a failed
    POST is logged and dropped, never surfaced.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session_id_provider: Callable[[], str | None],
        poster: Poster | None = None,
        autostart_worker: bool = True,
        timeout: float = _POST_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the remote endpoint + live-session provider.

        ``poster`` is injectable (tests capture calls without a live server); the
        default POSTs JSON via ``urllib``. ``autostart_worker`` gates the daemon
        drainer so tests can drive :meth:`flush` deterministically.
        """
        self._base = base_url.rstrip("/")
        self._token = token
        self._session_id_provider = session_id_provider
        self._poster: Poster = poster or self._http_post
        self._autostart = autostart_worker
        self._timeout = timeout
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- bus observer (the on_event contract) -----------------------------

    def __call__(self, session_id: str, event: dict[str, Any]) -> None:
        """Ingest one appended event; enqueue it for the active session only.

        Best-effort and non-blocking: runs on the publish hot path, so it never
        blocks and never raises into it. Events for a session other than the live
        active one (a background/forked session) are dropped — the CLI mirrors
        only what the user is actively working on.
        """
        try:
            active = self._session_id_provider()
            if active is not None and session_id != active:
                return
            if self._autostart:
                self._ensure_worker()
            self._enqueue((session_id, event))
        except Exception:  # noqa: BLE001 — mirroring must never break the append path
            logging.debug("Remote transcript sync enqueue failed", exc_info=True)

    def _enqueue(self, item: tuple[str, dict[str, Any]]) -> None:
        """Non-blocking put; on overflow drop the oldest (like the event bus)."""
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    # -- worker + drain ---------------------------------------------------

    def _ensure_worker(self) -> None:
        """Lazily start the daemon drainer on first event."""
        if self._worker is not None and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run, name="mewbo-remote-sync", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        """Daemon loop: block for the next event, batch-drain, POST, repeat."""
        while True:
            grouped = self._collect_batch(block=True)
            if grouped:
                self._post_batch(grouped)

    def flush(self) -> None:
        """Synchronously drain + POST everything queued (shutdown / test seam)."""
        while True:
            grouped = self._collect_batch(block=False)
            if not grouped:
                return
            self._post_batch(grouped)

    def _collect_batch(self, *, block: bool) -> dict[str, list[dict[str, Any]]]:
        """Drain up to ``_BATCH_MAX`` queued events, grouped by session id."""
        items: list[tuple[str, dict[str, Any]]] = []
        try:
            if block:
                items.append(self._queue.get())
            while len(items) < _BATCH_MAX:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        grouped: dict[str, list[dict[str, Any]]] = {}
        for session_id, event in items:
            grouped.setdefault(session_id, []).append(event)
        return grouped

    def _post_batch(self, grouped: dict[str, list[dict[str, Any]]]) -> None:
        """POST one request per session — failure isolated, local stays truth."""
        for session_id, records in grouped.items():
            url = f"{self._base}/api/sessions/{session_id}/events"
            try:
                self._poster(url, {"records": records})
            except Exception:  # noqa: BLE001 — best-effort mirror; JSONL is authoritative
                logging.warning("Remote transcript sync POST failed: {}", url, exc_info=True)

    def _http_post(self, url: str, payload: dict[str, Any]) -> None:
        """Default poster: POST JSON with the remote API key header."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": self._token},
        )
        urllib.request.urlopen(req, timeout=self._timeout)  # noqa: S310 — operator-configured URL


__all__ = [
    "RemoteTranscriptSync",
    "mewbo_mcp_server_config",
    "sink_label",
    "sink_facet",
    "expand_env",
]
