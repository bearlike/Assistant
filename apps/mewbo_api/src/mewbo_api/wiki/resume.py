"""WikiResume — checkpoint-aware recovery of an interrupted indexing job (#54, B).

Unlike ``WikiIndexingJob.refresh`` (a full rebuild that mints a NEW job_id and
clones the latest HEAD), :class:`WikiResume` RE-USES the same job_id, re-clones at
the job's recorded ``commit_sha`` (so the reused graph stays consistent), and skips
the expensive idempotent phases whose store artifacts already exist (graph / enrich
/ plan) — only the remaining pages + finalize are (re)done. It RE-DRIVES the
existing ``wiki-indexer`` AgentDef via the shared ``_start_indexer_session`` seam;
it does NOT add a parallel control loop.

The "what's already done" decision is computed once here (``ResumePlan.build``) and
persisted on the job's resume sidecar so the phase tools' one-line skip guards read
it cheaply per call (see ``mewbo_graph.wiki.resume`` + ``plugins/wiki/_ctx``).
"""
from __future__ import annotations

from typing import Any

from mewbo_core.common import get_logger
from mewbo_graph.wiki.credentials import CredentialStore
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.tokens import CloneTokenCache
from mewbo_graph.wiki.types import IndexingJob

from .jobs import _render_resume_query, _start_indexer_session

logging = get_logger(name="api.wiki.resume")

# Terminal statuses a job must NOT be in to be resumable. ``complete`` is done;
# ``cancelled`` was a deliberate user stop. Everything else (failed / interrupted
# / queued / scanning / finalizing) is a candidate — :class:`ResumePlan` decides
# how much can actually be reused.
_NON_RESUMABLE: frozenset[str] = frozenset({"complete", "cancelled"})


class WikiResume:
    """Static façade — resume an interrupted index from its checkpoints."""

    @staticmethod
    def is_resumable(job: IndexingJob) -> bool:
        """True when *job* is a checkpoint-resume candidate (non-terminal-success)."""
        return job.status not in _NON_RESUMABLE

    @classmethod
    def resume(
        cls,
        store: WikiStoreBase,
        runtime: Any,
        job_id: str,
        *,
        hook_manager: Any = None,
        user_initiated: bool = True,
        restart: bool = False,
    ) -> dict[str, str]:
        """Resume the interrupted index *job_id*; return ``{job_id, session_id, status}``.

        Reuses the SAME job_id (continuous event log). Restores the durable
        credential, computes + persists a :class:`ResumePlan`, resets the job to a
        running state, emits a ``resume`` marker, and re-drives the indexer with the
        plan summary injected so the agent skips completed work.

        ``restart=True`` (the "Restart from scratch" intent) forces a NO-SKIP plan —
        the empty :class:`ResumePlan` whose guards never short-circuit — so the
        index rebuilds every phase (idempotent upsert overwrites the stale graph /
        pages) while still reusing the same job_id + recorded commit. ``False``
        (default) is the checkpoint resume that skips already-done phases.

        Raises ``KeyError`` if the job is unknown, ``ValueError`` if it is not
        resumable (already complete / cancelled).
        """
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(f"job {job_id} not found")
        if not cls.is_resumable(job):
            raise ValueError(f"job {job_id} is {job.status} — not resumable")

        slug = job.slug

        # Restore the durable per-slug credential into the ephemeral cache so the
        # re-clone authenticates (the process that warmed CloneTokenCache is gone).
        cred = CredentialStore.load(store, slug)
        if cred is not None and cred.kind == "token":
            CloneTokenCache.store(job_id, cred.value)

        # Compute the checkpoint decision ONCE (graph count + plan + pages) and
        # persist it so the per-tool-call ctx rebuild is a cheap dict read. A
        # ``restart`` forces the empty no-skip plan → every phase rebuilds.
        plan = ResumePlan() if restart else ResumePlan.build(store, job)
        store.save_resume_plan(job_id, plan.to_persisted())

        # User-initiated resume is exempt from / resets the per-slug auto-recovery
        # cap — a human asking to retry should not be blocked by prior auto-retries.
        if user_initiated:
            try:
                store.reset_recovery_attempts(slug)
            except Exception as exc:  # pragma: no cover — best-effort
                logging.warning("wiki resume: reset recovery cap for %s failed: %s", slug, exc)

        # Reset the job to a running state (clear any prior error) + emit a
        # resume marker on its log.
        store.update_job(job_id, status="scanning", error=None)
        store.append_job_event(job_id, {
            "type": "log",
            "level": "info",
            "text": (
                f"Resuming index (skip={sorted(plan.skip)}, "
                f"pages_done={len(plan.pages_done)}, "
                f"pages_remaining={len(plan.pages_remaining)})"
            ),
        })

        user_query = _render_resume_query(store, job, plan)
        session_id = _start_indexer_session(
            store=store,
            runtime=runtime,
            job_id=job_id,
            model=job.model or "",
            user_query=user_query,
            hook_manager=hook_manager,
        )
        logging.info(
            "wiki resume job=%s slug=%s skip=%s remaining=%d",
            job_id, slug, sorted(plan.skip), len(plan.pages_remaining),
        )
        return {"job_id": job_id, "session_id": session_id, "status": "scanning"}


__all__ = ["WikiResume"]
