"""Per-session wiki context resolvers.

Wiki built-in tools are SessionTool instances constructed with just a
``session_id``. They need a way to find the wiki job (or QA answer) the
session is running, plus the clone dir and the submission. This module
provides those lookups via the store's reverse-session index.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase


def resolve_runtime() -> Any:
    """Return a handle carrying the shared wiki store (the down-only seam).

    Replaces the wiki tools' former reach-**up** into
    ``mewbo_api.wiki.routes._runtime``: the store now lives in this package
    and is fetched **down** through its process-wide singleton. Each tool keeps
    a module-level ``_resolve_runtime`` alias delegating here, so tests can
    still patch a fake store-bearing runtime per tool without touching this.
    """
    from mewbo_graph.wiki.store import get_wiki_store  # noqa: PLC0415

    return SimpleNamespace(wiki_store=get_wiki_store())


@dataclass(frozen=True)
class WikiJobCtx:
    """All the state a wiki indexing tool needs given a session id."""

    job_id: str
    slug: str
    session_id: str
    clone_dir: Path
    store: WikiStoreBase  # the shared wiki store (same library, imported down)


@dataclass(frozen=True)
class WikiQaCtx:
    """All the state a wiki QA tool needs given a session id."""

    answer_id: str
    slug: str
    session_id: str
    store: WikiStoreBase  # the shared wiki store (same library, imported down)


_DEFAULT_CLONE_ROOT = "/tmp/mewbo/wiki/clones"


def _clone_dir_for(job_id: str) -> Path:
    """Return the on-disk clone directory for a wiki indexing job.

    Reads ``MEWBO_WIKI_CLONE_ROOT`` from the environment; falls back to
    ``/tmp/mewbo/wiki/clones``. An empty-string env var is treated as unset
    to avoid ``Path("")`` silently resolving to the process CWD.
    """
    root = os.environ.get("MEWBO_WIKI_CLONE_ROOT") or _DEFAULT_CLONE_ROOT
    return Path(root) / job_id


def resolve_job_ctx(session_id: str, runtime: Any) -> WikiJobCtx | None:
    """Return the WikiJobCtx for *session_id*, or ``None`` if not a wiki indexing run."""
    store = getattr(runtime, "wiki_store", None)
    if store is None:
        return None
    job_id = store.find_job_by_session(session_id)
    if job_id is None:
        return None
    job = store.get_job(job_id)
    if job is None:
        return None
    return WikiJobCtx(
        job_id=job_id,
        slug=job.slug,
        session_id=session_id,
        clone_dir=_clone_dir_for(job_id),
        store=store,
    )


def resolve_qa_ctx(session_id: str, runtime: Any) -> WikiQaCtx | None:
    """Return the WikiQaCtx for *session_id* or ``None`` if not a QA run."""
    store = getattr(runtime, "wiki_store", None)
    if store is None:
        return None
    answer_id = store.find_qa_by_session(session_id)
    if answer_id is None:
        return None
    ans = store.get_qa(answer_id)
    if ans is None:
        return None
    # ``QaAnswer.slug`` is backend-internal (exclude=True) — it's populated
    # in-memory when the QA session is started but is not persisted to the
    # store. After a restart the field defaults to "". Callers that need the
    # slug post-restart should store it separately or accept an empty string.
    slug = getattr(ans, "slug", "")
    return WikiQaCtx(
        answer_id=answer_id,
        slug=slug,
        session_id=session_id,
        store=store,
    )


def emit_phase(ctx: WikiJobCtx, name: str) -> None:
    """Append a ``phase`` event AND persist phase + start ts on the job snapshot.

    The event stream drives the live indexing-page progress bar; the
    persisted snapshot drives the landing-page card (which polls the
    snapshot endpoint, never SSE). Both reads are derived from the same
    write here, so the two surfaces can never disagree.
    """
    import datetime as _dt  # noqa: PLC0415

    started_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        ctx.store.append_job_event(ctx.job_id, {"type": "phase", "name": name})
    except Exception:
        pass
    try:
        ctx.store.update_job(ctx.job_id, phase=name, phase_started_at=started_at)
    except Exception:
        pass


def emit_log(ctx: WikiJobCtx, text: str, *, level: str = "info") -> None:
    """Append a free-form ``log`` event for the indexing timeline."""
    try:
        ctx.store.append_job_event(
            ctx.job_id, {"type": "log", "level": level, "text": text}
        )
    except Exception:
        pass


def resolve_qa_clone_dir(slug: str, store: Any) -> Path | None:
    """Return the on-disk clone dir for *slug*'s most-recent completed job.

    Q&A tools that need source-file access (read_file, grep, list_files)
    use this to scope themselves to the right repo snapshot. We pick the
    most-recent ``complete`` job's clone dir, because that is the source
    the wiki was built from — answers stay consistent with the wiki the
    user is reading.

    Returns ``None`` if no completed job exists or the dir is gone (e.g.
    the volume was wiped). Callers should report that to the LLM as
    "source files not available — answer from wiki/graph only".
    """
    try:
        jobs = store.list_jobs(slug=slug)
    except Exception:
        return None
    # ``list_jobs`` already sorts most-recent first in both stores; filter
    # to completed and take the first hit.
    for job in jobs:
        status = getattr(job, "status", None)
        if status != "complete":
            continue
        clone_dir = _clone_dir_for(job.job_id)
        if clone_dir.is_dir():
            return clone_dir
    return None


__all__ = [
    "WikiJobCtx",
    "WikiQaCtx",
    "resolve_runtime",
    "resolve_job_ctx",
    "resolve_qa_ctx",
    "resolve_qa_clone_dir",
    "emit_phase",
    "emit_log",
]
