"""Wiki SSE event-stream generator.

Mirrors the polling pattern of ``apps/mewbo_api/src/mewbo_api/backend.py``
``SessionStream`` — reads events from the per-job log, yields new ones,
sleeps briefly, repeats until the job terminates or idle timeout.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from mewbo_graph.wiki.store import WikiStoreBase

_TERMINAL_TYPES = frozenset({"complete", "cancelled", "error"})

# A 2KB padded SSE comment yielded once at stream start. Some HTTP/2
# reverse proxies (notably OpenResty / NPM with default settings) buffer
# small response chunks before flushing to the client — so the first few
# real events never reach the browser until the buffer fills, and on a
# slow-trickle stream they may never flush at all. Yielding a comment
# larger than the proxy's default buffer forces an immediate flush at
# response start, which tells the proxy "this is a streaming response,
# stop buffering". This is the standard SSE-vs-proxy workaround.
_SSE_PRIMER = ":" + (" " * 2048) + "\n\n"


def _default_max_idle() -> int:
    """600 cycles (~5 min at 0.5s sleep); test override via MEWBO_WIKI_SSE_MAX_IDLE."""
    return int(os.environ.get("MEWBO_WIKI_SSE_MAX_IDLE", "600"))


def _default_sleep() -> float:
    """0.5s between polls; test override via MEWBO_WIKI_SSE_SLEEP."""
    return float(os.environ.get("MEWBO_WIKI_SSE_SLEEP", "0.5"))


def _heartbeat_frame() -> str:
    """Heartbeat frame padded to exceed default proxy buffers (~4KB)."""
    return ":" + (" " * 2048) + "\nevent: heartbeat\ndata: {}\n\n"


@dataclass
class _WikiSseGenerator:
    r"""Shared one-shot SSE poll loop for a wiki event log.

    Polls ``_load(after_idx)`` until a terminal event is observed or the idle
    threshold is exceeded, yielding ``event: <type>\ndata: <json>\n\n`` frames
    (with the proxy-flush primer + periodic heartbeats). Subclasses bind the
    concrete per-log poll call.
    """

    store: WikiStoreBase
    after_idx: int = -1
    max_idle_cycles: int = field(default_factory=_default_max_idle)
    sleep_s: float = field(default_factory=_default_sleep)
    heartbeat_every: int = 40           # 40 * 0.5s = 20s heartbeat cadence

    def _load(self, after_idx: int) -> list[dict]:
        """Return events with ``idx > after_idx`` for this generator's log."""
        raise NotImplementedError

    def generate(self) -> Iterator[str]:
        """Yield SSE frames until terminal or idle timeout."""
        # Force proxies to flush the response immediately by emitting a
        # buffer-sized comment frame ahead of any real event. Without this,
        # OpenResty / NPM hold small responses in a 4KB buffer until the
        # client connection closes.
        yield _SSE_PRIMER
        last_idx = self.after_idx
        idle = 0
        terminal_seen = False
        while True:
            events = self._load(last_idx)
            if events:
                for ev in events:
                    yield _to_sse(ev)
                    last_idx = max(last_idx, ev.get("idx", last_idx + 1))
                    if ev.get("type") in _TERMINAL_TYPES:
                        terminal_seen = True
                idle = 0
            else:
                idle += 1
            if terminal_seen:
                break
            if idle >= self.max_idle_cycles:
                break
            if idle > 0 and idle % self.heartbeat_every == 0:
                yield _heartbeat_frame()
            time.sleep(self.sleep_s)


@dataclass
class WikiSseGenerator(_WikiSseGenerator):
    """One-shot SSE generator for a wiki indexing job's event log."""

    job_id: str = ""

    def _load(self, after_idx: int) -> list[dict]:
        """Poll the per-job event log."""
        return self.store.load_job_events(self.job_id, after_idx=after_idx)


@dataclass
class WikiQaSseGenerator(_WikiSseGenerator):
    """One-shot SSE generator for a wiki QA answer's event log.

    ``after_idx=-1`` (default) streams from the very first event, which is the
    ``meta`` event emitted synchronously by ``WikiQaSession.start``.
    """

    answer_id: str = ""

    def _load(self, after_idx: int) -> list[dict]:
        """Poll the per-answer QA event log."""
        return self.store.load_qa_events(self.answer_id, after_idx=after_idx)


def _to_sse(ev: dict) -> str:
    """Format a raw event dict as an SSE frame.

    Emits ``id: <idx>`` so EventSource records it as ``Last-Event-ID`` and
    sends it back on auto-reconnect — lets the route resume from the same
    point instead of replaying from event 0 when a flaky proxy drops the
    connection. ``idx`` itself is stripped from the payload body.
    """
    ev = dict(ev)  # don't mutate caller
    idx = ev.pop("idx", None)
    ev_type = ev.pop("type", "message")
    head = f"id: {idx}\n" if idx is not None else ""
    return f"{head}event: {ev_type}\ndata: {json.dumps(ev)}\n\n"


__all__ = ["WikiSseGenerator", "WikiQaSseGenerator"]
