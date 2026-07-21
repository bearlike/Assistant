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
    re-creating a store per call (a real latency/leak fix from a past incident). Each tool
    keeps a module-level ``_resolve_runtime`` alias delegating here, so tests can
    still patch a fake store-bearing runtime per tool without touching this.
    """
    from mewbo_graph.wiki.store import get_wiki_store  # noqa: PLC0415

    return SimpleNamespace(wiki_store=get_wiki_store(), session_store=get_session_store())


@dataclass(frozen=True)
class WikiJobCtx:
    """All the state a wiki indexing tool needs given a session id.

    ``resume_plan`` is ``None`` for a normal (from-scratch) index. On a
    checkpoint-aware *resume*, it carries the precomputed
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
    # The commit this job indexed, resolved once from the job record. Every
    # phase writer stamps its artifacts with it so a completed re-index can
    # supersede the prior commit's; ``None`` for a job whose clone never
    # resolved a sha (the artifacts are then written commit-less).
    commit_sha: str | None = None


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


@dataclass(frozen=True)
class SessionProject:
    """The repo-identifying facts an ORDINARY session carries about its project.

    Grounding a plain task session in a wiki needs a repo identity, and neither a
    config project (name + path) nor a managed worktree records a repo URL
    anywhere a library below the app can read — the app derives one by shelling
    out to ``git remote``, which is an app-layer concern this package must not
    reach up into. So identity is recovered from the session's own two durable
    signals instead, exactly as :class:`~mewbo_core.session_provenance.SessionOrigin`
    recovers origin: the transcript's context events and the session's tags.

    ``owner_repo`` comes from a ``vcs:<owner/repo>:<kind>:<n>`` pickup tag and is
    the precise key; ``repo`` is a bare project name and is the weak one. Both
    are match keys against the wiki store's own slugs — the store is the
    authority on what "indexed" means, so nothing here has to guess a host.
    """

    owner_repo: str | None = None
    repo: str | None = None

    @staticmethod
    def session_store_of(runtime: Any) -> SessionStoreBase | None:
        """Return the transcript store carried on *runtime*, else the singleton.

        Never ``create_session_store()`` per call — that re-opened a Mongo client
        and leaked a pool on every retrieval call. ``None`` when no session
        backend is available at all.
        """
        store = getattr(runtime, "session_store", None)
        if store is not None:
            return store
        try:
            return get_session_store()
        except Exception:  # pragma: no cover — no session backend available
            return None

    @classmethod
    def for_session(cls, session_id: str, runtime: Any) -> SessionProject:
        """Read *session_id*'s project facts off its transcript + tags.

        The store arrives through *runtime* (the down-only seam) rather than
        being reached for, mirroring ``ResumePlan.build(store, job)``. Every read
        is best-effort: an unknown session or a dead backend yields an empty
        instance, never a raise into a tool call.
        """
        store = cls.session_store_of(runtime)
        if store is None:
            return cls()
        repo: str | None = None
        try:
            events = store.load_transcript(session_id)
        except Exception:  # pragma: no cover — unknown session / backend error
            events = []
        for event in events:
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            # ``repo`` (a managed worktree's PARENT project name) is preferred
            # over ``project``, which for such a session is an opaque
            # ``managed:<uuid>`` that names no repository. Last write wins, the
            # same reducer ``resolve_workspace_slug`` uses.
            candidate = payload.get("repo") or payload.get("project")
            if isinstance(candidate, str) and candidate.strip():
                cleaned = candidate.strip()
                if not cleaned.startswith("managed:"):
                    repo = cleaned
        owner_repo: str | None = None
        try:
            tags = store.tags_for_session(session_id)
        except Exception:  # pragma: no cover — backend error
            tags = []
        for tag in tags:
            parts = tag.split(":")
            if len(parts) >= 2 and parts[0] == "vcs" and "/" in parts[1]:
                owner_repo = parts[1]
        return cls(owner_repo=owner_repo, repo=repo)

    @property
    def label(self) -> str:
        """A human label for this project, for an operator-facing message."""
        return self.owner_repo or self.repo or "this session"

    def resolve_slug(self, wiki_store: Any) -> str | None:
        """Return the indexed wiki slug this project matches, or ``None``.

        A wiki slug is ``host/owner/repo``, so a candidate is matched against the
        slug's TAIL — two segments for the precise ``owner_repo`` key, one for
        the bare ``repo`` name — and the precise key is tried first.

        **An ambiguous match resolves to nothing**, never to an arbitrary winner:
        the same repo name legitimately appears under two owners or two hosts,
        and answering a question from the wrong repository's wiki is worse than
        answering it from none (the rule the scip resolver already follows for
        ambiguous descriptors).
        """
        try:
            slugs = [p.slug for p in wiki_store.list_projects()]
        except Exception:  # pragma: no cover — store unavailable
            return None
        for key, depth in ((self.owner_repo, 2), (self.repo, 1)):
            if not key:
                continue
            wanted = key.strip("/").lower()
            hits = {s for s in slugs if self._tail(s, depth) == wanted}
            if len(hits) == 1:
                return hits.pop()
            if hits:
                return None
        return None

    @staticmethod
    def _tail(slug: str, depth: int) -> str:
        """Return *slug*'s last *depth* segments, lowercased."""
        segments = [s for s in slug.lower().split("/") if s]
        return "/".join(segments[-depth:])


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
        commit_sha=job.commit_sha,
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
    session_store = SessionProject.session_store_of(runtime)
    if session_store is None:
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

    Three-tier resolution, strongest binding first (one resolver, so
    ``_base._qa_ctx`` stays unchanged):

    1. a registered QA answer (``find_qa_by_session``);
    2. a structured-response session scoped to a workspace
       (:func:`resolve_workspace_slug`);
    3. an ORDINARY task session whose own project is an indexed wiki
       (:meth:`SessionProject.resolve_slug`).

    Tiers 2 and 3 both yield a slug-only ctx (``answer_id=None``) so the
    read/navigate tools ground while the QA-emit/event tools keep guarding on
    ``answer_id`` — tier 3 is deliberately shape-identical to tier 2, so every
    tool already safe under one is safe under the other. ``None`` now means only
    that the session's project has no wiki indexed (or it has no project at all),
    which is an EXPECTED state rather than a fault — see
    ``WikiSessionTool._ungrounded_result``.
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
            # Last: an ordinary task session working IN a repo that happens to
            # have a wiki. Nothing scopes it, so its own project is the binding.
            slug = SessionProject.for_session(session_id, runtime).resolve_slug(store)
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


def emit_phase_once(ctx: WikiJobCtx, name: str) -> None:
    """Advance to phase *name* only if the job is not already in it.

    The seam for a phase whose work is done by a FAN-OUT rather than by one
    boundary tool: every worker calls this, the first one moves the job, the rest
    are no-ops — without the guard the event log would carry one ``phase`` event
    per worker.

    It replaces the alternative that was tried first: stamping the phase from the
    tail of the PRECEDING tool. That marks a phase as started at the moment its
    predecessor ENDED, so a run that died in the gap reported a phase whose work
    never began at all (a job sat at ``enrich`` after being cancelled 43ms after
    the graph build returned). A phase must be stamped by work that has actually
    started.
    """
    try:
        job = ctx.store.get_job(ctx.job_id)
    except Exception:
        job = None
    if job is not None and getattr(job, "phase", None) == name:
        return
    emit_phase(ctx, name)


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
    "SessionProject",
    "WikiJobCtx",
    "WikiQaCtx",
    "get_session_store",
    "resolve_runtime",
    "resolve_job_ctx",
    "resolve_qa_ctx",
    "resolve_workspace_slug",
    "resolve_qa_clone_dir",
    "emit_phase",
    "emit_phase_once",
    "emit_log",
]
