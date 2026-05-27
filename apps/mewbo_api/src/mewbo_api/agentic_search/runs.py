"""``SearchRun`` — the run-lifecycle façade (create → drive → finalize).

A thin static façade over the store + the active :class:`SearchRunner`,
mirroring ``WikiIndexingJob``. All durable state lives in the store; this class
only orchestrates the transitions and the workspace-history bookkeeping.

The backing-session id is a tag placeholder for the echo runner
(``agentic_search:run:<id>``); the real ``OrchestratedSearchRunner`` resolves a
genuine ``SessionRuntime`` session and patches it onto the record.
"""

from __future__ import annotations

from typing import Any

from mewbo_core.common import get_logger

from . import events
from .catalog import SourceCatalog
from .runner import get_search_runner
from .schemas import (
    OUTPUT_CONTRACT_VERSION,
    PastQuery,
    RunPayload,
    RunRecord,
    utc_now_iso,
)
from .store import AgenticSearchStoreBase, _new_run_id

logging = get_logger(name="api.agentic_search.runs")


class SearchRun:
    """Static façade — run state lives in the store."""

    @staticmethod
    def start(
        *,
        workspace_id: str,
        query: str,
        store: AgenticSearchStoreBase,
        runtime: Any = None,
        project: str | None = None,
    ) -> RunPayload | None:
        """Create + drive a run for *query*. Returns None if the workspace is gone.

        Appends a ``running`` history entry up-front so the console can show an
        in-flight query, scopes ``allowed_tools`` from the workspace sources,
        then hands off to the active runner. On completion the history entry is
        patched with the final status + result count.
        """
        workspace = store.get_workspace(workspace_id)
        if workspace is None:
            return None

        run_id = _new_run_id()
        session_id = f"agentic_search:run:{run_id}"
        allowed_tools = SourceCatalog.tools_for(workspace.sources, project)
        now = utc_now_iso()
        run = RunRecord(
            run_id=run_id,
            session_id=session_id,
            workspace_id=workspace_id,
            query=query,
            status="running",
            created_at=now,
            started_at=now,
            source_ids=list(workspace.sources),
            allowed_tools=allowed_tools,
            output_contract_version=OUTPUT_CONTRACT_VERSION,
        )
        store.create_run(run)

        # History entry up-front (status=running) so the FE can render it live.
        store.append_past_query(
            workspace_id,
            PastQuery(
                q=query,
                when="just now",
                results=0,
                ran_at=now,
                run_id=run_id,
                status="running",
            ),
        )

        runner = get_search_runner()
        try:
            payload = runner.start(run, workspace, store=store, runtime=runtime)
        except Exception as exc:  # pragma: no cover — runner is stubbed in tests
            logging.warning("search run %s failed: %s", run_id, exc)
            store.append_run_event(
                run_id, events.error(code="internal", message=str(exc))
            )
            store.update_run(
                run_id, status="failed", completed_at=utc_now_iso(), error=str(exc)
            )
            store.update_past_query(
                workspace_id, run_id, status="failed", results=0
            )
            return RunPayload(
                run_id=run_id,
                session_id=session_id,
                query=query,
                workspace_id=workspace_id,
                status="failed",
                error=str(exc),
            )

        store.update_past_query(
            workspace_id,
            run_id,
            status=payload.status,
            results=len(payload.results),
        )
        return payload

    @staticmethod
    def get(run_id: str, *, store: AgenticSearchStoreBase) -> RunRecord | None:
        """Return the run record (with its accumulated payload), or None."""
        return store.get_run(run_id)

    @staticmethod
    def cancel(
        run_id: str, *, store: AgenticSearchStoreBase, runtime: Any = None
    ) -> bool:
        """Cancel a run; best-effort cancel the backing session when real."""
        record = store.get_run(run_id)
        appended = store.cancel_run(run_id)
        if appended and record is not None:
            store.update_past_query(
                record.workspace_id, run_id, status="cancelled", results=0
            )
            # Real runner sessions are cancellable; echo placeholders are not.
            if runtime is not None and not record.session_id.startswith("agentic_search:"):
                try:
                    runtime.cancel(record.session_id)
                except Exception as exc:  # pragma: no cover — best-effort
                    logging.warning("runtime.cancel(%s) failed: %s", record.session_id, exc)
        return appended


__all__ = ["SearchRun"]
