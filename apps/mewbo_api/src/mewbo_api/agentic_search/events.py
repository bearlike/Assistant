r"""Normalized search-run SSE stream + event builders.

The run event log (in the store) *is* the normalized search-event stream. This
module turns it into ``text/event-stream`` frames and provides typed builders
so producers (the echo runner today, the real orchestrator's adapter tomorrow)
never hand-assemble event dicts.

Frame format mirrors the wiki stream: ``id: <idx>\nevent: <type>\ndata: <json>\n\n``
with a 2 KB primer + padded heartbeats to defeat proxy buffering. See
``apps/mewbo_api/src/mewbo_api/wiki/events.py`` for the buffer-flush rationale.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from .schemas import (
    TERMINAL_EVENT_TYPES,
    AnswerSynthesis,
    SearchResult,
    TraceLine,
)
from .store import AgenticSearchStoreBase


class EventLoader(Protocol):
    """A log-loader: ``(id, after_idx) -> events``.

    Defaults to the run event log; the map-source SSE route injects
    ``store.load_map_job_events`` so the SAME generator tails the map-job log
    (its log shares the run event-log shape). The keyword ``after_idx`` matches
    both ``load_run_events`` and ``load_map_job_events``.
    """

    def __call__(self, id: str, /, after_idx: int = -1) -> list[dict[str, Any]]:
        """Return events for *id* with idx > *after_idx* (-1 returns all)."""
        ...

# 2 KB padded comment — forces buffering proxies to flush at stream start.
_SSE_PRIMER = ":" + (" " * 2048) + "\n\n"


def _default_max_idle() -> int:
    """600 cycles (~5 min at 0.5 s); test override via MEWBO_AGENTIC_SSE_MAX_IDLE."""
    return int(os.environ.get("MEWBO_AGENTIC_SSE_MAX_IDLE", "600"))


def _default_sleep() -> float:
    """0.5 s between polls; test override via MEWBO_AGENTIC_SSE_SLEEP."""
    return float(os.environ.get("MEWBO_AGENTIC_SSE_SLEEP", "0.5"))


def _heartbeat_frame() -> str:
    """Padded heartbeat (~2 KB) to keep flushing through proxy buffers."""
    return ":" + (" " * 2048) + "\nevent: heartbeat\ndata: {}\n\n"


def _to_sse(ev: dict[str, Any]) -> str:
    r"""Format a raw event dict as ``id:/event:/data:`` SSE frame."""
    ev = dict(ev)
    idx = ev.pop("idx", None)
    ev_type = ev.pop("type", "message")
    head = f"id: {idx}\n" if idx is not None else ""
    return f"{head}event: {ev_type}\ndata: {json.dumps(ev)}\n\n"


@dataclass
class RunSseGenerator:
    r"""One-shot SSE generator for a search run.

    Polls ``store.load_run_events(run_id, after_idx=...)`` until a terminal
    event is observed or the idle threshold is exceeded. For a completed echo
    run every event is already logged, so the stream replays from idx 0 and
    closes immediately — giving the console an instant typewriter replay.
    """

    store: AgenticSearchStoreBase
    run_id: str
    after_idx: int = -1
    max_idle_cycles: int = field(default_factory=_default_max_idle)
    sleep_s: float = field(default_factory=_default_sleep)
    heartbeat_every: int = 40  # 40 * 0.5 s = 20 s heartbeat cadence
    # Defaults to the run event log; the map-source route injects
    # ``store.load_map_job_events`` to tail a map-job log with the same generator.
    load: EventLoader | None = None

    def generate(self) -> Iterator[str]:
        """Yield SSE frames until a terminal event or idle timeout."""
        loader = self.load or self.store.load_run_events
        yield _SSE_PRIMER
        last_idx = self.after_idx
        idle = 0
        terminal_seen = False
        while True:
            events = loader(self.run_id, after_idx=last_idx)
            if events:
                for ev in events:
                    yield _to_sse(ev)
                    last_idx = max(last_idx, ev.get("idx", last_idx + 1))
                    if ev.get("type") in TERMINAL_EVENT_TYPES:
                        terminal_seen = True
                idle = 0
            else:
                idle += 1
            if terminal_seen or idle >= self.max_idle_cycles:
                break
            if idle > 0 and idle % self.heartbeat_every == 0:
                yield _heartbeat_frame()
            time.sleep(self.sleep_s)


# ---------------------------------------------------------------------------
# Event builders — the only place run-event dicts are assembled.
# ---------------------------------------------------------------------------


def run_started(
    *, run_id: str, session_id: str, workspace_id: str, query: str, sources: list[str]
) -> dict[str, Any]:
    """First event — announces the run + the sources fanning out."""
    return {
        "type": "run_started",
        "run_id": run_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "query": query,
        "sources": list(sources),
    }


def agent_start(*, agent_id: str, source_id: str, name: str, slot: int) -> dict[str, Any]:
    """A per-source sub-agent began searching."""
    return {
        "type": "agent_start",
        "agent_id": agent_id,
        "source_id": source_id,
        "name": name,
        "slot": slot,
    }


def agent_line(*, agent_id: str, line: TraceLine) -> dict[str, Any]:
    """A trace line from a running sub-agent."""
    return {"type": "agent_line", "agent_id": agent_id, "line": line.model_dump()}


def agent_done(
    *,
    agent_id: str,
    results_count: int,
    empty: bool = False,
    result: str = "",
    returned_count: int | None = None,
) -> dict[str, Any]:
    """A sub-agent finished; carries its terminal evidence block.

    ``empty`` marks a dead-ended lane (``NO DATA`` / no output) so the console
    styles it distinctly; ``result`` is the probe's compressed evidence block
    (additive — older consumers ignore it) so the lane can show what it found.

    ``results_count`` is the lane's KEPT card count (after cross-emitter dedup);
    ``returned_count`` is how many it RAW-emitted before dedup (additive — the
    console shows the ``returned − kept`` delta as "N filtered" so the trace
    reads how much each tool contributed vs. how much was a duplicate). Defaults
    to ``results_count`` when the caller has no separate raw tally.
    """
    return {
        "type": "agent_done",
        "agent_id": agent_id,
        "results_count": results_count,
        "returned_count": results_count if returned_count is None else returned_count,
        "empty": empty,
        "result": result,
    }


def result(*, item: SearchResult) -> dict[str, Any]:
    """A normalized hit landed."""
    return {"type": "result", "result": item.model_dump()}


def answer_delta(*, text: str) -> dict[str, Any]:
    """A streamed synthesis token (drives the typewriter)."""
    return {"type": "answer_delta", "text": text}


def answer_ready(*, answer: AnswerSynthesis) -> dict[str, Any]:
    """The full cited synthesis block is ready."""
    return {"type": "answer_ready", "answer": answer.model_dump()}


def related_questions(*, questions: list[str]) -> dict[str, Any]:
    """Follow-up suggestions for the right rail (a parallel structured call).

    Emitted once at settle, AFTER ``answer_ready`` and BEFORE the terminal
    ``run_done`` (so the stream still delivers it), so the live view shows the
    same follow-ups the snapshot's ``RunPayload.related_questions`` carries.
    """
    return {"type": "related_questions", "questions": list(questions)}


def run_done(*, status: str, total_ms: int) -> dict[str, Any]:
    """Terminal — the run reached a final state."""
    return {"type": "run_done", "status": status, "total_ms": total_ms}


def error(*, code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    """Terminal — the run failed."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    return {"type": "error", "error": payload}


__all__ = [
    "RunSseGenerator",
    "run_started",
    "agent_start",
    "agent_line",
    "agent_done",
    "result",
    "answer_delta",
    "answer_ready",
    "related_questions",
    "run_done",
    "error",
]
