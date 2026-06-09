"""Restart durability for wiki indexing — JobRecovery atomic class.

When the API process dies mid-index, the backing Mewbo session is gone, so the
job can never finish on its own. Recovery now drives the SAME checkpoint-aware
:class:`WikiResume` path the manual resume endpoint uses (Gitea #54, Part B):
re-clone + re-scan, but SKIP the expensive idempotent phases whose store
artifacts already exist (graph / enrich / plan) and write only the remaining
pages. The restored per-slug credential (``CredentialStore``) authenticates the
re-clone, the SAME job_id is reused (continuous event log), and a partially-built
index finishes from its checkpoints instead of rebuilding from scratch.

A small per-slug retry cap (``store.{get,bump}_recovery_attempts`` — its OWN
slug-keyed persistent surface, NOT the submission sidecar) still bounds the
AUTOMATIC path so a job that keeps dying can't loop the API on every restart
(slug-keyed so the cap bounds re-drives across recovery generations). The manual
endpoint bypasses/resets this cap — a human asking to retry gets a fresh budget.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from .resume import WikiResume

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase

logging = get_logger(name="api.wiki.recovery")

# Statuses worth re-driving on restart. ``interrupted`` IS included: if the API
# died after marking a job ``interrupted`` but before its ``refresh`` minted the
# replacement job, the next restart must still retry it — the slug-keyed cap
# prevents any loop.
_RECOVERABLE = ("queued", "scanning", "finalizing", "interrupted")


class JobRecovery:
    """Static façade — re-drive interrupted indexing jobs through refresh."""

    MAX_RETRIES = 3

    @classmethod
    def recover_interrupted(cls, store: WikiStoreBase, runtime: Any) -> list[str]:
        """Re-drive interrupted jobs via refresh; return the slugs re-triggered.

        Idempotent per startup: each distinct slug is refreshed at most once,
        and only while under the slug-keyed retry cap.
        """
        try:
            stranded = [j for j in store.list_jobs() if j.status in _RECOVERABLE]
        except Exception as exc:
            logging.warning("wiki recovery: list_jobs failed (%s); skipping", exc)
            return []
        if not stranded:
            return []

        seen: set[str] = set()
        refreshed: list[str] = []
        for job in stranded:
            # A slug that has exhausted its retry budget must STOP being a
            # zombie. Leaving it non-terminal (``interrupted``) keeps it in the
            # active-jobs surface forever — the "still indexing" ghost that
            # hides a later completed index. Move it to terminal ``failed``
            # instead of re-marking it interrupted or re-driving it.
            if cls._over_cap(store, job.slug):
                logging.warning(
                    "wiki recovery: slug %s over retry cap; marking failed", job.slug
                )
                cls._mark_failed(
                    store,
                    job.job_id,
                    "indexing did not complete after repeated recovery attempts",
                )
                continue
            if job.status != "interrupted":
                try:
                    store.update_job(job.job_id, status="interrupted")
                except Exception as exc:
                    logging.warning("wiki recovery: mark %s interrupted: %s", job.job_id, exc)
            if job.slug in seen:
                continue
            seen.add(job.slug)
            cls._bump_attempts(store, job.slug)
            try:
                # Checkpoint-aware resume reuses the SAME job_id and skips the
                # expensive phases already done. ``user_initiated=False`` keeps the
                # per-slug cap intact for the automatic path (the manual endpoint
                # resets it). Each distinct slug is resumed at most once per startup.
                WikiResume.resume(
                    store, runtime, job.job_id, hook_manager=None, user_initiated=False
                )
                refreshed.append(job.slug)
            except Exception as exc:
                logging.warning("wiki recovery: resume %s failed: %s", job.job_id, exc)
        if refreshed:
            logging.info("wiki recovery: re-triggered %d slug(s)", len(refreshed))
        return refreshed

    @staticmethod
    def _over_cap(store: WikiStoreBase, slug: str) -> bool:
        """True when this slug's recovery attempts have reached MAX_RETRIES."""
        try:
            return store.get_recovery_attempts(slug) >= JobRecovery.MAX_RETRIES
        except Exception:
            return False

    @staticmethod
    def _bump_attempts(store: WikiStoreBase, slug: str) -> None:
        """Increment the slug-keyed recovery-attempt counter (own surface)."""
        try:
            store.bump_recovery_attempts(slug)
        except Exception as exc:  # pragma: no cover — best-effort bookkeeping
            logging.warning("wiki recovery: bump attempts for %s failed: %s", slug, exc)

    @staticmethod
    def _mark_failed(store: WikiStoreBase, job_id: str, message: str) -> None:
        """Move a retry-exhausted job to terminal ``failed`` (no more zombie)."""
        try:
            store.update_job(job_id, status="failed")
            store.append_job_event(job_id, {
                "type": "error",
                "error": {"code": "internal", "message": message},
            })
        except Exception as exc:  # pragma: no cover — best-effort
            logging.warning("wiki recovery: mark %s failed: %s", job_id, exc)


__all__ = ["JobRecovery"]
