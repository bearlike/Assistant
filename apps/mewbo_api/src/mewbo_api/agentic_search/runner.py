"""The orchestration seam — the run-execution strategy.

This package owns the *contracts*; the orchestration team owns the *fan-out*.
:class:`SearchRunner` is the swap-seam: given a run + workspace, drive it by
appending normalized search events to the store's event log and return the
snapshot. :class:`EchoSearchRunner` is the dev/default strategy — it replays the
prototype fixtures over the real event log + store, so the whole console↔API
integration works end-to-end with no LLM.

The real ``OrchestratedSearchRunner`` (other team) starts a tool-scoped
``SessionRuntime`` session and translates its transcript events into this same
event protocol (see ``events.py`` builders). Swap it in with
:func:`set_search_runner`; the routes call :func:`get_search_runner` and stay
agnostic to which strategy is wired.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

from . import events, fixtures
from .schemas import (
    OUTPUT_CONTRACT_VERSION,
    AnswerBullet,
    AnswerSynthesis,
    RelatedPerson,
    RunPayload,
    RunRecord,
    SearchResult,
    TraceAgent,
    Workspace,
    utc_now_iso,
)


class SearchRunner(Protocol):
    """Drives a run to (or toward) a terminal state.

    ``start`` MUST append a terminal event (``run_done`` / ``error``) for a
    synchronous runner. An async runner returns a ``running`` snapshot and
    appends events as the backing session progresses.
    """

    def start(
        self,
        run: RunRecord,
        workspace: Workspace,
        *,
        store: Any,
        runtime: Any = None,
    ) -> RunPayload:
        """Execute (or launch) the run; return the current normalized snapshot."""
        ...


# ---------------------------------------------------------------------------
# Echo runner — dev/default; replays fixtures over the real event log.
# ---------------------------------------------------------------------------


def _typewriter_chunks(text: str, *, words_per_chunk: int = 6) -> list[str]:
    """Split *text* into whitespace-preserving chunks for ``answer_delta``."""
    parts = text.split(" ")
    chunks: list[str] = []
    for i in range(0, len(parts), words_per_chunk):
        chunk = " ".join(parts[i : i + words_per_chunk])
        # Re-attach a trailing space except on the last chunk so concatenation
        # on the client reconstructs the original string.
        if i + words_per_chunk < len(parts):
            chunk += " "
        chunks.append(chunk)
    return chunks


class EchoSearchRunner:
    """Synchronous fixtures-backed runner.

    Filters the canned results/trace/answer to the workspace's enabled sources
    (preserving the original mock's per-workspace coherence), appends the full
    normalized event sequence to the store, and returns the completed payload.
    """

    def start(
        self,
        run: RunRecord,
        workspace: Workspace,
        *,
        store: Any,
        runtime: Any = None,
    ) -> RunPayload:
        """Replay fixtures as a real (instant) event stream; return the payload."""
        _ = runtime  # echo runner needs no session/LLM
        enabled = set(workspace.sources)

        results = [
            SearchResult.model_validate(r)
            for r in fixtures.DEMO_RESULTS
            if r["source"] in enabled
        ]
        trace = [
            TraceAgent.model_validate(a)
            for a in fixtures.DEMO_TRACE
            if a["source_id"] in enabled
        ]
        visible_ids = {r.id for r in results}
        answer = self._build_answer(visible_ids, len(results))
        related_people = [
            RelatedPerson.model_validate(p) for p in fixtures.DEMO_RELATED_PEOPLE
        ]

        # -- emit the normalized stream into the run's event log --------------
        store.append_run_event(
            run.run_id,
            events.run_started(
                run_id=run.run_id,
                session_id=run.session_id,
                workspace_id=run.workspace_id,
                query=run.query,
                sources=list(workspace.sources),
            ),
        )
        by_source: dict[str, int] = {}
        for r in results:
            by_source[r.source] = by_source.get(r.source, 0) + 1
        for agent in trace:
            store.append_run_event(
                run.run_id,
                events.agent_start(
                    agent_id=agent.agent_id,
                    source_id=agent.source_id,
                    name=agent.name,
                    slot=agent.slot,
                ),
            )
            for line in agent.lines:
                store.append_run_event(
                    run.run_id, events.agent_line(agent_id=agent.agent_id, line=line)
                )
            count = by_source.get(agent.source_id, 0)
            store.append_run_event(
                run.run_id,
                events.agent_done(
                    agent_id=agent.agent_id,
                    results_count=count,
                    empty=count == 0,
                ),
            )
        for r in results:
            store.append_run_event(run.run_id, events.result(item=r))
        for chunk in _typewriter_chunks(answer.tldr):
            store.append_run_event(run.run_id, events.answer_delta(text=chunk))
        store.append_run_event(run.run_id, events.answer_ready(answer=answer))
        store.append_run_event(
            run.run_id,
            events.run_done(status="completed", total_ms=fixtures.DEMO_TOTAL_MS),
        )

        payload = RunPayload(
            run_id=run.run_id,
            session_id=run.session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status="completed",
            total_ms=fixtures.DEMO_TOTAL_MS,
            answer=answer,
            results=results,
            trace=trace,
            related_questions=list(fixtures.DEMO_RELATED_QUESTIONS),
            related_people=related_people,
        )
        # Persist the terminal snapshot + timing onto the record.
        store.update_run(
            run.run_id,
            status="completed",
            completed_at=utc_now_iso(),
            total_ms=fixtures.DEMO_TOTAL_MS,
            payload=payload,
        )
        return payload

    @staticmethod
    def _build_answer(visible_ids: set[str], results_count: int) -> AnswerSynthesis:
        """Filter the canned answer's bullets to cite only visible results."""
        raw = fixtures.DEMO_ANSWER
        bullets = [
            AnswerBullet(
                text=b["text"], cites=[c for c in b["cites"] if c in visible_ids]
            )
            for b in raw["bullets"]
            if any(c in visible_ids for c in b["cites"])
        ]
        return AnswerSynthesis(
            tldr=raw["tldr"],
            bullets=bullets,
            confidence=raw["confidence"],
            sources_count=results_count,
        )


# ---------------------------------------------------------------------------
# Active-runner registry
# ---------------------------------------------------------------------------

_runner: SearchRunner = EchoSearchRunner()
_runner_lock = threading.Lock()


def get_search_runner() -> SearchRunner:
    """Return the active search runner (echo by default)."""
    with _runner_lock:
        return _runner


def set_search_runner(runner: SearchRunner) -> None:
    """Register the active runner — the orchestration team swaps in the real one."""
    global _runner
    with _runner_lock:
        _runner = runner


# Re-exported so the contract version is importable from the seam module.
__all__ = [
    "OUTPUT_CONTRACT_VERSION",
    "SearchRunner",
    "EchoSearchRunner",
    "get_search_runner",
    "set_search_runner",
]
