"""``SearchRun`` — the run-lifecycle façade (create → drive → finalize).

A thin static façade over the store + the active :class:`SearchRunner`,
mirroring ``WikiIndexingJob``. All durable state lives in the store; this class
only orchestrates the transitions and the workspace-history bookkeeping.

The backing-session id is a tag placeholder for the echo runner
(``agentic_search:run:<id>``); the real ``OrchestratedSearchRunner`` resolves a
genuine ``SessionRuntime`` session and patches it onto the record.
"""

from __future__ import annotations

from typing import Any, cast

from mewbo_core.common import get_logger

from . import events
from .catalog import SourceCatalog
from .mcp_config import WorkspaceMcpConfig
from .runner import get_search_runner
from .scg.config import ScgConfig
from .schemas import (
    OUTPUT_CONTRACT_VERSION,
    PastQuery,
    RunPayload,
    RunRecord,
    SearchTierLiteral,
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
        tier: str | None = None,
        model: str | None = None,
        fallback_models: tuple[str, ...] | None = None,
        source_platform: str | None = None,
    ) -> RunPayload | None:
        """Create + launch a run for *query*. Returns None if the workspace is gone.

        Appends a ``running`` history entry up-front so the console can show an
        in-flight query, scopes ``allowed_tools`` from the workspace sources,
        then hands off to the per-run resolved runner. ``tier`` (the budget
        knob) defaults to the configured ``scg`` default and rides the record
        so the runner reads it per run; ``model`` (an explicit per-run
        override) rides the same way and wins over the tier's configured
        model. ``fallback_models`` is an optional per-run cross-model
        ladder; ``None`` inherits the configured fallback policy, exactly like
        ``model``. ``source_platform`` (the originating
        client surface — the route forwards ``request_surface()``) is passed to
        the runner so the orchestrated drive stamps ``surface:<platform>`` on the
        Langfuse trace. A synchronous runner (echo) returns the terminal
        payload and the history entry is patched here; an async runner
        (orchestrated) returns a ``running`` snapshot and its worker patches
        the history entry when it settles.
        """
        workspace = store.get_workspace(workspace_id)
        if workspace is None:
            return None

        run_id = _new_run_id()
        session_id = f"agentic_search:run:{run_id}"
        # Run-grant resolution: the workspace's PERSISTED virtual MCP config
        # is the source of truth for what a run may reach — resolve the grant from
        # its attached server names when one exists, else fall back to the
        # workspace's raw ``sources`` against the live catalog (current behavior).
        grant_sources = (
            WorkspaceMcpConfig.attached_server_names(store, workspace_id)
            or list(workspace.sources)
        )
        allowed_tools = SourceCatalog.tools_for(grant_sources, project)
        now = utc_now_iso()
        run = RunRecord(
            run_id=run_id,
            session_id=session_id,
            workspace_id=workspace_id,
            query=query,
            status="running",
            # The route validated an explicit tier; Pydantic re-validates here
            # (the config default is Literal-typed at its definition).
            tier=cast("SearchTierLiteral", tier or ScgConfig.default_tier()),
            model=model,
            fallback_models=fallback_models,
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
            payload = runner.start(
                run,
                workspace,
                store=store,
                runtime=runtime,
                source_platform=source_platform,
            )
        except Exception as exc:  # pragma: no cover — runner is stubbed in tests
            logging.warning("search run {} failed: {}", run_id, exc)
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
                tier=run.tier,
                model=run.model,
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
        """Cancel a run; best-effort cancel the backing session when real.

        The orchestrated drive runs through ``runtime.start_command`` (the
        ``RunRegistry`` seam), so ``runtime.cancel(session_id)`` reaches a live
        ``RunHandle`` and flips the drive's ``should_cancel``; the worker's
        settle then finds the record already terminal and appends no second
        terminal event.
        """
        record = store.get_run(run_id)
        appended = store.cancel_run(run_id)
        if appended and record is not None:
            store.update_past_query(
                record.workspace_id, run_id, status="cancelled", results=0
            )
            # Real runner sessions are cancellable; echo placeholders are not.
            if runtime is not None and not record.is_echo_backed:
                try:
                    runtime.cancel(record.session_id)
                except Exception as exc:  # pragma: no cover — best-effort
                    logging.warning("runtime.cancel({}) failed: {}", record.session_id, exc)
        return appended

    @staticmethod
    def reconcile_after_recovery(
        run_id: str, *, store: AgenticSearchStoreBase, runtime: Any
    ) -> RunPayload | None:
        """Amend a ``failed`` run whose backing session went on to complete.

        The public entry point for a session-recovery caller (e.g. a
        session-end hook, or a ``/recover`` completion path): a run this class
        settled ``failed`` normally stays wrong forever (``_already_settled``),
        discarding real work when the backing session was later recovered and
        actually finished. This looks up the run and delegates to the active
        runner's own ``reconcile_after_recovery`` when it exposes one — only
        :class:`~mewbo_api.agentic_search.scg.orchestrated_runner.OrchestratedSearchRunner`
        does, since the echo runner has no real backing session to recover.
        Returns the amended payload, or ``None`` when there is nothing to
        amend (unknown run, already non-``failed``, an echo-backed run, or the
        session did not actually reach ``completed``). Never raises — a
        reconciliation attempt must not surface as a caller-visible failure.
        """
        record = store.get_run(run_id)
        if record is None or runtime is None or not record.is_amendable_to_completed():
            return None
        runner = get_search_runner()
        reconcile = getattr(runner, "reconcile_after_recovery", None)
        if reconcile is None:
            return None
        try:
            return reconcile(record, store=store, runtime=runtime)
        except Exception as exc:  # pragma: no cover — best-effort reconciliation
            logging.warning("reconcile_after_recovery({}) failed: {}", run_id, exc)
            return None


__all__ = ["SearchRun"]
