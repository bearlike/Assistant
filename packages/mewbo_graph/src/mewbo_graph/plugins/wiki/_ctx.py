"""Per-session wiki context resolvers.

Wiki built-in tools are SessionTool instances constructed with just a
``session_id``. They need a way to find the wiki job (or QA answer) the
session is running, plus the clone dir and the submission. This module
provides those lookups via the store's reverse-session index.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from mewbo_core.contracts.progress import ProgressLedger, StepSpec, StepState
from mewbo_core.session.session_provenance import SessionTag
from mewbo_core.session.session_store import SessionStoreBase, create_session_store

from mewbo_graph.wiki.events import LogJobEvent, ProgressErrorJobEvent
from mewbo_graph.wiki.types import IndexingJob

if TYPE_CHECKING:
    from mewbo_graph.wiki.resume import ResumePlan
    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.types import ScopePreview


# Process-wide core session store, lazily built ONCE — mirrors the wiki store's
# ``get_wiki_store()`` singleton. Under the Mongo driver, constructing a store
# opens a ``MongoClient`` + pings + ensures indexes, so building it per
# retrieval-tool call — a ``create_session_store()`` per call — leaks a
# connection pool and costs 5–100ms each, straight against the sub-1.5s
# streaming budget. Caching it here makes ``resolve_workspace_slug`` a
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

    @property
    def progress(self) -> ProgressReporter:
        """Build a ledger reporter for one tool invocation.

        Bind this once per tool call and pass it down rather than rebuilding one
        inside a loop. Construction reads one job record so a fresh tool instance
        sees the durable ledger history.

        Cost: ``O(one record)``.
        """
        return ProgressReporter(self)

    @property
    def job_bound(self) -> bool:
        """True when this ctx names a real indexing JOB, not just a project.

        The one predicate separating the two shapes :func:`resolve_job_ctx`
        returns. A job-bound ctx carries a job record, a checkout of its own and
        a resume plan; a SLUG-bound one (a maintainer session, which edits an
        already-indexed project on demand) carries the project and nothing else,
        so ``job_id`` is empty.

        Everything keyed on ``job_id`` — the phase/progress writers, the
        submitted-page claim counter, the plan denominator, the job event log —
        belongs to a job and must be skipped rather than written under an empty
        id, which would file records nothing can ever read back. Ask this rather
        than testing ``job_id`` directly, so the rule has one spelling.
        """
        return bool(self.job_id)

    @classmethod
    def for_maintainer(
        cls, session_id: str, runtime: Any, store: WikiStoreBase
    ) -> WikiJobCtx | None:
        """Build the PROJECT-bound ctx for a maintainer session, else ``None``.

        Two conditions, both required. The session must carry the maintainer TAG
        (:meth:`SessionProject.maintainer_slug` — the authorization, and where
        the slug comes from), and that slug must name a project row that really
        exists. The row is what makes the binding real: a slug naming nothing
        indexed resolves to ``None`` rather than a ctx whose reads return
        nothing and whose writes land under a key no reader knows, so the tool
        reports "no wiki here" — a state an agent can learn from in one call.

        Collaborators arrive as ARGUMENTS rather than being reached for, the
        same shape ``ResumePlan.build(store, job)`` has, so the precedence is
        testable against fakes with no I/O.
        """
        slug = SessionProject.maintainer_slug(session_id, runtime)
        if not slug:
            return None
        try:
            project = store.get_project(slug)
        except Exception:  # pragma: no cover — store unavailable
            return None
        if project is None:
            return None
        return cls(
            job_id="",
            slug=slug,
            session_id=session_id,
            # The most recent completed index's checkout — what the source tools
            # resolve for themselves anyway — or a path that provably does not
            # exist when none survives.
            clone_dir=(
                resolve_qa_clone_dir(slug, store) or _clone_dir_for(_NO_CHECKOUT_DIR)
            ),
            store=store,
            resume_plan=None,
            commit_sha=getattr(project, "commit_sha", None),
        )


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
    signals instead, exactly as :class:`~mewbo_core.session.session_provenance.SessionOrigin`
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

    # The session type the wiki tag grammar gives a MAINTAINER session. Read
    # through ``SessionTag.parse`` rather than matched as a prefix, so this
    # package never re-spells a grammar core owns.
    MAINTAINER_SESSION_TYPE = "wiki_maintain"

    @classmethod
    def maintainer_slug(cls, session_id: str, runtime: Any) -> str | None:
        """The project a MAINTAINER session was minted for, or ``None``.

        **The tag is the authorization; the slug is only the address — and both
        live in the tag for exactly that reason.** A session's ``context`` is
        writable by any caller (``backend.py``'s ``_build_context_payload``
        merges a request's ``context`` verbatim, with no unknown-key refusal),
        so a bare ``slug`` key proves nothing: a client could advertise the
        ``wiki`` capability, name any project, and — once anything READ that key
        — reach that project's page-write tools without ever having been given a
        session for it. Only the api's maintainer route stamps this tag, and
        only after validating the slug against an indexed project, so carrying
        it is the proof. Taking the slug from the tag too closes the residual
        half: a tag cannot be re-pointed by a later turn, where a context key
        can, which would otherwise let a session authorized for one project be
        re-addressed at another.

        A session carrying a ``slug`` context value and no tag therefore
        resolves NOTHING here, exactly as before this tier existed. The context
        value is still written and still consumed — it is what binds
        ``SessionSpec.slug`` — it is simply not an authorization.

        Cost: ``O(1)`` — one tag read (a single indexed lookup on both drivers).
        """
        store = cls.session_store_of(runtime)
        if store is None:
            return None
        try:
            tags = store.tags_for_session(session_id)
        except Exception:  # pragma: no cover — backend error
            return None
        for tag in tags:
            parsed = SessionTag.parse(tag)
            if parsed is not None and parsed.session_type == cls.MAINTAINER_SESSION_TYPE:
                return parsed.ids.get("wiki_id") or None
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

# The ``clone_dir`` a SLUG-bound ctx reports when the project has no completed
# index whose checkout survives on disk. A job id is a ``uuid4`` hex, so this
# name can never collide with one, and nothing writes here — it exists so the
# field stays a real ``Path`` (an empty one resolves to the process CWD) while
# still failing every ``exists()`` guard a reader applies.
_NO_CHECKOUT_DIR = "_no_checkout"


def _clone_dir_for(job_id: str) -> Path:
    """Return the on-disk clone directory for a wiki indexing job.

    Reads ``MEWBO_WIKI_CLONE_ROOT`` from the environment; falls back to
    ``/tmp/mewbo/wiki/clones``. An empty-string env var is treated as unset
    to avoid ``Path("")`` silently resolving to the process CWD.
    """
    root = os.environ.get("MEWBO_WIKI_CLONE_ROOT") or _DEFAULT_CLONE_ROOT
    return Path(root) / job_id


def resolve_job_ctx(session_id: str, runtime: Any) -> WikiJobCtx | None:
    """Return the write-side wiki ctx for *session_id*, or ``None`` if it has none.

    Two tiers, and **the job tier always wins**:

    1. a session bound to an INDEXING JOB (``find_job_by_session``) — the full
       ctx every phase tool needs: job id, that job's own checkout, its resume
       plan and the commit it is indexing;
    2. a session carrying the MAINTAINER TAG (:meth:`WikiJobCtx.for_maintainer`)
       — opened against an already-indexed project on demand, with no job to
       attach to and never will have one.

    Tier 2 exists because a page write had no other way in: ``wiki_submit_page``
    resolves through here, so before it a session could hold the whole page-write
    ceiling and be unable to use any of it. The tiers are ordered rather than
    merged so an INDEXING run is completely unaffected — a job-bound session
    never reaches the tag read, and no ctx it receives changes by one field.

    **Tier 2 is gated on a server-stamped TAG, never on a context key**, and
    that is a security property rather than a style choice: a caller can put an
    arbitrary ``slug`` into a session's context, so reading one here would turn
    an inert dead end into a live write path against a project the caller was
    never given a session for. :meth:`SessionProject.maintainer_slug` carries
    the argument.

    A tier-2 ctx reports ``job_bound`` False, carries an empty ``job_id`` and no
    resume plan, and stamps ``commit_sha`` from the PROJECT (the generation the
    wiki currently describes). Read ``job_bound`` before anything keyed on a job
    — the phase writers already do.

    Cost: ``O(one record)`` on the job tier. Tier 2 adds an ``O(1)`` tag read, a
    project read, and ``O(collection)`` in the slug's own job count for the
    checkout walk; it is reached only by a session that has no job at all.
    """
    store = getattr(runtime, "wiki_store", None)
    if store is None:
        return None
    job_id = store.find_job_by_session(session_id)
    if job_id is None:
        return WikiJobCtx.for_maintainer(session_id, runtime, store)
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


def build_jobless_ctx(
    *, job_id: str, slug: str, store: WikiStoreBase
) -> WikiJobCtx:
    """Build the :class:`WikiJobCtx` a SESSIONLESS runner operates over.

    Shared by every ``JoblessIndexRunner`` — the graph-only indexer and the
    scoped refresh runner alike. None of them has a Mewbo session, so
    ``session_id`` is empty and ``resume_plan`` is ``None``: the plan describes
    which LLM-driven phases an interrupted agent run may reuse, and a
    deterministic runner has none to reuse. ``clone_dir`` resolves via the same
    :func:`_clone_dir_for` rule the agent path uses, so the clone lands where
    the graph view + source endpoint expect it.
    """
    return WikiJobCtx(
        job_id=job_id,
        slug=slug,
        session_id="",
        clone_dir=_clone_dir_for(job_id),
        store=store,
        resume_plan=None,
    )


def resolve_workspace_slug(session_id: str, runtime: Any) -> str | None:
    """Return the wiki slug a *structured-response* session is grounded in.

    A ``StructuredResponder`` run (the ``/v1/structured`` ``workspace`` path)
    writes a ``{"structured_workspace": <slug>}`` context event onto the core
    session transcript but is NOT a registered wiki QA answer — so
    :func:`resolve_qa_ctx` alone can't scope its retrieval tools. This recovers
    the slug from the latest such event (last write wins).

    Deliberately the ONLY transcript-walking slug read in this module. The
    maintainer binding does NOT get a sibling here: it reads a session TAG
    instead (:meth:`SessionProject.maintainer_slug`), because a context key is
    re-writable by any later request and a tag is not.

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


def _job_slug(session_id: str, store: WikiStoreBase) -> str | None:
    """The slug of the indexing job backing *session_id*, if there is one.

    Best-effort by construction: a session with no job, a job row that has since
    been deleted, or a job carrying an empty slug all resolve to ``None`` so the
    caller falls through to the next tier rather than grounding on nothing.
    """
    job_id = store.find_job_by_session(session_id)
    if job_id is None:
        return None
    job = store.get_job(job_id)
    return job.slug if job is not None and job.slug else None


def resolve_qa_ctx(session_id: str, runtime: Any) -> WikiQaCtx | None:
    """Return the WikiQaCtx for *session_id*, or ``None`` if not grounded.

    Five-tier resolution, strongest binding first (one resolver, so
    ``_base._qa_ctx`` stays unchanged):

    1. a registered QA answer (``find_qa_by_session``);
    2. a session bound to an INDEXING JOB (``find_job_by_session``);
    3. a session carrying the maintainer TAG
       (:meth:`WikiJobCtx.for_maintainer`) — a project-bound maintainer session;
    4. a structured-response session scoped to a workspace
       (:func:`resolve_workspace_slug`);
    5. an ORDINARY task session whose own project is an indexed wiki
       (:meth:`SessionProject.resolve_slug`).

    Tiers 2-5 all yield a slug-only ctx (``answer_id=None``) so the
    read/navigate tools ground while the QA-emit/event tools keep guarding on
    ``answer_id`` — each is deliberately shape-identical to the others, so every
    tool already safe under one is safe under the rest. ``None`` means only
    that the session's project has no wiki indexed (or it has no project at all),
    which is an EXPECTED state rather than a fault — see
    ``WikiSessionTool._ungrounded_result``.

    **Tier 2 is what lets ONE session both READ and WRITE pages.** The page
    read tools resolve through here while ``wiki_submit_page`` resolves through
    :func:`resolve_job_ctx`, and without this tier no single session can satisfy
    both: a job-bound session has no QA answer and no workspace, so every read
    refuses, while a QA/structured session has no job, so every write
    refuses. An agent that must DECIDE which pages need updating has to read them
    first, so the two halves had to meet somewhere; they meet here, on the slug,
    which is the only thing the read tools need and which a job already knows.
    It sits directly below the QA tier because both are exact registrations,
    above the two heuristic tiers below it — and a session cannot be both a
    registered answer and an indexing job, so the ordering is unambiguous rather
    than a tie-break.
    """
    store = getattr(runtime, "wiki_store", None)
    if store is None:
        return None
    answer_id = store.find_qa_by_session(session_id)
    if answer_id is None:
        # An indexing job's own session: exact, and its slug is on the job.
        # Failing that, a session bound to a PROJECT by its own ``slug`` context
        # value — the same exact binding ``resolve_job_ctx``'s second tier
        # reads, and it has to be honoured on BOTH sides: a maintainer session
        # able to rewrite a page it cannot first read is the half-feature that
        # tier-2 above exists to prevent for indexing sessions.
        slug = _job_slug(session_id, store)
        if slug is None:
            maintainer = WikiJobCtx.for_maintainer(session_id, runtime, store)
            slug = maintainer.slug if maintainer is not None else None
        if slug is not None:
            return WikiQaCtx(
                answer_id=None,
                slug=slug,
                session_id=session_id,
                store=store,
            )
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

    A BACKWARD move is dropped from both. A resume is instructed to re-clone and
    re-scan so the source is on disk before pages are written, so those tools
    re-stamp phases the job passed long ago — a job that had already built its
    graph reported ``scan`` again with a later ``phase_started_at``, walking the
    progress bar backwards. Dropping the write on both surfaces rather than one
    is what preserves the can-never-disagree property above; the phase's own
    ``log`` lines still show the re-run happening. A deliberate RESTART is
    exempt: it really is redoing the pipeline from the top, so its phase must be
    allowed to return there.

    Entering a phase CLEARS the three ``phase_progress_*`` fields and stamps
    ``last_progress_at``. The clear is the invariant every progress reader leans
    on: a non-null ``phase_progress_current`` therefore always belongs to the
    phase named in ``phase``, never a leftover from one that already ended —
    which is precisely how ``scanned_count``/``current_file`` became
    untrustworthy. It lives HERE rather than at each phase tool so a phase added
    later inherits it without knowing it exists.

    ``last_progress_at`` is STAMPED, not cleared, and that asymmetry is
    deliberate: it answers "when did this job last move at all", and entering a
    phase is movement. Clearing it would erase the one signal that separates a
    long phase from a dead one at the exact moment a phase begins.

    A ctx with no job at all (a maintainer session — see
    :attr:`WikiJobCtx.job_bound`) reports nothing: a phase is a position in an
    indexing PIPELINE, so there is no progress here to describe, and writing one
    under an empty job id would append to an event log no stream ever reads.
    """
    if not getattr(ctx, "job_id", ""):
        return
    # Read defensively: this seam is reached with duck-typed ctx stand-ins that
    # carry only what their own tool needs, so a hard attribute read here turns
    # an unrelated tool's test into a crash inside progress reporting.
    plan = getattr(ctx, "resume_plan", None)
    if not (plan is not None and getattr(plan, "restart", False)):
        try:
            job = ctx.store.get_job(ctx.job_id)
        except Exception:
            job = None
        if job is not None and getattr(job, "regresses_to", None) and job.regresses_to(name):
            return

    started_at = IndexingJob.format_stamp(datetime.now(timezone.utc))
    try:
        ctx.store.append_job_event(ctx.job_id, {"type": "phase", "name": name})
    except Exception:
        pass
    try:
        ctx.store.update_job(
            ctx.job_id,
            phase=name,
            phase_started_at=started_at,
            last_progress_at=started_at,
            phase_progress_current=None,
            phase_progress_total=None,
            phase_progress_unit=None,
        )
    except Exception:
        pass


def emit_scope_preview(ctx: WikiJobCtx, preview: ScopePreview) -> None:
    """Append a ``scope_preview`` event AND persist it on the job snapshot.

    The same one-writer/two-transports property :func:`emit_phase` holds, for
    the same reason: the live indexing timeline reads the event stream while the
    landing card polls the snapshot endpoint, and a preview written down only
    ONE of those paths would let the two surfaces report different scopes for
    the same refresh. Both reads are derived from the single call here, so they
    cannot disagree — and a caller cannot accidentally write one without the
    other, which is what a second emitter would eventually do.

    The counts are spread FLAT alongside ``type`` rather than nested under a
    key, matching the ``queued`` event and :class:`ScopePreview`'s own
    deliberately flat shape: the reader is a panel rendering a row of integers,
    and the model carries no ``type`` field for the spread to collide with.

    Best-effort on both legs, like every other emitter here — a store hiccup
    must cost a progress detail, never the refresh that produced it.
    """
    try:
        ctx.store.append_job_event(
            ctx.job_id,
            {"type": "scope_preview", **preview.model_dump(by_alias=True)},
        )
    except Exception:
        pass
    try:
        ctx.store.update_job(ctx.job_id, scope_preview=preview)
    except Exception:
        pass


def emit_phase_once(ctx: WikiJobCtx, name: str) -> None:
    """Advance to phase *name* only if the job is not already in it.

    The seam for a phase whose work is done by a FAN-OUT rather than by one
    boundary tool: every worker calls this, the first one moves the job, the rest
    are no-ops — without the guard the event log would carry one ``phase`` event
    per worker.

    Do NOT instead stamp the phase from the tail of the PRECEDING tool: that
    marks a phase started at the moment its predecessor ENDED, so a run dying in
    the gap reports a phase whose work never began (a job sits at ``enrich``
    after being cancelled 43ms after the graph build returned). A phase must be
    stamped by work that has actually started.
    """
    try:
        job = ctx.store.get_job(ctx.job_id)
    except Exception:
        job = None
    if job is not None and getattr(job, "phase", None) == name:
        return
    emit_phase(ctx, name)


_UNSET_STEP = object()
_MUTATION_FAILED = object()


def emit_log(
    ctx: WikiJobCtx,
    text: str,
    *,
    level: Literal["info", "warn", "error"] = "info",
    step: str | None | object = _UNSET_STEP,
) -> None:
    """Append a free-form ``log`` event for the indexing timeline.

    An open :class:`ProgressReporter` stamps its active step unless *step*
    explicitly names one. A line intentionally outside the ledger, such as a
    phase opener, must pass ``step=None`` at its call site rather than omit the
    argument. The event model records missing attribution instead of raising:
    this writer is best-effort, and a validation error here would silently drop
    the line rather than preserve the evidence the coverage gate needs.

    Silent for a ctx with no job, for :func:`emit_phase`'s reason: the timeline
    belongs to an indexing job, and a line written under an empty job id is an
    orphan record no surface can reach.

    Cost: ``O(1)`` — one append to this job's event stream.
    """
    if not getattr(ctx, "job_id", ""):
        return
    active = _ACTIVE_STEP.get()
    if step is _UNSET_STEP and active is not None and active[0] == id(ctx):
        step = active[1]
    # Preserve an omitted step rather than rejecting the line. The stored event's
    # computed fact lets the coverage test expose this escaped scope deterministically.
    event = LogJobEvent(
        type="log", level=level, text=text, step=step if isinstance(step, str) else None
    ).stored_payload()
    try:
        ctx.store.append_job_event(ctx.job_id, event)
    except Exception:
        pass


# Kept task-local so simultaneous job/tool calls cannot stamp one another's
# logs. The context manager resets its token even on an exception.
_ACTIVE_STEP: ContextVar[tuple[int, str] | None] = ContextVar(
    "wiki_active_progress_step", default=None
)


# How long a bulk phase may run without reporting progress. Same order as
# ``scan.py``'s per-file event flush and chosen the same way: often enough that
# a reader never mistakes a working phase for a wedged one, rare enough that the
# write is noise beside the work it reports.
_PROGRESS_INTERVAL_S: float = 5.0


class StepHandle(AbstractContextManager["StepHandle"]):
    """The open scope for one :class:`ProgressReporter` step.

    A handle is hot in-process state, not a persisted contract. It owns the
    scoped active-step stamp and leaves no running record behind when its body
    raises.
    """

    def __init__(
        self, reporter: ProgressReporter, key: str, *, total: int | None = None
    ) -> None:
        self._reporter = reporter
        self._key = key
        self._total = total
        self._token: Token[tuple[int, str] | None] | None = None
        self._declared = False

    def __enter__(self) -> StepHandle:
        """Open the step and force its durable running state. Cost: ``O(steps)``."""
        self._declared = self._reporter._enter(self._key, total=self._total)
        if self._declared:
            self._token = _ACTIVE_STEP.set((id(self._reporter.ctx), self._key))
        return self

    def __exit__(
        self, exc_type: Any, exc: BaseException | None, traceback: Any
    ) -> Literal[False]:
        """Close the step durably, marking a raised body failed before re-raising.

        Cost: ``O(steps)``.
        """
        if self._token is not None:
            _ACTIVE_STEP.reset(self._token)
        if self._declared:
            if exc is None:
                self._reporter._finish(self._key, state="done")
            else:
                self._reporter._finish(
                    self._key, state="failed", note=self._reporter._exception_note(exc)
                )
        return False

    def advance(
        self,
        current: int | None = None,
        total: int | None = None,
        detail: str = "",
    ) -> None:
        """Advance this step and persist on the shared five-second cadence.

        Cost: ``O(steps)`` only when the cadence is due; otherwise ``O(1)``.
        """
        if self._declared:
            self._reporter._advance(self._key, current, total, detail=detail)

    def log(
        self, text: str, *, level: Literal["info", "warn", "error"] = "info"
    ) -> None:
        """Append a line owned by this handle's declared step.

        The explicit key makes attribution independent of task-local lookup, so a
        callback or a later refactor cannot silently turn step work into a phase
        line merely by running outside this context manager's dynamic extent.
        """
        emit_log(self._reporter.ctx, text, level=level, step=self._key)

    def count(self, iterable: Iterable[Any], total: int | None = None) -> Iterator[Any]:
        """Yield items while advancing once per item without caller bookkeeping.

        Cost: ``O(1)`` per yielded item, plus an ``O(steps)`` persist when due.
        """
        current = self._reporter._current_for(self._key)
        for item in iterable:
            current += 1
            self.advance(current=current, total=total)
            yield item


class ProgressReporter:
    """The ONE writer of a job's step ledger.

    Construction reads the durable ledger from the job record, so a fresh tool
    instance per call sees prior declarations and completions. Bind one reporter
    per tool invocation and pass it down; do not construct reporters inside a
    loop. The context, clock, and shared :class:`PhaseProgress` cadence are
    injected collaborators so time can be tested without sleeping.
    """

    def __init__(self, ctx: Any, *, clock: Any = None, interval_s: float | None = None) -> None:
        """Load *ctx*'s persisted ledger and bind its persistence collaborators.

        A missing job or unavailable store starts an empty ledger: reporting must
        not be the thing that fails an index.

        Cost: ``O(one record)``.
        """
        self.ctx = ctx
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ledger = ProgressLedger()
        # Filled by ``settle``; see ``settled_unreported``.
        self._settled_unreported: list[str] = []
        if not getattr(ctx, "job_id", ""):
            job = None
        else:
            try:
                job = ctx.store.get_job(ctx.job_id)
            except Exception:
                job = None
        if job is not None and getattr(job, "progress", None) is not None:
            self._ledger = job.progress.model_copy(deep=True)
        # PhaseProgress owns the cross-instance persisted-clock throttle. This
        # reporter intentionally delegates that cadence rather than growing a
        # second elapsed-time rule for ledger updates.
        self._cadence = PhaseProgress(
            ctx, label="", unit="", interval_s=interval_s, clock=self._clock
        )
        self._declare_pipeline_plan()

    def _declare_pipeline_plan(self) -> None:
        """Persist every pipeline step before the first phase can report it.

        Cost: ``O(one record + declared steps)``. A phase-local declaration makes
        the denominator grow as work starts, so an in-progress index measures a
        prefix of the pipeline as though it were the whole run. Extending an old
        partial ledger preserves its history while adding its formerly unseen
        tail, which keeps persisted jobs readable and corrects their next write.
        """
        slug = getattr(self.ctx, "slug", None)
        if not getattr(self.ctx, "job_id", "") or not isinstance(slug, str) or not slug:
            return
        try:
            from mewbo_graph.plugins.wiki.step_plans import (  # noqa: PLC0415
                planned_pipeline_steps_for_slug,
            )

            before = len(self._ledger.steps)
            self._ledger.extend(planned_pipeline_steps_for_slug(self.ctx.store, slug))
            if len(self._ledger.steps) != before:
                self._persist(self._clock(), force=True)
        except Exception as exc:
            self._record_mutation_failure("declare_pipeline", exc)

    @property
    def active_key(self) -> str | None:
        """The currently open declared step, or ``None``.

        Cost: ``O(steps)``.
        """
        active = self._ledger_active()
        return active.key if active is not None else None

    def declare(self, specs: Sequence[StepSpec]) -> None:
        """Extend the declared plan idempotently and force a durable write.

        Cost: ``O(steps)``.
        """
        try:
            self._ledger.extend(specs)
        except Exception as exc:
            self._record_mutation_failure("declare", exc)
            return
        self._persist(self._clock(), force=True)

    def _record_mutation_failure(
        self, operation: str, exc: Exception, *, step: str | None = None
    ) -> None:
        """Persist a bounded failure record without letting progress stop work.

        A ledger validation error proves the display state is invalid, not that
        the index's actual work failed. The record makes that omission observable
        on the job timeline rather than silently swallowing the defect, while the
        enclosing index proceeds.
        """
        if not getattr(self.ctx, "job_id", ""):
            return
        try:
            self.ctx.store.append_job_event(
                self.ctx.job_id,
                ProgressErrorJobEvent(
                    type="progress_error",
                    operation=operation,
                    error=self._exception_note(exc),
                    step=step,
                ).stored_payload(),
            )
        except Exception:
            pass

    def _mutate(self, operation: str, action: Any, *, step: str | None = None) -> Any:
        """Run one ledger mutation without allowing reporting to fail an index."""
        try:
            return action()
        except Exception as exc:
            self._record_mutation_failure(operation, exc, step=step)
            return _MUTATION_FAILED

    def _ledger_record(self, key: str) -> Any:
        """Read one record while reporting malformed ledger state visibly."""
        return self._mutate("find", lambda: self._ledger.find(key), step=key)

    def _ledger_active(self) -> Any:
        """Read the active record while reporting malformed ledger state visibly."""
        return self._mutate("active", lambda: self._ledger.active)

    def step(self, key: str, *, total: int | None = None) -> StepHandle:
        """Return the context manager that opens and closes declared *key*.

        Enter writes ``running`` immediately; normal exit writes ``done`` and an
        exceptional exit writes ``failed`` with bounded exception text before the
        original exception is re-raised.

        Cost: ``O(1)`` to construct; entering and exiting cost ``O(steps)``.
        """
        return StepHandle(self, key, total=total)

    def skip(self, key: str, *, note: str = "") -> None:
        """Mark a declared step skipped and force its durable terminal state.

        Cost: ``O(steps)``.
        """
        record = self._ledger_record(key)
        if record is None or record is _MUTATION_FAILED or record.terminal:
            return
        now = self._clock()
        finished = self._mutate(
            "skip", lambda: self._ledger.finish(key, now, state="skipped", note=note), step=key
        )
        if finished is _MUTATION_FAILED:
            return
        self._persist(now, force=True)

    def set_total(self, key: str, total: int) -> None:
        """Prime an aggregate step's known denominator without opening it.

        Cost: ``O(steps)``. A plan can know a fan-out's size before its first
        worker begins; stamping a total alone keeps that distinction truthful.
        """
        record = self._ledger_record(key)
        if record is None or record is _MUTATION_FAILED or record.terminal:
            return
        result = self._mutate(
            "set_total", lambda: self._ledger.advance(key, current=0, total=total), step=key
        )
        if result is not _MUTATION_FAILED:
            self._persist(self._clock(), force=True)

    def finish(self, key: str) -> None:
        """Close an aggregate step after its durable final unit lands.

        Cost: ``O(steps)``. Aggregate fan-outs report each unit through
        :meth:`report` and use this once at their real terminal boundary.
        """
        record = self._ledger_record(key)
        if record is None or record is _MUTATION_FAILED or record.terminal:
            return
        self._finish(key, state="done")

    def skip_group(self, group: str, *, note: str = "") -> None:
        """Mark every unfinished step in a bypassed phase terminal.

        Cost: ``O(steps)``. The full pipeline plan is declared before a job starts,
        so a known-bypassed phase must close every one of its records; otherwise
        its pending weight would strand the operation short of completion.
        """
        now = self._clock()
        changed = False
        for record in self._ledger.steps_in(group):
            if record.terminal:
                continue
            result = self._mutate(
                "skip_group",
                lambda key=record.key: self._ledger.finish(
                    key, now, state="skipped", note=note
                ),
                step=record.key,
            )
            changed = changed or result is not _MUTATION_FAILED
        if changed:
            self._persist(now, force=True)

    def finish_group(self, group: str) -> None:
        """Close each unfinished step in an externally driven completed phase.

        Cost: ``O(steps)``. Sessionless runners reuse phase cores that do not
        expose individual step boundaries. They still ran the phase, so recording
        it as skipped would lie; these zero-duration records preserve completion
        without inventing a second progress mechanism for that runner. They must
        not feed calibration: an uninstrumented runner observed no duration.
        """
        now = self._clock()
        changed = False
        for record in self._ledger.steps_in(group):
            if record.terminal:
                continue
            result = self._mutate(
                "finish_group",
                lambda key=record.key: self._ledger.enter(key, now),
                step=record.key,
            )
            if result is _MUTATION_FAILED:
                continue
            result = self._mutate(
                "finish_group",
                lambda key=record.key: self._ledger.finish(key, now),
                step=record.key,
            )
            changed = changed or result is not _MUTATION_FAILED
        if changed:
            self._persist(now, force=True)

    def settle(self) -> None:
        """Close every non-terminal declared step at the operation's own end.

        Cost: ``O(steps)``. A terminal boundary tool is the one place the whole
        operation can honestly answer "is anything still open" — a fan-out step
        opened via :meth:`report` (no closing ``with`` scope) has no other
        moment that marks it finished, and a run shape that never reaches a
        declared group (an optional phase this deployment or resume skipped)
        has no other moment that marks it inapplicable. A ``running`` record
        genuinely did work, so it closes ``done``; a ``pending`` one never
        started this run, so it closes ``skipped`` — the same distinction
        :meth:`skip_group` draws, generalised to whatever is still open rather
        than one named group.

        **Settling must not be silent about the suspicious case.** A step left
        `pending` in a group whose OTHER steps completed did not go unreached —
        that group ran, and one implementation of it reported nothing. Closing
        it `skipped` writes a false statement about work that happened, and a
        false statement nobody can see is how a reporting gap survives: an
        uninstrumented second implementation of the scan phase was found only
        because a finished job still showed its steps `pending`. So those
        settle with a note naming what actually happened, and settling itself
        says so on the timeline — the emission lives HERE rather than at the
        call site because a caller that forgets it is a second writer on one
        timeline, the shape this ledger exists to remove.
        """
        now = self._clock()
        changed = False
        settled_partial: list[str] = []
        # A group is "reached" when anything in it settled — the discriminator
        # between an optional phase this run genuinely skipped and a phase that
        # ran while half of it stayed silent.
        reached = {record.group for record in self._ledger.steps if record.terminal}
        for record in self._ledger.steps:
            if record.terminal:
                continue
            if record.state == "running":
                # A fan-out reported through ``report`` has no closing scope;
                # this is its documented end, not an anomaly. No note: `note`
                # explains a SKIP or a FAILURE, and the contract rejects one on
                # a step that finished its work.
                result = self._mutate(
                    "settle", lambda key=record.key: self._ledger.finish(key, now), step=record.key
                )
            else:
                partial = record.group in reached
                if partial:
                    settled_partial.append(record.key)
                note = (
                    "never reported — its phase ran without it"
                    if partial
                    else "not reached this run"
                )
                result = self._mutate(
                    "settle",
                    lambda key=record.key, note=note: self._ledger.finish(
                        key, now, state="skipped", note=note
                    ),
                    step=record.key,
                )
            changed = changed or result is not _MUTATION_FAILED
        if changed:
            self._persist(now, force=True)
        self._settled_unreported = settled_partial
        if settled_partial:
            try:
                emit_log(
                    self.ctx,
                    "Progress gap: "
                    + ", ".join(sorted(settled_partial))
                    + " never reported while their phase completed",
                    level="warn",
                )
            except Exception as exc:  # pragma: no cover - reporting is best-effort
                self._record_mutation_failure("settle_warn", exc)

    @property
    def settled_unreported(self) -> list[str]:
        """Steps :meth:`settle` closed whose own phase had already reported.

        Empty on a healthy run. A non-empty list names steps whose work either
        ran uninstrumented or never ran while its siblings did — either way a
        reporting defect the operator should see rather than a run shape.
        """
        return list(self._settled_unreported)

    def _enter(self, key: str, *, total: int | None) -> bool:
        """Open one declared step and force the initial snapshot. Cost: ``O(steps)``."""
        now = self._clock()
        record = self._mutate("enter", lambda: self._ledger.enter(key, now), step=key)
        if record is None or record is _MUTATION_FAILED:
            return False
        if total is not None:
            advanced = self._mutate(
                "enter", lambda: self._ledger.advance(key, current=0, total=total), step=key
            )
            if advanced is _MUTATION_FAILED:
                return False
        self._persist(now, force=True)
        return True

    def _finish(self, key: str, *, state: StepState, note: str = "") -> None:
        """Finish one declared step and force its terminal snapshot. Cost: ``O(steps)``."""
        now = self._clock()
        result = self._mutate(
            "finish", lambda: self._ledger.finish(key, now, state=state, note=note), step=key
        )
        if result is _MUTATION_FAILED:
            self._discard_failed_step(key)
        self._persist(now, force=True)

    def _discard_failed_step(self, key: str) -> None:
        """Remove a step whose close was rejected rather than leave it running.

        A failed ledger transition cannot be persisted as a terminal record, but
        retaining the prior ``running`` record would assert work is still active
        after its scope has exited. Dropping this one progress detail is the only
        honest best-effort fallback; the preceding ``progress_error`` event
        records why it vanished.
        """
        try:
            self._ledger = ProgressLedger(
                version=self._ledger.version,
                steps=[record for record in self._ledger.steps if record.key != key],
            )
        except Exception as exc:
            self._record_mutation_failure("discard_failed_step", exc, step=key)

    def report(
        self,
        key: str,
        current: int | None = None,
        total: int | None = None,
        *,
        detail: str = "",
    ) -> None:
        """Record a declared active step without closing a fan-out scope.

        A fresh reporter per worker reloads the prior running record, advances it,
        and leaves it open for the next worker. Cost: ``O(steps)`` when due.
        """
        record = self._ledger_record(key)
        if record is None or record is _MUTATION_FAILED:
            return
        if record.state != "running":
            now = self._clock()
            entered = self._mutate(
                "report", lambda: self._ledger.enter(key, now), step=key
            )
            if entered is _MUTATION_FAILED:
                return
            advanced = self._mutate(
                "report",
                lambda: self._ledger.advance(key, current, total, detail=detail),
                step=key,
            )
            if advanced is not _MUTATION_FAILED:
                self._persist(now, force=True)
            return
        self._advance(key, current, total, detail=detail)

    def _advance(
        self, key: str, current: int | None, total: int | None, *, detail: str
    ) -> None:
        """Record one unit and persist only when the shared cadence is due.

        Cost: ``O(1)`` when throttled; ``O(steps)`` when it writes.
        """
        advanced = self._mutate(
            "advance",
            lambda: self._ledger.advance(key, current=current, total=total, detail=detail),
            step=key,
        )
        if advanced is _MUTATION_FAILED:
            return
        now = self._clock()
        self._persist(now, force=False)

    def _current_for(self, key: str) -> int:
        """Return this step's current count, defaulting to zero. Cost: ``O(steps)``."""
        record = self._ledger_record(key)
        if record is None or record is _MUTATION_FAILED:
            return 0
        return record.current if record.current is not None else 0

    def _exception_note(self, exc: BaseException) -> str:
        """Return bounded exception text without allowing formatting to fail reporting.

        Cost: ``O(1)``.
        """
        try:
            return str(exc)[:500]
        except Exception:
            return exc.__class__.__name__

    def _persist(self, now: datetime, *, force: bool) -> None:
        """Write the ledger snapshot and its matching SSE event best-effort.

        Every snapshot is a NAMED-field write: writing ``progress`` cannot revert
        a concurrent ``cancel_job`` status update. Concurrent ledger reporters
        can lose one another's step update because fan-out mints fresh tools; the
        loss is bounded and heals on the next write. Reverting an unrelated field
        is the unacceptable direction, so no lock is added here.

        The legacy triple is derived from an active declared-count step with a
        reported position so an in-flight job and an un-migrated client keep
        working; it is no longer the
        progress model. Snapshot and event share this cadence, never one write per
        repository unit.

        Cost: ``O(steps)`` when due; ``O(1)`` when throttled.
        """
        if not getattr(self.ctx, "job_id", ""):
            return
        if force:
            self._cadence._last = now
        elif not self._cadence._due(now):
            return
        else:
            self._cadence._last = now
        active = self._ledger.active
        fields: dict[str, Any] = {
            "progress": self._ledger,
            "last_progress_at": IndexingJob.format_stamp(now),
            "phase_progress_current": None,
            "phase_progress_total": None,
            "phase_progress_unit": None,
        }
        if active is not None and active.unit is not None and active.current is not None:
            fields.update(
                phase_progress_current=active.current,
                phase_progress_total=active.total,
                phase_progress_unit=active.unit,
            )
        try:
            self.ctx.store.update_job(self.ctx.job_id, **fields)
        except Exception:
            pass
        if not force:
            try:
                self.ctx.store.append_job_event(
                    self.ctx.job_id, {"type": "progress", **self._ledger.export(now)}
                )
            except Exception:
                pass


class PhaseProgress:
    """Throttled cadence for an open step in the bulk phases.

    The durable step ledger is now the progress mechanism. This class remains
    its shared five-second write cadence, preserving cross-instance throttling
    for long loops and fresh fan-out tool instances.

    ``graph`` and ``enrich`` are the two phases whose work is a long loop or a
    fan-out with no per-unit boundary tool, so nothing at all was written to the
    job between their start and their end — one real index spent 25 minutes
    inside the tree-sitter loop emitting nothing, which from outside is
    indistinguishable from a dead run. This is the throttle ``scan.py`` already
    applies to its per-file event flush, hoisted onto a class so the two silent
    phases share one cadence rather than each growing its own.

    **Cross-instance by construction.** The enrich fan-out reaches this through
    a FRESH tool instance per mint, so a purely instance-local timer would never
    throttle anything. The first :meth:`advance` therefore seeds its deadline
    from the job's own persisted ``last_progress_at``; every later call on the
    same instance uses the in-memory one, which is what keeps the graph loop
    from reading the job once per file.

    Collaborators are injected — the job ctx carries the store, and the clock is
    a field so a test can drive the throttle without sleeping.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        label: str,
        unit: str,
        units_of: Any = None,
        interval_s: float | None = None,
        clock: Any = None,
        on_flush: Any = None,
    ) -> None:
        """Bind the progress writer to *ctx*'s job.

        *label* names the work in the timeline ("Parsing"); *unit* is the plural
        noun a progress reader renders ("files", "nodes", "entities") — both are
        constant for the life of one phase, which is why they are fields rather
        than per-call arguments.

        ``units_of`` is a ``() -> (current, total)`` supplier for a phase whose
        position costs a store read — it is invoked ONLY when a write is
        actually due, which is the whole reason it is a callable rather than the
        numbers themselves. A loop that already knows where it is passes them
        straight to :meth:`advance` instead and needs no supplier.

        ``on_flush`` is a callback run on the same due-check, for a caller with
        writes of its own to batch on this cadence (``scan.py``'s per-file event
        drain). It exists so there is ONE throttle in the pipeline rather than a
        second inline copy of the same elapsed-time rule.

        ``interval_s`` defaults to :data:`_PROGRESS_INTERVAL_S` — resolved HERE
        rather than in the signature, so the module constant stays the single
        source of truth at call time instead of being frozen into a default at
        import time.
        """
        self._ctx = ctx
        self._label = label
        self._unit = unit
        self._units_of = units_of
        self._on_flush = on_flush
        self._interval = _PROGRESS_INTERVAL_S if interval_s is None else interval_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last: datetime | None = None

    def advance(
        self,
        current: int | None = None,
        total: int | None = None,
        *,
        detail: str = "",
        force: bool = False,
    ) -> None:
        """Report progress; write only when the interval has elapsed.

        ``force`` writes regardless of the throttle — for the FINAL unit of a
        loop, so a finished phase's last position is the one on record rather
        than whatever the last throttled write happened to catch.
        """
        now = self._clock()
        if not (force or self._due(now)):
            return
        self._last = now
        if current is None:
            current, total = self._supplied_units()
        if self._on_flush is not None:
            self._on_flush()
        if self._label:
            emit_log(self._ctx, self._text(current, total, detail))
        fields: dict[str, Any] = {"last_progress_at": IndexingJob.format_stamp(now)}
        if current is not None:
            # The ONE generic per-phase progress triple. ``emit_phase`` clears
            # it on every transition, so what is written here can only ever be
            # read as belonging to the phase that wrote it.
            fields["phase_progress_current"] = current
            fields["phase_progress_total"] = total
            fields["phase_progress_unit"] = self._unit
        try:
            self._ctx.store.update_job(self._ctx.job_id, **fields)
        except Exception:
            pass

    def _supplied_units(self) -> tuple[int | None, int | None]:
        """Resolve ``units_of`` if one was injected — best-effort, like the rest.

        A count that cannot be taken must not cost the run its progress line:
        the position is dropped and the timeline still records that the phase
        moved.
        """
        if self._units_of is None:
            return (None, None)
        try:
            done, total = self._units_of()
        except Exception:
            return (None, None)
        return (done, total)

    def _due(self, now: datetime) -> bool:
        """True when *now* is at least one interval past the last progress write."""
        if self._last is not None:
            return (now - self._last).total_seconds() >= self._interval
        elapsed = self._persisted_elapsed(now)
        # Only an elapsed inside [0, interval) is a reason to stay silent. A
        # NEGATIVE one means the stored stamp is ahead of our clock — skew, or a
        # writer on another host — and reading that as "written recently, hold
        # off" would suppress every progress write for as long as the skew
        # lasts. An unusable baseline must always mean "write", never "wait".
        if elapsed is None or not (0.0 <= elapsed < self._interval):
            return True
        # Adopt the persisted stamp as this instance's baseline, so a fan-out
        # worker that arrives mid-interval stays silent instead of writing.
        self._last = now - timedelta(seconds=elapsed)
        return False

    def _persisted_elapsed(self, now: datetime) -> float | None:
        """Seconds since the job's own last progress write, or ``None`` if unknown.

        ``None`` means there is nothing to throttle against — no stamp, an
        unreadable store, or a duck-typed ctx stand-in whose job is not a real
        :class:`~mewbo_graph.wiki.types.IndexingJob`. Every one of those says
        "write", never "stay silent": a missing baseline must not be the reason
        a phase reports nothing.
        """
        try:
            job = self._ctx.store.get_job(self._ctx.job_id)
        except Exception:
            return None
        reader = getattr(job, "seconds_since_progress", None)
        return reader(now) if reader is not None else None

    def _text(self, done: int | None, total: int | None, detail: str) -> str:
        """Compose the timeline line: ``"<label> <done>/<total>: <detail>"``."""
        head = self._label
        if done is not None:
            head = f"{head} {done}/{total}" if total else f"{head} {done}"
        return f"{head}: {detail}" if detail else head


def resolve_qa_clone_dir(
    slug: str, store: Any, *, session_id: str | None = None
) -> Path | None:
    """Return the on-disk clone dir a caller should read *slug*'s source from.

    ONE resolver, two questions — and the answers genuinely differ:

    * **A session bound to an indexing job reads that job's OWN checkout.**
      Pass ``session_id`` and, when ``find_job_by_session`` hits, the job's
      clone dir wins. A job is not ``complete`` until its own finalize runs,
      so the completed-job rule below would hand a live job the PREVIOUS
      index's clone — the source as it stood BEFORE the changes that job is
      indexing. That path raises nothing and reads nothing empty; it just
      answers from stale bytes, which is the worst shape of defect.
    * **Everything else gets the most-recent ``complete`` job's clone dir.**
      That is the source the wiki was built from, so a Q&A answer stays
      consistent with the wiki the user is reading. This is the DEFAULT:
      omit ``session_id`` (the slug-only source route does) and the
      behaviour is exactly what it always was.

    The job tier is deliberately NOT filtered on status. A job-bound session
    should read its own checkout whether or not finalize has run yet —
    filtering would make the resolution jump to a different directory
    mid-session at the moment its own job completes. The job's ``slug`` is
    checked against *slug* so a session bound to some other repo's job can
    never be handed that repo's checkout.

    Returns ``None`` if nothing resolves or the dir is gone (e.g. the volume
    was wiped). Callers should report that to the LLM as "source files not
    available — answer from wiki/graph only".

    Cost: ``O(collection)`` in the slug's job count for the completed-job
    walk, ``O(one record)`` for the job tier.
    """
    if session_id:
        try:
            job_id = store.find_job_by_session(session_id)
            job = store.get_job(job_id) if job_id is not None else None
        except Exception:
            job = None
        if job is not None and getattr(job, "slug", None) == slug:
            job_clone = _clone_dir_for(job.job_id)
            if job_clone.is_dir():
                return job_clone
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
    "build_jobless_ctx",
    "get_session_store",
    "resolve_runtime",
    "resolve_job_ctx",
    "resolve_qa_ctx",
    "resolve_workspace_slug",
    "resolve_qa_clone_dir",
    "emit_phase",
    "emit_phase_once",
    "emit_log",
    "emit_scope_preview",
    "ProgressReporter",
    "StepHandle",
]
