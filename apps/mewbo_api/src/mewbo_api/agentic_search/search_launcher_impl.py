"""Concrete ``SearchLauncher`` — the self-facing async agentic-search backend.

Bridges the down-only ``mewbo_graph.scg.search_launcher.SearchLauncher`` seam to
THIS app's run lifecycle so a task-spawned engine agent's ``agentic_search``
tool call drives a real ``scg-search`` run — reusing :class:`SearchRun.start`
and the run store — WITHOUT the engine importing up into the app. The exact
counterpart of ``_register_map_phase_sink``: the engine can't reach the run
store/runtime, so the api injects a writer here at startup.

Async-by-handle: :meth:`start` hands off to the (orchestrated) runner, which
returns a ``running`` snapshot promptly; we surface the ``run_id`` immediately
so the calling agent never blocks on a multi-minute session. A run that settled
synchronously (the echo runner) — or an idempotent reuse of a recent completed
run for the same question — is returned fully formed. :meth:`fetch` projects the
durable snapshot (cited answer + ``computed_at``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mewbo_core.common import get_logger

from . import store as store_mod
from .runs import SearchRun
from .schemas import TERMINAL_RUN_STATUSES, RunRecord, Workspace

logging = get_logger(name="api.agentic_search.search_launcher_impl")

# Keep the agent-facing result list compact — a task agent wants the cited
# answer + an index it can cite, not the full console payload.
_MAX_RESULTS = 25


@dataclass(frozen=True)
class RunStoreSearchLauncher:
    """A :class:`SearchLauncher` impl over the api run store + session runtime."""

    runtime: Any = None

    # -- launcher protocol --------------------------------------------------

    def start(
        self, query: str, *, workspace: str | None = None, tier: str | None = None
    ) -> dict[str, object]:
        """Resolve the workspace, idempotently reuse or launch a run, return it."""
        store = store_mod.get_store()
        ws = self._resolve_workspace(store, workspace)

        reuse = self._recent_completed(store, ws.id, query)
        if reuse is not None:
            snap = self._shape(reuse)
            snap["reused"] = True
            return snap

        payload = SearchRun.start(
            workspace_id=ws.id,
            query=query,
            store=store,
            runtime=self.runtime,
            tier=tier,
            source_platform="agent",
        )
        if payload is None:  # pragma: no cover — ws resolved above
            raise ValueError(f"workspace '{ws.id}' is no longer available")

        # A synchronous (echo) or already-terminal run: return the full snapshot
        # so the agent gets its answer in one call. An async run is still
        # ``running`` — hand back the resumable handle.
        if payload.status in TERMINAL_RUN_STATUSES:
            record = store.get_run(payload.run_id)
            if record is not None:
                return self._shape(record)
        return {
            "run_id": payload.run_id,
            "session_id": payload.session_id,
            "workspace_id": ws.id,
            "workspace": ws.name,
            "query": query,
            "tier": payload.tier,
            "status": "processing" if payload.status == "running" else payload.status,
            "note": (
                "The search is running in its own session. Call agentic_search "
                "again with this run_id to retrieve the cited answer when ready."
            ),
        }

    def fetch(self, run_id: str) -> dict[str, object] | None:
        """Return the run's last-known snapshot, or ``None`` if unknown."""
        record = store_mod.get_store().get_run(run_id)
        if record is None:
            return None
        return self._shape(record)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _resolve_workspace(store: Any, ref: str | None) -> Workspace:
        """Resolve *ref* (id or unique name) → a workspace; raise with guidance.

        Mirrors the MCP ``_resolve_workspace`` ergonomics so the self and
        outside surfaces resolve identically: exact id first, then a unique
        case-insensitive name. With no ref, default to the only workspace; raise
        a list-bearing error when zero or several exist so the agent can pick.
        """
        workspaces = store.list_workspaces()
        if not workspaces:
            raise ValueError("no search workspaces are configured")
        if not ref:
            if len(workspaces) == 1:
                return workspaces[0]
            names = sorted(w.name for w in workspaces)
            raise ValueError(
                "several workspaces exist — pass 'workspace' (id or name). "
                f"Available: {names}"
            )
        for ws in workspaces:
            if ws.id == ref:
                return ws
        matches = [w for w in workspaces if w.name.lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0]
        names = sorted(w.name for w in workspaces)
        if not matches:
            raise ValueError(f"no workspace matches '{ref}'. Available: {names}")
        raise ValueError(f"workspace name '{ref}' is ambiguous — use its id. Names: {names}")

    @staticmethod
    def _recent_completed(
        store: Any, workspace_id: str, query: str
    ) -> RunRecord | None:
        """The most recent COMPLETED run for the exact same query, or ``None``.

        Gives the "re-invoke the same query → last-known answer" idempotency:
        an identical question returns its prior cited answer (+ ``computed_at``)
        instead of launching a duplicate session. Exact-match on the query text
        only (a different question, or a not-yet-completed run, launches anew).
        """
        runs = store.list_runs(workspace_id)
        best: RunRecord | None = None
        for run in runs:
            if run.status != "completed" or run.query != query:
                continue
            if best is None or (run.created_at or "") > (best.created_at or ""):
                best = run
        return best

    @classmethod
    def _shape(cls, record: RunRecord) -> dict[str, object]:
        """Project a :class:`RunRecord` into the compact agent-facing snapshot.

        The cited synthesis + a compact result index (so citations resolve) +
        ``computed_at`` (when the answer was calculated) — never the per-source
        trace or decorative fields.
        """
        payload = record.payload
        answer = payload.answer if payload is not None else None
        results = payload.results[:_MAX_RESULTS] if payload is not None else []
        status = "processing" if record.status == "running" else record.status
        out: dict[str, object] = {
            "run_id": record.run_id,
            "session_id": record.session_id,
            "workspace_id": record.workspace_id,
            "query": record.query,
            "tier": record.tier,
            "status": status,
            "total_ms": record.total_ms,
            # When the answer was calculated — None while still processing.
            "computed_at": record.completed_at,
            "results": [
                {
                    "id": r.id,
                    "source": r.source,
                    "kind": r.kind,
                    "title": r.title,
                    "url": r.url,
                    "relevance": r.relevance,
                }
                for r in results
            ],
        }
        if answer is not None:
            out["answer"] = {
                "tldr": answer.tldr,
                "bullets": [
                    {"text": b.text, "cites": list(b.cites)} for b in answer.bullets
                ],
                "confidence": answer.confidence,
                "sources_count": answer.sources_count,
            }
        if payload is not None and payload.related_questions:
            out["related_questions"] = list(payload.related_questions)
        if payload is not None and payload.error:
            out["error"] = payload.error
        return out


__all__ = ["RunStoreSearchLauncher"]
