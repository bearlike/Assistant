"""Wiki backend — opt-in via ``mewbo-api[wiki]`` extras.

``init_wiki(app, runtime)`` mounts the /v1/wiki/* routes when the extras
are installed and reachable. It returns False silently otherwise so the
API server starts cleanly without the wiki feature.
"""
from __future__ import annotations

from mewbo_core.common import get_logger

logging = get_logger(name="api.wiki")


def init_wiki(app, runtime) -> bool:
    """Mount /v1/wiki/* on the Flask app. Returns False if wiki extras are absent."""
    try:
        from mewbo_graph.wiki.store import create_wiki_store, set_wiki_store
    except ImportError as exc:
        logging.info("wiki extras not installed (%s); skipping /v1/wiki/* routes", exc)
        return False
    try:
        store = create_wiki_store()
    except Exception as exc:
        logging.warning("wiki store init failed: %s; skipping routes", exc)
        return False
    # Pin the process-wide singleton so the relocated wiki SessionTools resolve
    # the SAME store instance (down-only) instead of reaching up into the API
    # runtime; keep ``runtime.wiki_store`` for the API routes that read it.
    set_wiki_store(store)
    runtime.wiki_store = store
    from .routes import register

    register(app, runtime)
    # Reap jobs that were running when the previous process died: their
    # sessions are gone, so they can never finish. Without this they'd
    # linger forever in the landing page's "Indexing now" surface.
    _reap_stranded_jobs(runtime.wiki_store)
    logging.info("wiki routes mounted at /v1/wiki/*")
    return True


def _reap_stranded_jobs(store) -> None:
    """Mark non-terminal jobs as failed on startup — their sessions are gone."""
    NON_TERMINAL = ("queued", "scanning", "finalizing")
    try:
        stranded = [j for j in store.list_jobs() if j.status in NON_TERMINAL]
    except Exception as exc:
        logging.warning("wiki: list_jobs failed during reap (%s); skipping", exc)
        return
    if not stranded:
        return
    for job in stranded:
        try:
            store.update_job(job.job_id, status="failed")
            store.append_job_event(job.job_id, {
                "type": "error",
                "error": {
                    "code": "internal",
                    "message": "Indexing session was lost on API restart.",
                },
            })
        except Exception as exc:
            logging.warning("wiki: failed to reap job %s: %s", job.job_id, exc)
    logging.info("wiki: reaped %d stranded job(s) on startup", len(stranded))
