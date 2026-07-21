"""WikiResume — checkpoint-aware recovery of an interrupted indexing job (B).

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
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.types import IndexingJob, WizardSubmission

from .jobs import (
    _fallback_ladder,
    _load_job_submission,
    _render_resume_query,
    _start_graph_only_index,
    _start_indexer_session,
)

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

    @staticmethod
    def _graph_only_submission(
        store: WikiStoreBase, job: IndexingJob
    ) -> WizardSubmission | None:
        """Return the persisted submission iff *job* is graph-only, else ``None``.

        ``graph_only`` is sticky on the submission sidecar (round-trips through
        ``save_job_submission``), so a graph-only job is detected by reconstructing
        its ``WizardSubmission``. A missing / invalid / non-graph-only sidecar
        yields ``None`` → the normal agent resume path runs.
        """
        raw = store.get_job_submission(job.job_id)
        if not raw:
            return None
        try:
            sub = WizardSubmission.model_validate(raw)
        except Exception:
            return None
        return sub if sub.graph_only else None

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

        Reuses the SAME job_id (continuous event log). Computes + persists a
        :class:`ResumePlan`, resets the job to a running state, emits a ``resume``
        marker, and re-drives the indexer with the plan summary injected so the
        agent skips completed work. The re-clone re-authenticates via the clone
        tool's own credential chain (``mewbo_graph.wiki.credentials.resolve_chain``)
        reading the durable per-slug ``CredentialStore`` directly — this method no
        longer restores anything into an ephemeral cache.

        ``restart=True`` (the "Restart from scratch" intent) forces the no-skip
        :class:`ResumePlan` (``for_restart()``) whose guards never short-circuit —
        so the index rebuilds every phase (idempotent upsert overwrites the stale
        graph / pages) while still reusing the same job_id + recorded commit.
        ``False`` (default) is the checkpoint resume that skips already-done phases.

        Raises ``KeyError`` if the job is unknown, ``ValueError`` if it is not
        resumable (already complete / cancelled), ``RuntimeError`` if a resume
        is already in flight for this job, and (uncaught, propagated)
        :class:`~mewbo_graph.wiki.resume.ResumeCountError` when the checkpoint
        decision could not be computed (a store read failed) — refusing to resume
        rather than silently falling back to a full rebuild.
        """
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(f"job {job_id} not found")
        if not cls.is_resumable(job):
            raise ValueError(f"job {job_id} is {job.status} — not resumable")

        # In-flight guard — mirrors WikiQaSession.follow_up's guard (jobs.py:479).
        # Checked BEFORE any mutation below (ResumePlan.build, the recovery-cap
        # reset, the job status reset): a resume dispatched while the previous
        # graph build is still embedding races the SAME job_id-named clone
        # directory checkout — observed when a resume landed while the prior
        # run was still embedding its graph.
        session_id = store.get_job_session(job_id)
        if session_id and runtime.is_running(session_id):
            raise RuntimeError(f"job {job_id} is already running (session {session_id})")

        slug = job.slug

        # CORE INVARIANT: a graph-only (developer-mode) job must NEVER re-enter the
        # LLM/agent path on recovery OR manual resume. ``graph_only`` is sticky on
        # the persisted submission; when set we re-drive the SAME job_id through the
        # deterministic ``GraphOnlyIndexer`` instead of ``_start_indexer_session``.
        # That path is idempotent (re-clone/scan/build/finalize + supersede-stale),
        # so a from-scratch re-drive is correct — no checkpoint-resume machinery
        # needed. Both ``JobRecovery`` (auto) and the manual resume endpoint funnel
        # through here, so this ONE branch covers both.
        gosub = cls._graph_only_submission(store, job)
        if gosub is not None:
            store.update_job(job_id, status="scanning", error=None)
            store.append_job_event(job_id, {
                "type": "log", "level": "info",
                "text": "Resuming graph-only index (deterministic re-drive, no LLM)",
            })
            _start_graph_only_index(store=store, job_id=job_id, submission=gosub)
            logging.info("wiki resume (graph-only) job={} slug={}", job_id, slug)
            return {"job_id": job_id, "session_id": "", "status": "scanning"}

        # Compute the checkpoint decision ONCE (graph count + plan + pages) and
        # persist it so the per-tool-call ctx rebuild is a cheap dict read.
        # ``restart`` uses the dedicated ``for_restart()`` constructor — structurally
        # identical to the no-skip default, but it records the REBUILD intent so
        # ``summary()`` renders "discard everything" rather than "resume, reuse
        # completed work" (a bare ``ResumePlan()`` carries ``restart=False`` and
        # would render the WRONG instruction for a deliberate restart).
        #
        # ``ResumePlan.build`` fails CLOSED: a store read it depends on raising
        # ``ResumeCountError`` propagates OUT of this method uninterrupted — this
        # method must never catch it and fall back to an empty/full-rebuild plan,
        # since that IS the expensive mistake failing closed exists to prevent.
        plan = ResumePlan.for_restart() if restart else ResumePlan.build(store, job)
        store.save_resume_plan(job_id, plan.to_persisted())

        # User-initiated resume is exempt from / resets the per-slug auto-recovery
        # cap — a human asking to retry should not be blocked by prior auto-retries.
        if user_initiated:
            try:
                store.reset_recovery_attempts(slug)
            except Exception as exc:  # pragma: no cover — best-effort
                logging.warning("wiki resume: reset recovery cap for {} failed: {}", slug, exc)

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
        # The reconstructed submission sidecar is the same one _render_resume_query
        # just read — reading it again here (rather than threading it through) keeps
        # this call symmetric with WikiIndexingJob.start's, which also derives the
        # ladder from its own submission at its own call site.
        submission = _load_job_submission(store, job_id)
        session_id = _start_indexer_session(
            store=store,
            runtime=runtime,
            job_id=job_id,
            model=job.model or "",
            user_query=user_query,
            hook_manager=hook_manager,
            fallback_models=_fallback_ladder(submission),
        )
        logging.info(
            "wiki resume job={} slug={} skip={} remaining={}",
            job_id, slug, sorted(plan.skip), len(plan.pages_remaining),
        )
        return {"job_id": job_id, "session_id": session_id, "status": "scanning"}


__all__ = ["WikiResume"]
