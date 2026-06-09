"""Wiki backend — opt-in via ``mewbo-api[wiki]`` extras.

``init_wiki(app, runtime)`` mounts the /v1/wiki/* routes when the extras
are installed and reachable. It returns False silently otherwise so the
API server starts cleanly without the wiki feature.
"""
from __future__ import annotations

from mewbo_core.common import get_logger

logging = get_logger(name="api.wiki")

# Restart-recovery façade. Imported behind a guard so a graph-less
# ``mewbo-api`` install (no ``wiki`` extra) still imports this package cleanly —
# ``recovery`` pulls ``mewbo_graph`` transitively. ``None`` means the feature is
# absent; ``_run_recovery`` then no-ops. Bound at module level so it's a
# patchable attribute (tests monkeypatch ``recover_interrupted``).
try:
    from .recovery import JobRecovery
except ImportError:  # pragma: no cover — graph-less install
    JobRecovery = None  # type: ignore[assignment,misc]


def init_wiki(app, runtime, hook_manager=None) -> bool:
    """Mount /v1/wiki/* on the Flask app. Returns False if wiki extras are absent.

    ``hook_manager`` (the API's shared :class:`HookManager`) is threaded through
    so the QA session-end finalizer can register on ``on_session_end`` AND the
    same instance is handed to ``WikiQaSession.start`` — the wiki-qa hypervisor
    has no terminal tool of its own, so its ``complete`` event + snapshot
    reconciliation ride this hook. ``None`` keeps the legacy (hookless) behaviour.
    """
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

    register(app, runtime, hook_manager=hook_manager)
    # Restart durability: re-drive jobs that were running when the previous
    # process died. Their sessions are gone, but credentials are persisted
    # per-slug, so the existing refresh path rebuilds them from clone.
    _run_recovery(runtime.wiki_store, runtime)
    logging.info("wiki routes mounted at /v1/wiki/*")
    return True


def _run_recovery(store, runtime) -> None:
    """Re-drive interrupted indexing jobs via the refresh path on startup."""
    if JobRecovery is None:  # pragma: no cover — graph-less install
        return
    try:
        JobRecovery.recover_interrupted(store, runtime)
    except Exception as exc:  # pragma: no cover — recovery is best-effort
        logging.warning("wiki: startup recovery failed (%s); skipping", exc)
