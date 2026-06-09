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

from mewbo_core.session_store import SessionStoreBase, create_session_store

if TYPE_CHECKING:
    from mewbo_graph.wiki.resume import ResumePlan
    from mewbo_graph.wiki.store import WikiStoreBase


# Process-wide core session store, lazily built ONCE — mirrors the wiki store's
# ``get_wiki_store()`` singleton. Under the Mongo driver, constructing a store
# opens a ``MongoClient`` + pings + ensures indexes, so building it per
# retrieval-tool call (the old ``create_session_store()``-on-every-call path)
# leaked a connection pool and cost 5–100ms each — straight against the
# sub-1.5s streaming budget. Caching it here makes ``resolve_workspace_slug`` a
# pure transcript read with zero per-call construction.
_SESSION_STORE: SessionStoreBase | None = None


def get_session_store() -> SessionStoreBase:
    """Return the process-wide core session store, constructing it on first use.

    Config-addressed (same JSON/Mongo backend the API's runtime uses), so a
    fresh instance still reads the same transcripts — the rationale is identical
    to ``get_wiki_store()``. The API never needs to pin this: the singleton
    converges on the same data regardless of who built it first.
    """
    global _SESSION_STORE
    if _SESSION_STORE is None:
        _SESSION_STORE = create_session_store()
    return _SESSION_STORE


def resolve_runtime() -> Any:
    """Return a handle carrying the shared wiki + session stores (down-only seam).

    Replaces the wiki tools' former reach-**up** into
    ``mewbo_api.wiki.routes._runtime``: the stores live in this package and are
    fetched **down** through their process-wide singletons. ``session_store``
    rides the seam so :func:`resolve_workspace_slug` reads the transcript without
    re-creating a store per call (the SideStage latency/leak fix). Each tool
    keeps a module-level ``_resolve_runtime`` alias delegating here, so tests can
    still patch a fake store-bearing runtime per tool without touching this.
    """
    from mewbo_graph.wiki.store import get_wiki_store  # noqa: PLC0415

    return SimpleNamespace(wiki_store=get_wiki_store(), session_store=get_session_store())


@dataclass(frozen=True)
class WikiJobCtx:
    """All the state a wiki indexing tool needs given a session id.

    ``resume_plan`` is ``None`` for a normal (from-scratch) index. On a
    checkpoint-aware *resume* (Gitea #54), it carries the precomputed
    "what's already done" decision so a phase tool can skip an expensive,
    already-completed phase with a one-line guard. It is rebuilt cheaply per
    tool call from the persisted resume sidecar (``store.get_resume_plan``) —
    the graph-counting :meth:`ResumePlan.build` runs ONCE at resume time, never
    per tool call.
    """

    job_id: str
    slug: str
    session_id: str
    clone_dir: Path
    store: WikiStoreBase  # the shared wiki store (same library, imported down)
    resume_plan: ResumePlan | None = None


@dataclass(frozen=True)
class WikiQaCtx:
    """All the state a wiki QA tool needs given a session id.

    ``answer_id`` is ``None`` for a *grounded structured-response* session (the
    ``/v1/structured`` ``workspace`` path): such a session is scoped to a wiki
    slug via the ``structured_workspace`` transcript event but is NOT a
    registered QA answer, so the retrieval tools (which need only ``slug`` +
    ``store``) work while the QA-emit/event tools must guard on ``answer_id``.
    """

    answer_id: str | None
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
    # Cheap per-call rebuild of the resume decision from its persisted sidecar
    # (a tiny dict — no graph re-query). ``None`` for a from-scratch index, so
    # every phase guard short-circuits to "not skipped".
    from mewbo_graph.wiki.resume import ResumePlan  # noqa: PLC0415

    resume_plan = ResumePlan.from_persisted(store.get_resume_plan(job_id))
    return WikiJobCtx(
        job_id=job_id,
        slug=job.slug,
        session_id=session_id,
        clone_dir=_clone_dir_for(job_id),
        store=store,
        resume_plan=resume_plan,
    )


def resolve_workspace_slug(session_id: str, runtime: Any) -> str | None:
    """Return the wiki slug a *structured-response* session is grounded in.

    A ``StructuredResponder`` run (the ``/v1/structured`` ``workspace`` path)
    writes a ``{"structured_workspace": <slug>}`` context event onto the core
    session transcript but is NOT a registered wiki QA answer — so
    :func:`resolve_qa_ctx` alone can't scope its retrieval tools. This recovers
    the slug from the latest such event (last write wins).

    Transcript access is down-only: it prefers a ``session_store`` carried on
    the seam (the production ``resolve_runtime`` puts the process-wide singleton
    there, and a real ``SessionRuntime`` / test double carries its own), and
    otherwise falls back to the same :func:`get_session_store` singleton — never
    a fresh ``create_session_store()`` per call (that re-opened a Mongo client +
    leaked a pool every retrieval call). Returns ``None`` if no workspace was
    scoped (a plain session).
    """
    session_store = getattr(runtime, "session_store", None)
    if session_store is None:
        try:
            session_store = get_session_store()
        except Exception:  # pragma: no cover — no session backend available
            return None
    try:
        events = session_store.load_transcript(session_id)
    except Exception:  # pragma: no cover — unknown session / backend error
        return None
    slug: str | None = None
    for event in events:
        if event.get("type") != "context":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("structured_workspace"):
            slug = str(payload["structured_workspace"])
    return slug


def resolve_qa_ctx(session_id: str, runtime: Any) -> WikiQaCtx | None:
    """Return the WikiQaCtx for *session_id*, or ``None`` if not grounded.

    Two-tier resolution (one resolver, so ``_base._qa_ctx`` stays unchanged):
    a registered QA answer (``find_qa_by_session``) wins; otherwise a
    structured-response session scoped to a workspace
    (:func:`resolve_workspace_slug`) yields a slug-only ctx (``answer_id=None``)
    so the retrieval tools ground in the workspace. ``None`` only when neither
    holds (a plain, unscoped session).
    """
    store = getattr(runtime, "wiki_store", None)
    if store is None:
        return None
    answer_id = store.find_qa_by_session(session_id)
    if answer_id is None:
        # Fallback: a grounded structured-response session (no QA answer, but a
        # workspace slug on the transcript). Retrieval tools use ctx.slug; the
        # QA-emit/event tools must guard ``if ctx.answer_id is None``.
        slug = resolve_workspace_slug(session_id, runtime)
        if slug is None:
            return None
        return WikiQaCtx(
            answer_id=None,
            slug=slug,
            session_id=session_id,
            store=store,
        )
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
    "get_session_store",
    "resolve_runtime",
    "resolve_job_ctx",
    "resolve_qa_ctx",
    "resolve_workspace_slug",
    "resolve_qa_clone_dir",
    "emit_phase",
    "emit_log",
]
