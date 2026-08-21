#!/usr/bin/env python3
"""Wiki persistence layer.

JSON-file backed implementation (default) + abstract base for the
MongoDB impl that lands in Task 1.4. Layout under ``$MEWBO_HOME/wiki/``:

    projects/<slug>.json                    (Project model — DISPLAY snapshot)
    settings/<slug>.json                    (ProjectSettings — the editable record)
    pages/<slug>/_index.json                (page-id→title index for fast listing)
    pages/<slug>/<page_id>.json             (full WikiPage including body)
    jobs/<job_id>/job.json                  (IndexingJob model)
    jobs/<job_id>/events.jsonl              (append-only event log with idx)
    jobs/<job_id>/session.txt               (Mewbo session_id — one line)
    qa/<answer_id>/answer.json              (QaAnswer model)
    qa/<answer_id>/events.jsonl             (append-only event log with idx)

Slugs that contain slashes (e.g. "org/repo") are escaped as "org__repo"
so they map safely to a single directory/filename segment.
"""
from __future__ import annotations

import abc
import json
import math
import shutil
import struct
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from pydantic import BaseModel

from mewbo_graph._util import cosine as _cosine
from mewbo_graph.entities.types import (
    Entity,
    EntityEmbedding,
    EntityFilter,
    EntityRecommendation,
    EntityRelation,
)

from .events import WikiJobEvent
from .memory_types import (
    DocPageNote,
    EntityKey,
    FileManifest,
    MemoryEdge,
    MemoryEmbedding,
    MemoryFilter,
    MemoryNode,
)
from .types import (
    CommitScope,
    Embedding,
    GraphEdge,
    GraphNode,
    GraphNodeAdapter,
    IndexingJob,
    Project,
    ProjectSettings,
    QaAnswer,
    WikiPage,
)

logging = get_logger(name="api.wiki.store")

_M = TypeVar("_M", bound=BaseModel)


class _HasVector(Protocol):
    """Structural type for any embedding row carrying a dense ``vector``.

    Both ``MemoryEmbedding`` and ``EntityEmbedding`` satisfy it, so the single
    cosine-rank core (`_rank_embeddings`) is reused across both families.
    """

    vector: list[float]


_V = TypeVar("_V", bound=_HasVector)


def _slug_to_path(slug: str) -> str:
    """Escape a slug so it maps safely to a single filesystem segment."""
    return slug.replace("/", "__")


@dataclass(frozen=True)
class PageClaim:
    """Outcome of a job claiming one page id: the new count + whether it was new.

    One answer rather than two reads. The read-then-increment this replaces
    asked the store twice — "does this page exist" and then "bump the counter" —
    so two page-writers landing together could both see "new" and over-count,
    and a caller that saw "not new" had to go back for the count.
    """

    count: int
    is_new: bool


@dataclass(frozen=True)
class JobPatch:
    """The ``IndexingJob`` fields a caller NAMED, validated and nothing else.

    The unit both drivers write, and what keeps two overlapping writers from
    losing each other's changes: "set these fields" and "rewrite the document
    that happens to hold them" are not the same operation. The second reverts
    every field a CONCURRENT writer changed between this writer's read and its
    write — a cancel landing between a progress writer's read and its write was
    silently undone, and the job carried on running with no record that a cancel
    had ever been asked for. Carrying only the named fields lets each backend
    narrow its write to what the caller actually asked for, so writers touching
    disjoint fields stop colliding at all.
    """

    # ``progress`` follows the same named-field rule: its write is disjoint from
    # ``cancel_job`` writing ``status``, so neither can revert the other. Fresh
    # fan-out reporters can race on the ledger field itself and lose one step
    # update; that bounded loss heals on the next report and is acceptable beside
    # the unacceptable alternative of reverting an unrelated field.
    fields: dict[str, Any]

    @classmethod
    def build(cls, job: IndexingJob, fields: Mapping[str, Any]) -> JobPatch:
        """Validate *fields* against the WHOLE *job*, then keep only those keys.

        Validation stays whole-document — an unknown key still fails
        ``extra="forbid"`` and every value is still coerced by the field that
        owns it — because what needed narrowing is the WRITE, not the check.

        An explicit ``None`` is a VALUE here, never an omission: ``emit_phase``
        clears the three ``phase_progress_*`` fields by naming them, so dropping
        falsy values (as the ``PROJECT_UPDATABLE`` filter does, for a surface
        whose ``None`` genuinely means "not supplied") would silently discard
        the write the progress invariant depends on.
        """
        merged = job.model_dump(by_alias=False)
        merged.update(fields)
        coerced = IndexingJob.model_validate(merged).model_dump(by_alias=False)
        return cls(fields={name: coerced[name] for name in fields})

    def apply(self, job: IndexingJob) -> IndexingJob:
        """Return *job* with this patch's fields set, re-validated as a whole."""
        merged = job.model_dump(by_alias=False)
        merged.update(self.fields)
        return IndexingJob.model_validate(merged)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class WikiStoreBase(abc.ABC):
    """Abstract base for wiki persistence backends."""

    # Projects

    @abc.abstractmethod
    def create_project(self, project: Project) -> None:
        """Persist a new project record."""

    @abc.abstractmethod
    def get_project(self, slug: str) -> Project | None:
        """Return the project for *slug*, or None if absent."""

    @abc.abstractmethod
    def list_projects(self) -> list[Project]:
        """Return all projects sorted by indexed_at descending."""

    @abc.abstractmethod
    def delete_project(self, slug: str) -> bool:
        """Delete project *slug*; return True if deleted, False if absent."""

    # The ONLY ``Project`` fields a partial :meth:`update_project` may write.
    # Deliberately tiny, and not an oversight: every other field is SYSTEM-owned
    # — rebuilt wholesale by the next index run (``pages``/``indexed_at``/
    # ``landing_page_id``/``commit_sha``/``branch``/``maintainer_edited``/
    # ``graph_only``) or part of identity (``slug``/``source``/``host``/
    # ``repo_url``). Writing one here would either be silently clobbered at the
    # next finalize or make the record lie about what was actually indexed.
    # Settings that take effect on the NEXT index belong on ``ProjectSettings``.
    #
    # ``resolution`` is the one member written by an INDEX rather than by a
    # user edit: the graph phase is the only place that knows whether exact
    # cross-file symbol resolution ran, and it runs while the project record
    # for the previous index is still the current one. It is listed here
    # because that write is a partial update of an existing row, not because
    # the field is user-editable — nothing on the settings surface may set it.
    PROJECT_UPDATABLE: frozenset[str] = frozenset({"desc", "resolution"})

    def update_project(self, slug: str, fields: dict[str, Any]) -> Project | None:
        """Apply a partial update to *slug*'s Project; return the new state.

        Cost: ``O(one record)`` — one project read and one upsert of that same
        record, regardless of how much the project has indexed.

        Returns ``None`` when the project is absent. Only keys in
        :data:`PROJECT_UPDATABLE` are honoured — an unknown or ``None`` value is
        ignored, so a caller can hand over a whole PATCH body without pre-filtering
        (mirrors ``agentic_search.store.update_workspace``). ``None`` meaning "not
        supplied" is the OPPOSITE of ``update_job``'s rule, where a named ``None``
        is a value to be written — do not carry one convention onto the other.

        Concrete on the base rather than per-backend: ``create_project`` is an
        UPSERT in both drivers, so read → ``model_copy`` → upsert needs no
        duplicated JSON/Mongo pair that could drift. It is a read-modify-write with
        the same (non-)atomicity as every other Project write in this store.
        """
        project = self.get_project(slug)
        if project is None:
            return None
        updates = {
            key: value
            for key, value in fields.items()
            if key in self.PROJECT_UPDATABLE and value is not None
        }
        if not updates:
            return project
        updated = project.model_copy(update=updates)
        self.create_project(updated)
        return updated

    # Project settings (slug-keyed edit target — see ``ProjectSettings``)
    #
    # Its own surface, NOT the job-keyed submission sidecar: the sidecar records
    # what ONE job ran with (immutable history), while this records what the
    # project is CONFIGURED with (mutable, the PATCH target). Same separation the
    # recovery counter makes for the same reason.

    @abc.abstractmethod
    def save_project_settings(self, slug: str, settings: ProjectSettings) -> None:
        """Persist (upsert) the editable settings record for *slug*."""

    @abc.abstractmethod
    def get_project_settings(self, slug: str) -> ProjectSettings | None:
        """Return *slug*'s settings record, or None when it has never been written.

        ``None`` is a NORMAL state, not an error — the caller falls back to
        the per-job submission scan.
        """

    @abc.abstractmethod
    def delete_project_settings(self, slug: str) -> bool:
        """Delete *slug*'s settings record; return True if one existed.

        Called on project delete so a re-created slug can't inherit the dead
        project's settings (the rule the freshness cache eviction already follows).
        """

    def reap_slug(self, slug: str) -> dict[str, int]:
        """Delete every family this store persists for *slug*; return the counts.

        The exceptions are the three the delete-project ROUTE already owns
        directly: ``wiki_projects``
        (:meth:`delete_project`), ``wiki_settings`` (:meth:`delete_project_settings`),
        and a git credential (``CredentialStore.delete`` — repo-scope ONLY; a
        host-scoped credential is shared by every repo on that host and must
        NEVER cascade off one project's delete, so this method never touches
        credentials at all).

        Everything else a completed or in-flight index could have written under
        *slug* is deleted here: pages, the code graph (nodes/edges/embeddings),
        the entity layer (entities/entity-edges/entity-embeddings/
        recommendations), the memory layer (nodes/edges/embeddings), doc notes,
        the file manifest, the recovery-attempt counter, every indexing job
        (with its plan/resume/submission/session sidecars and its event log),
        and every QA answer for this slug (with its event log). Without this, a
        project delete orphans the large majority of what an index ever wrote —
        it stays reachable by nothing, forever, and a slug reused later inherits
        none of it (the same "clean slate" reasoning behind clearing settings/
        credentials/the freshness cache above, extended to the rest of the store).

        Returns per-family deleted-row counts (mirroring
        :meth:`supersede_graph_artifacts`'s shape) so a caller can report exactly
        what was removed. Idempotent: a second call for an already-reaped slug
        finds nothing and returns all-zero counts.
        """
        raise NotImplementedError("Graph backend is not implemented on this driver")

    # Pages

    @abc.abstractmethod
    def save_page(
        self,
        slug: str,
        page: WikiPage,
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Persist *page* for the project *slug*; overwrites if same page_id.

        ``commit_sha``/``job_id`` attribute the page to its owning index. Unlike
        the graph families, page attribution is a store-internal column, never a
        ``WikiPage`` field — the page IS a console wire type serialized whole, so
        keeping attribution off the model preserves that wire byte-for-byte. Page
        supersession is unaffected: ``wiki_finalize`` already prunes pages to the
        committed plan, so this is provenance, not the supersede mechanism.
        """

    def get_page(self, slug: str, page_id: str) -> WikiPage | None:
        """Return a single wiki page, or None if absent.

        THE single doc-content read seam: every upstream doc reader (the API
        page route, the ``wiki_read_page`` Q&A tool, the MCP ``read_wiki_page``
        facade over the route) funnels through here, so guarding it once makes a
        graph-only project's "no documentation" failure deterministic everywhere.
        A project indexed in graph-only (developer) mode carries
        ``graph_only=True`` and has ZERO pages — reading page content then raises
        :class:`DocumentationUnavailableError` from this ONE place rather than
        returning a confusing ``None``. The driver-specific fetch lives in
        :meth:`_get_page_raw`; this template method only adds the guard. The graph
        endpoint never calls this, so visualisation stays unaffected.
        """
        self._assert_docs_available(slug)
        return self._get_page_raw(slug, page_id)

    @abc.abstractmethod
    def _get_page_raw(self, slug: str, page_id: str) -> WikiPage | None:
        """Driver fetch of a single page (no graph-only guard)."""

    def _assert_docs_available(self, slug: str) -> None:
        """Raise :class:`DocumentationUnavailableError` for a graph-only project.

        Best-effort lookup: a missing/absent project is NOT a graph-only one, so
        the read proceeds (the caller handles the ``None`` page). Only a project
        record present AND flagged ``graph_only`` blocks doc reads.
        """
        from .errors import DocumentationUnavailableError  # noqa: PLC0415

        project = self.get_project(slug)
        if project is not None and getattr(project, "graph_only", False):
            raise DocumentationUnavailableError(slug)

    @abc.abstractmethod
    def list_pages(self, slug: str) -> list[WikiPage]:
        """Return all pages for project *slug*.

        NOT doc-guarded: ``list_pages`` is also the page-id roster used by the
        deterministic internal paths (finalize prune, resume, retriever,
        ``QaFinalizer.tag_page_citations``), so guarding it would break indexing
        and finalize themselves. A graph-only project simply returns ``[]`` here
        (it has no pages); the guard lives on the doc-CONTENT read (:meth:`get_page`).
        """

    def prune_pages(self, slug: str, keep: Iterable[str]) -> int:
        """Drop every page for *slug* whose ``page_id`` is not in *keep*.

        Default impl uses ``list_pages`` + per-page ``delete_page`` so
        backends only need a single primitive. Returns the number of
        pages dropped.
        """
        keep_set = set(keep)
        dropped = 0
        for page in self.list_pages(slug):
            if page.id not in keep_set:
                self.delete_page(slug, page.id)
                dropped += 1
        return dropped

    @abc.abstractmethod
    def delete_page(self, slug: str, page_id: str) -> bool:
        """Delete a single wiki page. Returns True if a page was removed."""

    # Indexing jobs

    @abc.abstractmethod
    def create_job(self, job: IndexingJob) -> None:
        """Persist a new indexing job."""

    @abc.abstractmethod
    def get_job(self, job_id: str) -> IndexingJob | None:
        """Return the indexing job, or None if absent."""

    def update_job(self, job_id: str, **fields: Any) -> IndexingJob:
        """Partially update *job_id* with *fields*; return the updated record.

        Concrete on the base because the rule both drivers owe their callers is
        ONE rule: only the fields the caller NAMED are written. Persisting the
        whole merged document instead turns every write into a read-modify-write
        over every field, so two writers that overlap lose one another's changes
        even when the fields they touch are disjoint. The phase tools write
        progress on a 50ms-to-5s cadence while an HTTP thread writes ``status``,
        so a cancel silently reverting to ``running`` needs no exotic timing to
        reproduce.

        The narrowing lives here; making the narrow write INDIVISIBLE is
        per-backend (:meth:`_write_job_patch`), because a lock and a
        field-scoped update are not the same cure.

        Raises ``KeyError`` when the job is absent and ``ValidationError`` when
        a field is unknown to the model or its value is wrong for it. Naming no
        field at all is a read: there is nothing to narrow, so nothing is
        written.
        """
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        if not fields:
            return job
        return self._write_job_patch(job_id, JobPatch.build(job, fields))

    @abc.abstractmethod
    def _write_job_patch(self, job_id: str, patch: JobPatch) -> IndexingJob:
        """Persist exactly *patch*'s fields onto *job_id*; return the STORED record.

        The backend's one job here is to make "read the record, set these
        fields, put it back" indivisible with respect to another writer, and to
        leave every field the patch does not name exactly as that other writer
        left it. Returns what is actually stored afterwards — concurrent writes
        included — never the caller's locally merged guess at it. Raises
        ``KeyError`` if the job vanished since the caller's read.
        """

    @abc.abstractmethod
    def list_jobs(self, slug: str | None = None) -> list[IndexingJob]:
        """Return all jobs, optionally filtered to *slug*."""

    def latest_job(
        self, slug: str, *, statuses: Iterable[str] | None = None
    ) -> IndexingJob | None:
        """Return *slug*'s most recent job, ordered by ``phase_started_at``.

        The ONE "what is the latest attempt for this slug" answer — a thin
        convenience over :meth:`list_jobs`, which already returns newest-first
        by ``phase_started_at`` (never ``job_id``, a ``uuid4`` hex that sorts
        RANDOMLY with respect to when a job actually ran). *statuses* narrows
        the candidates before taking the first, e.g. ``{"complete"}`` for a
        freshness or QA-source baseline. Returns ``None`` when no job
        (matching *statuses*, if given) exists.
        """
        candidates = self.list_jobs(slug=slug)
        if statuses is not None:
            wanted = set(statuses)
            candidates = [j for j in candidates if j.status in wanted]
        return candidates[0] if candidates else None

    def append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Validate then append one job event; return its monotonic index.

        The discriminated event model is the durable trust boundary: both the
        JSON and Mongo timelines, and therefore SSE, receive one known wire
        shape. Concrete drivers only own their atomic append mechanics.

        Cost: ``O(1)``.
        """
        payload = WikiJobEvent.parse(event).stored_payload()
        return self._append_job_event(job_id, payload)

    def _append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append already-validated *event* with driver-specific atomicity.

        Kept concrete so lightweight store subclasses that implement the
        long-standing public ``append_job_event`` contract remain instantiable.
        Production drivers override this hook; the base has no durable timeline.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def load_job_events(
        self, job_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return job events with idx > *after_idx* (-1 returns all)."""

    @abc.abstractmethod
    def cancel_job(self, job_id: str) -> bool:
        """Cancel *job_id*; return True on first cancel, False if already cancelled."""

    @abc.abstractmethod
    def attach_job_session(self, job_id: str, session_id: str) -> None:
        """Associate a Mewbo session_id with an indexing job (forward mapping)."""

    @abc.abstractmethod
    def get_job_session(self, job_id: str) -> str | None:
        """Return the session_id attached to *job_id*, or None."""

    @abc.abstractmethod
    def find_job_by_session(self, session_id: str) -> str | None:
        """Reverse lookup: return the job_id for *session_id*, or None."""

    # Job plan + extra metadata (not part of IndexingJob schema)

    @abc.abstractmethod
    def save_job_plan(self, job_id: str, plan: list[dict[str, Any]]) -> None:
        """Persist the page-plan list for *job_id*; overwrites any previous plan."""

    @abc.abstractmethod
    def get_job_plan(self, job_id: str) -> list[dict[str, Any]] | None:
        """Return the page-plan list, or None if no plan has been committed yet."""

    # Resume sidecar (checkpoint-aware recovery). A tiny dict computed
    # ONCE by ``ResumePlan.build`` at resume time; rebuilt cheaply per tool call
    # via ``ResumePlan.from_persisted`` so the phase skip-guards never re-query
    # the graph. Concrete defaults (no-op / None) so a backend that never persists
    # it simply degrades to a full rebuild on resume — never a crash.

    def save_resume_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the resume-plan dict for *job_id*; overwrites any previous one."""
        raise NotImplementedError

    def get_resume_plan(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted resume-plan dict, or None if the job isn't resuming."""
        return None

    # Act sidecar (the scoped refresh's second stage). ONE record carrying the
    # narrowed page-id work-list AND which stage the job reached, because the
    # two are read together and a job cannot express the second anywhere else:
    # ``refresh_decision`` is stamped once, before the run starts, so it can say
    # WHICH path a refresh took but never HOW FAR it got. Without this a job
    # that died in stage 2 re-ran clone + scan + the whole delta pass on resume.
    # Concrete defaults (no-op / None) for the same reason the resume sidecar
    # has them: a backend that never persists it degrades to re-driving stage 1,
    # which is the behaviour that existed before — never a crash.

    def save_act_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the act-stage record for *job_id*; overwrites any previous one."""
        raise NotImplementedError

    def get_act_plan(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted act-stage record, or None if stage 1 hasn't finished."""
        return None

    @abc.abstractmethod
    def get_job_submitted_count(self, job_id: str) -> int:
        """Return the number of pages submitted so far for *job_id*."""

    @abc.abstractmethod
    def claim_job_page(self, slug: str, job_id: str, page_id: str) -> PageClaim:
        """Atomically record *page_id* as written by *job_id*; return the claim.

        The submitted-pages counter belongs to the JOB, so its dedup key has to
        be the job's OWN set of claimed ids. Keying on the slug instead — "does a
        page with this id already exist for the slug" — answers yes for every
        page a PREVIOUS index of the same repository wrote, so a refresh would
        count zero new pages, emit no ``page_committed`` events, and leave the
        progress bar dead for the whole re-index.

        The returned count is the SIZE of that set, never a free-running
        increment. A counter that only ever counts distinct pages cannot drift
        past what the job actually wrote, where a read-then-``$inc`` can: a
        resumed job whose counter carried over from an earlier attempt reports
        90 pages written against a 50-page plan.

        A job with no claim record seeds its set from page attribution
        (:meth:`page_ids_for_job`) on first touch, so an interrupted index
        resumes with its earlier pages counted rather than from zero.

        Raises ``KeyError`` when *job_id* is unknown.
        """

    @abc.abstractmethod
    def get_job_page_ids(self, slug: str, job_id: str) -> frozenset[str]:
        """Return the page ids *job_id* wrote — its claim set, else attribution.

        The fallback is what makes a PRE-EXISTING interrupted job resumable: its
        meta carries a bare counter and no claim record, so reading the claim
        alone reported nothing done and the resume regenerated every page it had
        already written correctly — the exact waste the claim exists to prevent.
        """

    @abc.abstractmethod
    def page_ids_for_job(self, slug: str, job_id: str) -> frozenset[str]:
        """Page ids under *slug* whose stored attribution names *job_id*.

        ``save_page`` stamps attribution independently of the claim record,
        which is why this can answer for a job the claim record cannot.
        """

    @abc.abstractmethod
    def save_job_submission(self, job_id: str, submission: dict[str, Any]) -> None:
        """Persist the wizard submission dict for *job_id* (token must be absent)."""

    @abc.abstractmethod
    def get_job_submission(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted submission dict, or None if not yet saved."""

    # Repository credentials (isolated, per-slug, plaintext-at-rest)

    @abc.abstractmethod
    def save_credentials(self, slug: str, blob: dict[str, Any]) -> None:
        """Persist the (already encoded) credential *blob* for *slug*; overwrite."""

    @abc.abstractmethod
    def get_credentials(self, slug: str) -> dict[str, Any] | None:
        """Return the encoded credential blob for *slug*, or None if absent."""

    @abc.abstractmethod
    def delete_credentials(self, slug: str) -> bool:
        """Delete *slug*'s credential; return True if one was removed, else False."""

    @abc.abstractmethod
    def list_credentials(self) -> dict[str, dict[str, Any]]:
        """Return every stored credential blob keyed by scope (slug or bare host).

        The management surface (``CredentialStore.list`` → the ``/v1/git/credentials``
        route) reads this. Values are the raw encoded blobs; the caller decodes +
        redacts. The scope key is a full slug (``host/owner/repo``) or a bare host.
        """

    # Restart-recovery counter (slug-keyed, isolated from the submission sidecar)

    @abc.abstractmethod
    def get_recovery_attempts(self, slug: str) -> int:
        """Return the recovery-attempt count for *slug* (0 if never recovered)."""

    @abc.abstractmethod
    def bump_recovery_attempts(self, slug: str) -> int:
        """Atomically increment *slug*'s recovery counter; return the new value.

        Slug-keyed (not job-keyed) so the cap bounds re-drives across recovery
        generations / new job_ids. Lives on its OWN persistent surface so it
        never pollutes the wizard-submission sidecar (which validates strictly
        as a ``WizardSubmission``).
        """

    def reset_recovery_attempts(self, slug: str) -> None:
        """Clear *slug*'s recovery counter (a user-initiated resume gets a fresh budget).

        A human asking to retry an index must not be blocked by prior automatic
        re-drives, so the manual resume path resets the auto-recovery cap. Concrete
        default no-op so a backend that never tracks the counter is unaffected.
        """

    # QA

    @abc.abstractmethod
    def save_qa(self, answer: QaAnswer) -> None:
        """Persist a QA answer record. Use ONLY to create it (resets bookkeeping)."""

    @abc.abstractmethod
    def update_qa_fields(self, answer: QaAnswer) -> None:
        """Update a QA answer's content fields in place — NON-destructive.

        Persists every ``QaAnswer`` field but MUST NOT disturb store bookkeeping
        that some backends pack alongside the record: the ``event_count`` idx
        counter and the ``session_id`` mapping. ``save_qa`` does a FULL replace,
        which on Mongo resets ``event_count`` to 0 (so the next ``append_qa_event``
        collides at idx 0) AND drops ``session_id`` (breaking
        ``find_qa_by_session``). Mid-stream writers (``QaFinalizer``) MUST use
        this; ``save_qa`` is for creation only. (The JSON backend keeps session +
        events in separate files, so for it this is just an answer.json rewrite —
        the divergence is why a JSON-only test cannot catch the Mongo failure.)
        """

    @abc.abstractmethod
    def get_qa(self, answer_id: str) -> QaAnswer | None:
        """Return the QA answer, or None if absent."""

    @abc.abstractmethod
    def list_qa(self, status: str | None = None) -> list[QaAnswer]:
        """Return all QA answers, optionally filtered to *status*.

        Boot-time/offline use only (mirrors :meth:`list_jobs`) — an
        unfiltered call reads every persisted answer, so a caller on an
        interactive path must narrow with *status* rather than filtering the
        full list in Python.
        """

    @abc.abstractmethod
    def attach_qa_session(self, answer_id: str, session_id: str) -> None:
        """Associate a Mewbo session_id with a QA answer (forward mapping)."""

    @abc.abstractmethod
    def get_qa_session(self, answer_id: str) -> str | None:
        """Return the session_id attached to *answer_id*, or None."""

    @abc.abstractmethod
    def find_qa_by_session(self, session_id: str) -> str | None:
        """Reverse lookup: return the answer_id for *session_id*, or None."""

    @abc.abstractmethod
    def append_qa_event(self, answer_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the QA event log; return the monotonic idx."""

    @abc.abstractmethod
    def load_qa_events(
        self, answer_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return QA events with idx > *after_idx* (-1 returns all)."""

    # Graph + embeddings (raise NotImplementedError in v1)
    #
    # ``commit_sha``/``job_id`` are the per-job/commit attribution the store
    # stamps onto every artifact (see ``GraphNodeBase``). A caller that owns a
    # job passes them so the write is attributed to the commit it indexed; a
    # commit-less path (catalog ingest) omits them and the row is stamped
    # ``None``. Keyword-only + defaulted so a caller that owns no job can omit them.

    @staticmethod
    def _stamp_attribution(item: _M, commit_sha: str | None, job_id: str | None) -> _M:
        """Return *item* carrying the write's ``commit_sha``/``job_id``.

        A no-op (identity) when neither is supplied — a commit-less catalog or
        Q&A write leaves the row ``None``-stamped, which is what supersede treats
        as "not a per-commit snapshot" and preserves. Otherwise a frozen-safe
        ``model_copy`` overwrites both, so the persisted attribution is the
        writer's, never whatever a constructed model happened to carry.
        """
        if commit_sha is None and job_id is None:
            return item
        return item.model_copy(update={"commit_sha": commit_sha, "job_id": job_id})

    def upsert_nodes(
        self,
        slug: str,
        nodes: Iterable[GraphNode],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upsert code-graph nodes, reporting completed persistence batches."""
        raise NotImplementedError("Graph backend is not implemented on this driver")

    def upsert_edges(
        self,
        slug: str,
        edges: Iterable[GraphEdge],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upsert code-graph edges, reporting completed persistence batches."""
        raise NotImplementedError("Graph backend is not implemented on this driver")

    def upsert_embeddings(
        self,
        slug: str,
        items: Iterable[Embedding],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert dense embedding vectors."""
        raise NotImplementedError("Embeddings are not implemented on this driver")

    def live_scope(self, slug: str) -> CommitScope:
        """The generation *slug*'s readers should see: the project's own commit.

        A project row carries the commit its last completed index built from,
        so that — not the union of every commit ever indexed — is what "the
        code as it is now" means. Lives here because the store is what holds
        the project row; every graph reader already has one, so nobody has to
        thread a commit sha through their own call chain to read correctly.

        A project with no ``commit_sha`` (a commit-less catalog ingestion, or a
        slug with no project row yet) has exactly one generation, so scoping it
        could only be a way to get it wrong — the union and the live generation
        are the same set, and ``every()`` says so without asserting a commit
        that does not exist.

        Cost: ``O(one record)``.
        """
        project = self.get_project(slug)
        if project is None or project.commit_sha is None:
            return CommitScope.every()
        return CommitScope.at(project.commit_sha)

    def query_graph(
        self,
        slug: str,
        *,
        scope: CommitScope,
        node_type: str | None = None,
        name_match: str | None = None,
        neighbors_of: str | None = None,
        node_ids: Collection[str] | None = None,
    ) -> list[GraphNode]:
        """Query the code graph for *slug*, restricted to *scope*'s generation.

        *node_ids* fetches an explicit set by id. It exists so a caller holding
        a bounded list of ids — the knowledge-graph view repairing entity
        anchors that point into a superseded generation — can look exactly
        those up instead of reading a whole generation to find a handful. An
        empty collection returns nothing; ``None`` means "no id filter".

        *scope* is REQUIRED and has no default on purpose. This store holds the
        UNION of every commit ever indexed for a slug, so "which generation"
        has no safe default — and a default of ``CommitScope.every()`` would be
        precisely the fail-open filter the root guidance forbids: a caller that
        forgot to scope would silently read every generation and still return
        ``200``. Required means a missed call site is a typecheck failure
        instead. See :class:`CommitScope` for why this is not a ``commit_sha``
        parameter (``count_graph_nodes`` already owns that name with the
        opposite meaning for ``None``).

        Cost: ``O(nodes for the slug in scope)`` — a read of one project's
        graph generation, not of all history.
        """
        raise NotImplementedError("Graph backend is not implemented on this driver")

    def count_graph_nodes(self, slug: str, *, commit_sha: str | None) -> int:
        """Count nodes for *slug* built by exactly *commit_sha* (``None`` matches None).

        The commit-scoped count the resume skip predicate keys on: "the graph
        for THIS commit is built" is ``count_graph_nodes(slug, commit_sha=X) >
        0``.

        ``commit_sha`` here is an EXACT match — ``None`` counts the rows stamped
        NULL, it does not mean "any commit". :class:`CommitScope` exists to keep
        that convention unambiguous on the read methods, which is why
        ``query_graph`` takes a scope rather than re-using this parameter name
        with the opposite sense; ``count_graph_nodes(slug, commit_sha=X)`` and
        ``len(query_graph(slug, scope=CommitScope.at(X)))`` agree by
        construction.
        """
        raise NotImplementedError("Graph backend is not implemented on this driver")

    def supersede_graph_artifacts(
        self, slug: str, *, keep_commit_sha: str
    ) -> dict[str, int]:
        """Reap prior-commit graph + entity artifacts once *keep_commit_sha* completes.

        Deletes every node/edge/embedding/entity/entity-edge/entity-embedding for
        *slug* whose ``commit_sha`` is a REAL value other than *keep_commit_sha*.
        Rows stamped ``None`` are PRESERVED — for entities that is a QA-minted or
        pre-isolation record (accretive memory, not a per-commit snapshot); for
        code nodes there are none on a git slug (every index stamps its commit).
        Returns per-collection delete counts. Idempotent: a second call for the
        same *keep_commit_sha* finds nothing to reap.
        """
        raise NotImplementedError("Graph backend is not implemented on this driver")

    def restamp_graph_artifacts(
        self, slug: str, *, from_commit: str, to_commit: str
    ) -> dict[str, int]:
        """Carry *from_commit*'s surviving artifacts forward onto *to_commit*.

        Concrete-raising rather than ``@abc.abstractmethod``, matching its
        sibling ``supersede_graph_artifacts`` and the rest of this graph family:
        an abstract method here is inherited by every partial test double of
        this base and stops it being instantiable at all, which turns a new
        store capability into a broad, unrelated test failure.

        The counterpart a SCOPED re-index needs and a full one does not. A full
        index re-stamps every artifact by rewriting them all, so afterwards one
        generation describes the whole slug and ``live_scope`` finds everything.
        An incremental refresh re-stamps only what it re-parsed, so without this
        the untouched majority keeps the PREVIOUS commit while the project row
        advances — and since ``live_scope`` is ``at(project.commit_sha)``, every
        reader (the graph view, the retriever, the agent graph tools) would see
        only the handful of files that happened to change. Not deleted, not
        erroring: invisible. That is the failure this method exists to prevent,
        and it is the precondition the delta indexer's own unscoped reads name.

        **The semantic is a claim, and it is a true one:** an artifact built
        from a file that did NOT change describes *to_commit* just as accurately
        as it described *from_commit*, so moving its stamp forward asserts
        nothing false.

        **Why this is a MOVE from a named generation rather than "stamp
        everything".** The store can hold generations older than *from_commit* —
        a supersede that never ran, or the field-absent rows the isolation
        backfill exists for. Those describe code that is genuinely gone.
        Blanket-stamping them onto *to_commit* would resurrect deleted symbols
        into the live view, which is a worse bug than the one being fixed. Rows
        stamped ``None`` are likewise left alone, matching
        ``supersede_graph_artifacts``' preserve rule.

        Returns per-family moved counts. Idempotent: a second call finds nothing
        left at *from_commit*.

        Cost: ``O(artifacts at from_commit)`` — an offline finalize step.
        """
        raise NotImplementedError

    def list_edges(self, slug: str, *, scope: CommitScope) -> list[GraphEdge]:
        """Return *slug*'s edges for *scope*'s generation (graph-viewer endpoint).

        *scope* is required for the same reason as on ``query_graph`` — and it
        must agree with the scope the nodes were read under, or the view is
        assembled from edges whose endpoints belong to a different generation.
        """
        raise NotImplementedError("Graph backend is not implemented on this driver")

    def vector_search(
        self, slug: str, qvec: list[float], k: int = 10
    ) -> list[Embedding]:
        """Nearest-neighbour vector search."""
        raise NotImplementedError("Embeddings are not implemented on this driver")

    # Scoped graph deletes (used by the incremental GraphDeltaIndexer)

    def delete_nodes_by_file(self, slug: str, file: str) -> int:
        """Delete every code node in *file* AND its vectors; return the NODE count.

        The embedding cascade is part of this method rather than a second call
        the caller makes first, because :class:`Embedding` carries no ``file``
        field: a vector is reachable only through the node it points at, so once
        that node is deleted the vector can no longer be found by file, by
        commit, or by anything else — it is simply unreachable, unreapable, and
        still scoreable by :meth:`vector_search`. Cascading here makes "no
        orphaned vectors" a property of the store instead of a call-ordering
        discipline every future caller has to rediscover.

        The return value stays the NODE count so the delete reads the same as
        every other scoped delete on this class.
        """
        raise NotImplementedError("Memory layer methods land in the memory store")

    def delete_edges_by_source_file(self, slug: str, file: str) -> int:
        """Delete edges originating from any node in *file*; return count.

        "Originating" = the edge ``source`` node_id belongs to a node whose
        ``file`` is *file*. Call BEFORE :meth:`delete_nodes_by_file` for the
        same file so the source nodes are still present to resolve.
        """
        raise NotImplementedError("Memory layer methods land in the memory store")

    # Memory layer — nodes / edges / embeddings (multiplex overlay)
    #
    # Default impls raise NotImplementedError so a backend opts in by
    # overriding (no separate MemoryStoreBase ABC — KISS). Both shipping
    # drivers (JSON, Mongo) implement the full surface.

    def upsert_memory_nodes(self, slug: str, nodes: Iterable[MemoryNode]) -> None:
        """Upsert memory nodes; dedup by ``node_id``."""
        raise NotImplementedError

    def get_memory_node(self, slug: str, node_id: str) -> MemoryNode | None:
        """Return a single memory node, or None if absent."""
        raise NotImplementedError

    def delete_memory_node(self, slug: str, node_id: str) -> bool:
        """Delete a memory node + its embedding; return True if one was removed.

        Edges are NOT touched (callers invalidate them separately so history
        survives). Used when a merge supersedes a note under a new identity.
        """
        raise NotImplementedError

    def query_memory(
        self, slug: str, *, filt: MemoryFilter | None = None
    ) -> list[MemoryNode]:
        """Return memory nodes matching *filt*'s node-level facets."""
        raise NotImplementedError

    def upsert_memory_edges(self, slug: str, edges: Iterable[MemoryEdge]) -> None:
        """Upsert memory edges; dedup by ``(source, target, type)``."""
        raise NotImplementedError

    def list_memory_edges(
        self,
        slug: str,
        *,
        node_id: str | None = None,
        include_invalidated: bool = False,
    ) -> list[MemoryEdge]:
        """Return memory edges, optionally scoped to ``source == node_id``.

        Invalidated edges (``invalid_at`` set) are excluded unless
        *include_invalidated* is True.
        """
        raise NotImplementedError

    def memories_anchored_to(
        self,
        slug: str,
        entity_keys: Iterable[EntityKey],
        *,
        include_invalidated: bool = False,
    ) -> list[str]:
        """Reverse ANCHORS lookup: entity_keys → distinct memory node_ids."""
        raise NotImplementedError

    def upsert_memory_embeddings(
        self, slug: str, items: Iterable[MemoryEmbedding]
    ) -> None:
        """Upsert memory embedding vectors; dedup by ``node_id``."""
        raise NotImplementedError

    def memory_vector_search(
        self,
        slug: str,
        qvec: list[float],
        k: int = 10,
        *,
        filt: MemoryFilter | None = None,
    ) -> list[MemoryEmbedding]:
        """Top-k memory embeddings by cosine, after applying *filt*.

        Scale seam — keep signature stable. v1 is brute-force cosine; IVF /
        Matryoshka / quantization slot in here without touching callers.
        """
        raise NotImplementedError

    def _live_anchored_ids(self, slug: str) -> set[str]:
        """Memory node_ids with ≥1 live ANCHORS edge (validity gate)."""
        raise NotImplementedError

    @staticmethod
    def _rank_embeddings(
        pool: list[_V], qvec: list[float], k: int
    ) -> list[_V]:
        """Cosine-rank a pre-loaded embedding pool and return the top-k.

        The single cosine-sort core shared by memory AND entity vector search:
        each driver loads (and, for memory, facet-filters) its own pool, then
        delegates here so scoring/ordering can never desync across families or
        backends. Generic over any row with a ``vector`` (`_HasVector`).
        """
        if not pool:
            return []
        scored = [(emb, _cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

    def _rank_memory(
        self,
        slug: str,
        pool: list[MemoryEmbedding],
        qvec: list[float],
        k: int,
        filt: MemoryFilter | None,
    ) -> list[MemoryEmbedding]:
        """Facet-filter then cosine-rank a backend-loaded memory pool (top-k).

        The single ranking core both memory drivers share: each loads its own
        pool, then delegates here so facet/validity intersection can never
        desync across backends. The cosine-sort itself is `_rank_embeddings`.
        """
        if not pool:
            return []
        if filt is not None:
            # Only load + facet-filter nodes when a facet is actually set; the
            # common path (validity only) skips that O(N) node scan.
            allowed: set[str] | None = None
            if filt.corpus or filt.source or filt.kind or filt.labels:
                allowed = {n.node_id for n in self.query_memory(slug, filt=filt)}
            if filt.exclude_invalidated:
                live = self._live_anchored_ids(slug)
                allowed = live if allowed is None else (allowed & live)
            if allowed is not None:
                pool = [e for e in pool if e.node_id in allowed]
        return self._rank_embeddings(pool, qvec, k)

    # Documentation-page notes (docs-as-multiplex-nodes)

    def upsert_doc_notes(self, slug: str, notes: Iterable[DocPageNote]) -> None:
        """Upsert doc-page notes; dedup by ``page_id``."""
        raise NotImplementedError

    def get_doc_note(self, slug: str, page_id: str) -> DocPageNote | None:
        """Return a single doc-page note, or None if absent."""
        raise NotImplementedError

    def list_doc_notes(self, slug: str) -> list[DocPageNote]:
        """Return every doc-page note for *slug*."""
        raise NotImplementedError

    def delete_doc_note(self, slug: str, page_id: str) -> bool:
        """Delete a doc-page note; return True if one was removed."""
        raise NotImplementedError

    # File manifest (incremental retract index)

    def upsert_file_manifest(
        self, slug: str, entries: Iterable[FileManifest]
    ) -> None:
        """Upsert per-file manifest entries; dedup by ``path``."""
        raise NotImplementedError

    def get_file_manifest(self, slug: str, path: str) -> FileManifest | None:
        """Return a single file-manifest entry, or None if absent."""
        raise NotImplementedError

    def list_file_manifest(self, slug: str) -> list[FileManifest]:
        """Return every file-manifest entry for *slug*."""
        raise NotImplementedError

    def delete_file_manifest(self, slug: str, path: str) -> bool:
        """Delete a file-manifest entry; return True if one was removed."""
        raise NotImplementedError

    # Abstract-entity layer (multiplex overlay — same opt-in pattern as memory)
    #
    # Default impls raise NotImplementedError so a backend opts in by
    # overriding (no separate EntityStore ABC — KISS, one store). Both shipping
    # drivers (JSON, Mongo) implement the full surface; the base stays a
    # default-raise (not @abstractmethod) so existing partial test doubles keep
    # instantiating, exactly like the memory layer above.

    def upsert_entities(
        self,
        slug: str,
        entities: Iterable[Entity],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entities; dedup by ``id`` (= sha1(normalized_name|type))."""
        raise NotImplementedError

    def get_entity(self, slug: str, entity_id: str) -> Entity | None:
        """Return a single entity, or None if absent."""
        raise NotImplementedError

    def query_entities(
        self, slug: str, *, filt: EntityFilter | None = None
    ) -> list[Entity]:
        """Return entities matching *filt*'s facets (no filter ⇒ all)."""
        raise NotImplementedError

    def count_entities(self, slug: str, *, commit_sha: str | None) -> int:
        """Count entities for *slug* minted by exactly *commit_sha* (``None`` matches None).

        The enrich-phase analogue of :meth:`count_graph_nodes`: "entities for
        THIS commit are minted" is ``count_entities(slug, commit_sha=X) > 0``,
        which the resume skip predicate needs instead of the union count.
        """
        raise NotImplementedError

    def upsert_entity_embeddings(
        self,
        slug: str,
        items: Iterable[EntityEmbedding],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entity embedding vectors; dedup by ``entity_id``."""
        raise NotImplementedError

    def entity_vector_search(
        self, slug: str, qvec: list[float], k: int = 10
    ) -> list[EntityEmbedding]:
        """Top-k entity embeddings by cosine (the ANN block seam for ER)."""
        raise NotImplementedError

    def upsert_entity_edges(
        self,
        slug: str,
        edges: Iterable[EntityRelation],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entity relations; dedup by ``id`` (= source|type|target)."""
        raise NotImplementedError

    def list_entity_edges(
        self, slug: str, *, source_id: str | None = None
    ) -> list[EntityRelation]:
        """Return entity relations, optionally scoped to ``source_id``."""
        raise NotImplementedError

    def save_entity_recommendation(
        self, slug: str, rec: EntityRecommendation
    ) -> None:
        """Upsert a resolution-recommendation record (a prior for the next pass).

        Keyed on ``rec.id`` (deterministic over action + sorted subjects + type),
        NOT appended: these records are read back as priors by ``EntityResolver``,
        so an unkeyed insert let a replayed enrich pass state the same prior
        twice and re-weight the ladder purely by having run again.
        """
        raise NotImplementedError

    def get_entity_recommendations(self, slug: str) -> list[EntityRecommendation]:
        """Return every persisted entity recommendation for *slug*."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# JSON / filesystem driver
# ---------------------------------------------------------------------------


class JsonWikiStore(WikiStoreBase):
    """File-backed implementation under ``$MEWBO_HOME/wiki/`` (or a custom root)."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        """Initialise and create the directory tree."""
        if root_dir is None:
            home = get_config_value("runtime", "cache_dir", default="") or ".mewbo"
            root_dir = Path(home) / "wiki"
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("projects", "pages", "jobs", "qa"):
            (self.root_dir / sub).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- Private helpers -----------------------------------------------------

    def _save_json(self, path: Path, model: Any) -> None:
        """Persist a Pydantic model as JSON (by_alias, mode=json)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            model.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
        )

    def _load_json(self, path: Path, model_cls: type[_M]) -> _M | None:
        """Load a Pydantic model from JSON, returning None if missing."""
        if not path.exists():
            return None
        try:
            return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("Skipping malformed JSON at {}", path)
            return None

    def _event_path(self, scope: str, owner_id: str) -> Path:
        """Return path to the JSONL event log for jobs or qa."""
        return self.root_dir / scope / owner_id / "events.jsonl"

    def _append_event(
        self, scope: str, owner_id: str, event_dict: dict[str, Any]
    ) -> int:
        """Append event_dict to the JSONL log; return its monotonic idx."""
        with self._lock:
            path = self._event_path(scope, owner_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            idx = 0
            if path.exists():
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                idx = len(lines)
            payload = {**event_dict, "idx": idx}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
            return idx

    def _load_events(
        self, scope: str, owner_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Load events from JSONL; filter to idx > after_idx."""
        path = self._event_path(scope, owner_id)
        if not path.exists():
            return []
        results: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Skipping malformed event line in {}", path)
                continue
            if rec.get("idx", -1) > after_idx:
                results.append(rec)
        return results

    # -- Projects ------------------------------------------------------------

    def _project_path(self, slug: str) -> Path:
        """Filesystem path for a project JSON file."""
        return self.root_dir / "projects" / f"{_slug_to_path(slug)}.json"

    def create_project(self, project: Project) -> None:
        """Persist a new project record."""
        self._save_json(self._project_path(project.slug), project)

    def get_project(self, slug: str) -> Project | None:
        """Return the project for *slug*, or None if absent."""
        return self._load_json(self._project_path(slug), Project)

    def list_projects(self) -> list[Project]:
        """Return all projects sorted by indexed_at descending."""
        projects: list[Project] = []
        for p in (self.root_dir / "projects").glob("*.json"):
            proj = self._load_json(p, Project)
            if proj is not None:
                projects.append(proj)
        return sorted(projects, key=lambda pr: pr.indexed_at, reverse=True)

    def delete_project(self, slug: str) -> bool:
        """Delete project *slug*; return True if deleted, False if absent."""
        path = self._project_path(slug)
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- Project settings (slug-keyed sidecar) -------------------------------

    def _settings_path(self, slug: str) -> Path:
        """Filesystem path for a slug's editable settings record."""
        d = self.root_dir / "settings"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{_slug_to_path(slug)}.json"

    def save_project_settings(self, slug: str, settings: ProjectSettings) -> None:
        """Persist (upsert) the editable settings record for *slug*."""
        self._save_json(self._settings_path(slug), settings)

    def get_project_settings(self, slug: str) -> ProjectSettings | None:
        """Return *slug*'s settings record, or None when never written."""
        return self._load_json(self._settings_path(slug), ProjectSettings)

    def delete_project_settings(self, slug: str) -> bool:
        """Delete *slug*'s settings file; return True if one existed."""
        path = self._settings_path(slug)
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- Whole-slug reap (see WikiStoreBase.reap_slug for the family list) ---

    def reap_slug(self, slug: str) -> dict[str, int]:
        """See ``WikiStoreBase.reap_slug``.

        Counts are taken from each JSONL/file BEFORE its owning directory is
        removed, so a family's number here means the same thing as the Mongo
        driver's ``delete_many().deleted_count`` — one count per row, not "the
        directory existed". Graph/entity/memory each live under ONE directory
        (``_graph_dir``/``_memory_dir``), and pages under another
        (``_pages_dir``), so counting + removing those collapses into directory
        operations rather than per-file bookkeeping. Jobs and QA are id-keyed,
        not slug-keyed: their ids are gathered FIRST, before anything is
        deleted, then each owning directory is removed by id.
        """
        with self._lock:
            job_ids = [j.job_id for j in self.list_jobs(slug)]
            answer_ids = self._qa_answer_ids_for_slug(slug)

            graph_dir = self._graph_dir(slug)
            counts: dict[str, int] = {
                "graph_nodes": self._count_jsonl_lines(graph_dir / "nodes.jsonl"),
                "graph_edges": self._count_jsonl_lines(graph_dir / "edges.jsonl"),
                "embeddings": self._count_jsonl_lines(graph_dir / "embeddings.jsonl"),
            }
            self._rmdir_if_exists(graph_dir)

            mem_dir = self._memory_dir(slug)
            counts.update({
                "entities": self._count_jsonl_lines(mem_dir / "entities.jsonl"),
                "entity_edges": self._count_jsonl_lines(mem_dir / "entity_edges.jsonl"),
                "entity_embeddings": self._count_jsonl_lines(
                    mem_dir / "entity_embeddings.jsonl"
                ),
                "entity_recommendations": self._count_jsonl_lines(
                    mem_dir / "entity_recommendations.jsonl"
                ),
                "memory_nodes": self._count_jsonl_lines(mem_dir / "nodes.jsonl"),
                "memory_edges": self._count_jsonl_lines(mem_dir / "edges.jsonl"),
                "memory_embeddings": self._count_jsonl_lines(mem_dir / "embeddings.jsonl"),
                "doc_notes": self._count_jsonl_lines(mem_dir / "docs.jsonl"),
                "file_manifest": self._count_jsonl_lines(mem_dir / "manifest.jsonl"),
            })
            self._rmdir_if_exists(mem_dir)

            pages_dir = self._pages_dir(slug)
            counts["pages"] = (
                sum(
                    1
                    for p in pages_dir.glob("*.json")
                    if p.name not in ("_index.json", "_attribution.json")
                )
                if pages_dir.exists()
                else 0
            )
            self._rmdir_if_exists(pages_dir)

            recovery_path = self._recovery_path(slug)
            counts["recovery"] = 1 if recovery_path.exists() else 0
            if counts["recovery"]:
                recovery_path.unlink()

            counts["jobs"] = len(job_ids)
            counts["job_events"] = sum(
                self._count_jsonl_lines(self._event_path("jobs", jid)) for jid in job_ids
            )
            for job_id in job_ids:
                self._rmdir_if_exists(self._job_dir(job_id))

            counts["qa"] = len(answer_ids)
            counts["qa_events"] = sum(
                self._count_jsonl_lines(self._event_path("qa", aid)) for aid in answer_ids
            )
            for answer_id in answer_ids:
                self._rmdir_if_exists(self._qa_dir(answer_id))

        return counts

    def _qa_answer_ids_for_slug(self, slug: str) -> list[str]:
        """Scan ``qa/<answer_id>/answer.json`` for the ones belonging to *slug*.

        QA answers are id-keyed, not slug-keyed, so there is no single directory
        to remove wholesale the way graph/memory/pages allow — every answer_id
        for this slug has to be found via its own record first.
        """
        qa_root = self.root_dir / "qa"
        if not qa_root.exists():
            return []
        ids: list[str] = []
        for qa_dir in qa_root.iterdir():
            if not qa_dir.is_dir():
                continue
            answer = self._load_json(qa_dir / "answer.json", QaAnswer)
            if answer is not None and answer.slug == slug:
                ids.append(qa_dir.name)
        return ids

    @staticmethod
    def _count_jsonl_lines(path: Path) -> int:
        """Count non-empty lines in a JSONL file; 0 if the file is absent."""
        if not path.exists():
            return 0
        return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())

    @staticmethod
    def _rmdir_if_exists(path: Path) -> None:
        """Remove a directory tree if present; no-op if it was already gone."""
        if path.exists():
            shutil.rmtree(path)

    # -- Pages ---------------------------------------------------------------

    def _pages_dir(self, slug: str) -> Path:
        """Directory containing all pages for *slug*."""
        return self.root_dir / "pages" / _slug_to_path(slug)

    def _page_path(self, slug: str, page_id: str) -> Path:
        """Filesystem path for a page JSON file."""
        return self._pages_dir(slug) / f"{_slug_to_path(page_id)}.json"

    def _index_path(self, slug: str) -> Path:
        """Filesystem path for the page-id→title index."""
        return self._pages_dir(slug) / "_index.json"

    def _attribution_path(self, slug: str) -> Path:
        """Filesystem path for the page-id→{commit_sha,job_id} attribution sidecar.

        Page attribution rides a sidecar rather than the page JSON so the
        persisted ``WikiPage`` (an ``extra="forbid"`` console wire type) stays
        byte-identical — the same reason the Mongo driver keeps it a store column.
        """
        return self._pages_dir(slug) / "_attribution.json"

    def _load_index(self, slug: str) -> dict[str, str]:
        """Load the page-id→title index; returns {} if absent."""
        idx_path = self._index_path(slug)
        if not idx_path.exists():
            return {}
        try:
            return json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_attribution(self, slug: str) -> dict[str, dict[str, Any]]:
        """Load the page attribution sidecar; returns {} if absent/unreadable."""
        path = self._attribution_path(slug)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_page(
        self,
        slug: str,
        page: WikiPage,
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Persist *page* for the project *slug*; overwrites if same page_id."""
        pages_dir = self._pages_dir(slug)
        pages_dir.mkdir(parents=True, exist_ok=True)
        self._save_json(self._page_path(slug, page.id), page)
        index = self._load_index(slug)
        index[page.id] = page.title
        self._index_path(slug).write_text(json.dumps(index, indent=2), encoding="utf-8")
        attribution = self._load_attribution(slug)
        attribution[page.id] = {"commit_sha": commit_sha, "job_id": job_id}
        self._attribution_path(slug).write_text(
            json.dumps(attribution, indent=2), encoding="utf-8"
        )

    def _get_page_raw(self, slug: str, page_id: str) -> WikiPage | None:
        """Return a single wiki page, or None if absent (no doc-guard)."""
        return self._load_json(self._page_path(slug, page_id), WikiPage)

    def list_pages(self, slug: str) -> list[WikiPage]:
        """Return all pages for project *slug*."""
        pages_dir = self._pages_dir(slug)
        if not pages_dir.exists():
            return []
        pages: list[WikiPage] = []
        for p in pages_dir.glob("*.json"):
            if p.name in ("_index.json", "_attribution.json"):
                continue
            page = self._load_json(p, WikiPage)
            if page is not None:
                pages.append(page)
        return pages

    def delete_page(self, slug: str, page_id: str) -> bool:
        """Delete a single page on disk + drop it from the index."""
        path = self._page_path(slug, page_id)
        removed = path.exists()
        if removed:
            path.unlink()
        index = self._load_index(slug)
        if index.pop(page_id, None) is not None:
            self._index_path(slug).write_text(
                json.dumps(index, indent=2), encoding="utf-8"
            )
            removed = True
        return removed

    # -- Indexing jobs -------------------------------------------------------

    def _job_dir(self, job_id: str) -> Path:
        """Directory for a job's artefacts."""
        return self.root_dir / "jobs" / job_id

    def _job_path(self, job_id: str) -> Path:
        """Filesystem path for a job JSON file."""
        return self._job_dir(job_id) / "job.json"

    def _session_path(self, job_id: str) -> Path:
        """Filesystem path for the session-id text file."""
        return self._job_dir(job_id) / "session.txt"

    def create_job(self, job: IndexingJob) -> None:
        """Persist a new indexing job."""
        self._job_dir(job.job_id).mkdir(parents=True, exist_ok=True)
        self._save_json(self._job_path(job.job_id), job)

    def get_job(self, job_id: str) -> IndexingJob | None:
        """Return the indexing job, or None if absent."""
        return self._load_json(self._job_path(job_id), IndexingJob)

    def _write_job_patch(self, job_id: str, patch: JobPatch) -> IndexingJob:
        """Re-read, apply and save the job file under the store lock.

        The whole file is the unit of write here, so a field-scoped write is
        only real if the read the merge is built on and the write that replaces
        it cannot be interleaved — the lock has to span BOTH. That is why this
        re-reads rather than applying the patch to the snapshot the caller
        already validated against: the caller's read happened outside the lock
        and may already be stale.

        Every other read-modify-write in this class takes this lock; this path
        was the omission, not the convention.
        """
        with self._lock:
            job = self._load_json(self._job_path(job_id), IndexingJob)
            if job is None:
                raise KeyError(f"Job not found: {job_id}")
            updated = patch.apply(job)
            self._save_json(self._job_path(job_id), updated)
        return updated

    def list_jobs(self, slug: str | None = None) -> list[IndexingJob]:
        """Return all jobs, newest first by ``phase_started_at``, filtered to *slug*.

        ``iterdir()`` yields entries in arbitrary, platform-dependent filesystem
        order, so the list MUST be sorted before return, and NOT by ``job_id`` —
        a ``uuid4`` hex sorts RANDOMLY with respect to when a job actually ran,
        which is not merely unhelpful but ACTIVELY MISLEADING: at least one
        caller (``resolve_qa_clone_dir``) assumes this method returns
        most-recent-first and picks the FIRST ``complete`` hit, which under such
        a sort could be an arbitrary old checkout on any slug with 2+ completed
        jobs. ``phase_started_at`` is ISO-8601, so lexicographic ==
        chronological; a job that never emitted a phase (no timestamp) sorts
        last, never winning over one that has. :meth:`latest_job` is a thin
        convenience over this order (narrow by status, take the first).
        """
        jobs_root = self.root_dir / "jobs"
        if not jobs_root.exists():
            return []
        jobs: list[IndexingJob] = []
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            job = self._load_json(job_dir / "job.json", IndexingJob)
            if job is None:
                continue
            if slug is None or job.slug == slug:
                jobs.append(job)
        return sorted(jobs, key=lambda j: j.phase_started_at or "", reverse=True)

    def _append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append one validated event to this JSON job timeline. Cost: ``O(1)``."""
        return self._append_event("jobs", job_id, event)

    def load_job_events(
        self, job_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return job events with idx > *after_idx* (-1 returns all)."""
        return self._load_events("jobs", job_id, after_idx)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel *job_id*; return True on first cancel, False if already cancelled."""
        job = self.get_job(job_id)
        if job is None:
            return False
        if job.status == "cancelled":
            return False
        self.update_job(job_id, status="cancelled")
        self.append_job_event(job_id, {"type": "cancelled"})
        return True

    def attach_job_session(self, job_id: str, session_id: str) -> None:
        """Associate a Mewbo session_id with an indexing job."""
        path = self._session_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")

    def get_job_session(self, job_id: str) -> str | None:
        """Return the session_id attached to *job_id*, or None."""
        path = self._session_path(job_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def find_job_by_session(self, session_id: str) -> str | None:
        """Reverse lookup: scan job dirs for the session.txt that matches *session_id*."""
        jobs_root = self.root_dir / "jobs"
        if not jobs_root.exists():
            return None
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            sess_file = job_dir / "session.txt"
            if sess_file.exists() and sess_file.read_text(encoding="utf-8").strip() == session_id:
                return job_dir.name
        return None

    def _job_plan_path(self, job_id: str) -> Path:
        """Filesystem path for the page-plan sidecar file."""
        return self._job_dir(job_id) / "plan.json"

    def _job_meta_path(self, job_id: str) -> Path:
        """Filesystem path for the job extra-metadata sidecar file."""
        return self._job_dir(job_id) / "meta.json"

    def _load_job_meta(self, job_id: str) -> dict[str, Any]:
        """Load job metadata sidecar; returns {} if absent."""
        path = self._job_meta_path(job_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_job_plan(self, job_id: str, plan: list[dict[str, Any]]) -> None:
        """Persist the page-plan list for *job_id*; overwrites any previous plan."""
        path = self._job_plan_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def get_job_plan(self, job_id: str) -> list[dict[str, Any]] | None:
        """Return the page-plan list, or None if no plan has been committed yet."""
        path = self._job_plan_path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else None
        except Exception:
            return None

    def _job_resume_path(self, job_id: str) -> Path:
        """Filesystem path for the resume-plan sidecar file."""
        return self._job_dir(job_id) / "resume.json"

    def save_resume_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the resume-plan dict for *job_id*; overwrites any previous one."""
        path = self._job_resume_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def get_resume_plan(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted resume-plan dict, or None if the job isn't resuming."""
        path = self._job_resume_path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _job_act_path(self, job_id: str) -> Path:
        """Filesystem path for the act-stage sidecar file."""
        return self._job_dir(job_id) / "act.json"

    def save_act_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the act-stage record for *job_id*; overwrites any previous one."""
        path = self._job_act_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def get_act_plan(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted act-stage record, or None if stage 1 hasn't finished."""
        path = self._job_act_path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def get_job_submitted_count(self, job_id: str) -> int:
        """Return the number of pages submitted so far for *job_id*."""
        meta = self._load_job_meta(job_id)
        return int(meta.get("submitted_pages", 0))

    def claim_job_page(self, slug: str, job_id: str, page_id: str) -> PageClaim:
        """Record *page_id* under *job_id*, under the lock; count = set size."""
        if self.get_job(job_id) is None:
            raise KeyError(f"Job not found: {job_id}")
        with self._lock:
            meta = self._load_job_meta(job_id)
            claimed = self._claimed_ids(meta, slug, job_id)
            if page_id in claimed:
                return PageClaim(count=len(claimed), is_new=False)
            claimed.append(page_id)
            meta["submitted_page_ids"] = claimed
            # Kept in step purely so ``get_job_submitted_count`` stays a cheap
            # read; the SET is the authority, so the two can never disagree.
            meta["submitted_pages"] = len(claimed)
            path = self._job_meta_path(job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return PageClaim(count=len(claimed), is_new=True)

    def _claimed_ids(self, meta: dict[str, Any], slug: str, job_id: str) -> list[str]:
        """The job's claimed ids, seeded from attribution when it has no record.

        A MISSING key and an empty list are different answers: the first is a
        job with no claim record (recover what it wrote from attribution), the
        second is a job that has genuinely written nothing yet (and must not
        adopt some other index's pages).
        """
        stored = meta.get("submitted_page_ids")
        if stored is None:
            return sorted(self.page_ids_for_job(slug, job_id))
        return [str(p) for p in stored]

    def get_job_page_ids(self, slug: str, job_id: str) -> frozenset[str]:
        """Return the page ids *job_id* wrote (claim record, else attribution)."""
        return frozenset(self._claimed_ids(self._load_job_meta(job_id), slug, job_id))

    def page_ids_for_job(self, slug: str, job_id: str) -> frozenset[str]:
        """Page ids whose attribution sidecar entry names *job_id*."""
        return frozenset(
            pid
            for pid, meta in self._load_attribution(slug).items()
            if isinstance(meta, dict) and meta.get("job_id") == job_id
        )

    def _job_submission_path(self, job_id: str) -> Path:
        """Filesystem path for the submission sidecar file."""
        return self._job_dir(job_id) / "submission.json"

    def save_job_submission(self, job_id: str, submission: dict[str, Any]) -> None:
        """Persist the wizard submission dict for *job_id* (token must be absent)."""
        path = self._job_submission_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(submission, indent=2), encoding="utf-8")

    def get_job_submission(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted submission dict, or None if not yet saved."""
        path = self._job_submission_path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    # -- Repository credentials (isolated subdir, mode 0600) -----------------

    def _credentials_dir(self) -> Path:
        """Directory holding per-slug credential files (mode 0700)."""
        d = self.root_dir / "credentials"
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:  # pragma: no cover — best-effort on exotic filesystems
            pass
        return d

    def _credential_path(self, slug: str) -> Path:
        """Filesystem path for a slug's credential file."""
        return self._credentials_dir() / f"{_slug_to_path(slug)}.json"

    def save_credentials(self, slug: str, blob: dict[str, Any]) -> None:
        """Persist the encoded credential *blob* for *slug* at mode 0600."""
        path = self._credential_path(slug)
        path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass

    def get_credentials(self, slug: str) -> dict[str, Any] | None:
        """Return the encoded credential blob for *slug*, or None."""
        path = self._credential_path(slug)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def delete_credentials(self, slug: str) -> bool:
        """Delete *slug*'s credential file; return True if one existed."""
        path = self._credential_path(slug)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_credentials(self) -> dict[str, dict[str, Any]]:
        """Return every stored credential blob keyed by scope.

        The scope is read from the blob's ``scope`` field (stamped by
        ``CredentialStore.save``) — authoritative and lossless. Only a blob
        missing that field falls back to inverting :func:`_slug_to_path`
        (``__`` → ``/``), which corrupts a scope containing a literal ``__``;
        malformed files are skipped. Read-only: never creates the dir.
        """
        out: dict[str, dict[str, Any]] = {}
        cred_dir = self.root_dir / "credentials"
        if not cred_dir.exists():
            return out
        for path in cred_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logging.warning("Skipping malformed credential file {}", path)
                continue
            if isinstance(data, dict):
                blob_scope = data.get("scope")
                scope = blob_scope if isinstance(blob_scope, str) else path.stem.replace("__", "/")
                out[scope] = data
        return out

    # -- Restart-recovery counter (slug-keyed sidecar) -----------------------

    def _recovery_path(self, slug: str) -> Path:
        """Filesystem path for a slug's recovery-attempt counter file."""
        d = self.root_dir / "recovery"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{_slug_to_path(slug)}.json"

    def get_recovery_attempts(self, slug: str) -> int:
        """Return the recovery-attempt count for *slug* (0 if never recovered)."""
        path = self._recovery_path(slug)
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("attempts", 0)) if isinstance(data, dict) else 0
        except Exception:
            return 0

    def bump_recovery_attempts(self, slug: str) -> int:
        """Atomically increment *slug*'s recovery counter; return the new value."""
        with self._lock:
            count = self.get_recovery_attempts(slug) + 1
            self._recovery_path(slug).write_text(
                json.dumps({"attempts": count}, indent=2), encoding="utf-8"
            )
            return count

    def reset_recovery_attempts(self, slug: str) -> None:
        """Clear *slug*'s recovery counter file (user-initiated resume fresh budget)."""
        with self._lock:
            path = self._recovery_path(slug)
            if path.exists():
                path.unlink()

    # -- QA ------------------------------------------------------------------

    def _qa_dir(self, answer_id: str) -> Path:
        """Directory for a QA answer's artefacts."""
        return self.root_dir / "qa" / answer_id

    def _qa_path(self, answer_id: str) -> Path:
        """Filesystem path for a QA answer JSON file."""
        return self._qa_dir(answer_id) / "answer.json"

    def _qa_session_path(self, answer_id: str) -> Path:
        """Filesystem path for the QA session-id text file."""
        return self._qa_dir(answer_id) / "session.txt"

    def save_qa(self, answer: QaAnswer) -> None:
        """Persist a QA answer record (``slug`` round-trips through answer.json)."""
        self._qa_dir(answer.answer_id).mkdir(parents=True, exist_ok=True)
        self._save_json(self._qa_path(answer.answer_id), answer)

    def update_qa_fields(self, answer: QaAnswer) -> None:
        """Non-destructive field update.

        Session + events are separate files here, so a plain answer.json rewrite
        already preserves them.
        """
        self._save_json(self._qa_path(answer.answer_id), answer)

    def get_qa(self, answer_id: str) -> QaAnswer | None:
        """Return the QA answer, or None if absent."""
        return self._load_json(self._qa_path(answer_id), QaAnswer)

    def list_qa(self, status: str | None = None) -> list[QaAnswer]:
        """Return all QA answers, optionally filtered to *status*.

        ``iterdir()`` order is arbitrary — this is a boot-time/offline scan
        (mirrors :meth:`list_jobs`), never an interactive listing, so no
        ordering guarantee is made or needed here.
        """
        qa_root = self.root_dir / "qa"
        if not qa_root.exists():
            return []
        answers: list[QaAnswer] = []
        for qa_dir in qa_root.iterdir():
            if not qa_dir.is_dir():
                continue
            answer = self._load_json(qa_dir / "answer.json", QaAnswer)
            if answer is None:
                continue
            if status is None or answer.status == status:
                answers.append(answer)
        return answers

    def attach_qa_session(self, answer_id: str, session_id: str) -> None:
        """Associate a Mewbo session_id with a QA answer."""
        path = self._qa_session_path(answer_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")

    def get_qa_session(self, answer_id: str) -> str | None:
        """Return the session_id attached to *answer_id*, or None."""
        path = self._qa_session_path(answer_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def find_qa_by_session(self, session_id: str) -> str | None:
        """Reverse lookup: scan qa dirs for the session.txt that matches *session_id*."""
        qa_root = self.root_dir / "qa"
        if not qa_root.exists():
            return None
        for qa_dir in qa_root.iterdir():
            if not qa_dir.is_dir():
                continue
            sess_file = qa_dir / "session.txt"
            if sess_file.exists() and sess_file.read_text(encoding="utf-8").strip() == session_id:
                return qa_dir.name
        return None

    def append_qa_event(self, answer_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the QA event log; return the monotonic idx."""
        return self._append_event("qa", answer_id, event)

    def load_qa_events(
        self, answer_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return QA events with idx > *after_idx* (-1 returns all)."""
        return self._load_events("qa", answer_id, after_idx)

    # -- Graph + embeddings --------------------------------------------------

    def _graph_dir(self, slug: str) -> Path:
        """Directory for per-slug graph artefacts."""
        return self.root_dir / "graph" / _slug_to_path(slug)

    def _nodes_path(self, slug: str) -> Path:
        return self._graph_dir(slug) / "nodes.jsonl"

    def _edges_path(self, slug: str) -> Path:
        return self._graph_dir(slug) / "edges.jsonl"

    def _embeddings_path(self, slug: str) -> Path:
        return self._graph_dir(slug) / "embeddings.jsonl"

    def _load_jsonl(self, path: Path, model_cls: type[_M]) -> list[_M]:
        """Load a JSONL file; skip malformed lines. Returns [] if absent."""
        if not path.exists():
            return []
        out: list[_M] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(model_cls.model_validate_json(line))
            except Exception:
                logging.warning("Skipping malformed line in {}", path)
        return out

    def _load_graph_nodes(self, path: Path) -> list[GraphNode]:
        """Load a graph-node JSONL, dispatching each line to its per-kind class.

        ``GraphNode`` is a discriminated union (schema v2), so validation goes
        through :data:`GraphNodeAdapter` — the ``type`` discriminator picks
        ``FileNode``/``ClassNode``/… ; a line without ``subkind``/
        ``attributes`` validates to the defaults. Mirrors ``_load_jsonl`` but
        can't reuse it (the union is not a single ``BaseModel`` subclass).
        """
        if not path.exists():
            return []
        out: list[GraphNode] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(GraphNodeAdapter.validate_json(line))
            except Exception:
                logging.warning("Skipping malformed line in {}", path)
        return out

    def _write_jsonl(self, path: Path, items: list[Any]) -> None:
        """Atomically rewrite a JSONL file (tmp + rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(item.model_dump_json(by_alias=True) for item in items) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def upsert_nodes(
        self,
        slug: str,
        nodes: Iterable[GraphNode],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upsert graph nodes for *slug*; dedup by node_id, stamp attribution.

        Cost: ``O(nodes)`` — offline. The JSON driver writes one atomic file, so
        one non-empty input is one persistence batch.
        """
        items = list(nodes)
        with self._lock:
            existing = {n.node_id: n for n in self._load_graph_nodes(self._nodes_path(slug))}
            for node in items:
                existing[node.node_id] = self._stamp_attribution(node, commit_sha, job_id)
            self._write_jsonl(self._nodes_path(slug), list(existing.values()))
        if items and on_progress is not None:
            on_progress(1, 1)

    def upsert_edges(
        self,
        slug: str,
        edges: Iterable[GraphEdge],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upsert graph edges for *slug*; dedup by (source, target, type).

        Cost: ``O(edges)`` — offline. The JSON driver writes one atomic file, so
        one non-empty input is one persistence batch.
        """
        items = list(edges)
        with self._lock:
            existing = {
                (e.source, e.target, e.type): e
                for e in self._load_jsonl(self._edges_path(slug), GraphEdge)
            }
            for edge in items:
                existing[(edge.source, edge.target, edge.type)] = self._stamp_attribution(
                    edge, commit_sha, job_id
                )
            self._write_jsonl(self._edges_path(slug), list(existing.values()))
        if items and on_progress is not None:
            on_progress(1, 1)

    def upsert_embeddings(
        self,
        slug: str,
        items: Iterable[Embedding],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert embedding vectors for *slug*; dedup by node_id."""
        with self._lock:
            existing = {
                e.node_id: e
                for e in self._load_jsonl(self._embeddings_path(slug), Embedding)
            }
            for item in items:
                existing[item.node_id] = self._stamp_attribution(item, commit_sha, job_id)
            self._write_jsonl(self._embeddings_path(slug), list(existing.values()))

    def query_graph(
        self,
        slug: str,
        *,
        scope: CommitScope,
        node_type: str | None = None,
        name_match: str | None = None,
        neighbors_of: str | None = None,
        node_ids: Collection[str] | None = None,
    ) -> list[GraphNode]:
        """See ``WikiStoreBase.query_graph``."""
        if neighbors_of is not None:
            # The seed's own generation bounds the walk: an edge is only a
            # neighbour relation if it belongs to the same generation as the
            # nodes we are about to return, or the neighbourhood spans commits.
            edges = [
                e
                for e in self._load_jsonl(self._edges_path(slug), GraphEdge)
                if scope.matches(e.commit_sha)
            ]
            related_ids: set[str] = set()
            for edge in edges:
                if edge.source == neighbors_of:
                    related_ids.add(edge.target)
                elif edge.target == neighbors_of:
                    related_ids.add(edge.source)
            all_nodes = self._load_graph_nodes(self._nodes_path(slug))
            return [
                n
                for n in all_nodes
                if n.node_id in related_ids and scope.matches(n.commit_sha)
            ]
        nodes = [
            n
            for n in self._load_graph_nodes(self._nodes_path(slug))
            if scope.matches(n.commit_sha)
        ]
        if node_ids is not None:
            wanted = set(node_ids)
            nodes = [n for n in nodes if n.node_id in wanted]
        if node_type is not None:
            nodes = [n for n in nodes if n.type == node_type]
        if name_match is not None:
            lower = name_match.lower()
            nodes = [n for n in nodes if lower in n.name.lower()]
        return nodes

    def list_edges(self, slug: str, *, scope: CommitScope) -> list[GraphEdge]:
        """See ``WikiStoreBase.list_edges``."""
        return [
            e
            for e in self._load_jsonl(self._edges_path(slug), GraphEdge)
            if scope.matches(e.commit_sha)
        ]

    def count_graph_nodes(self, slug: str, *, commit_sha: str | None) -> int:
        """Count *slug* nodes stamped exactly *commit_sha* (``None`` matches None)."""
        return sum(
            1
            for n in self._load_graph_nodes(self._nodes_path(slug))
            if n.commit_sha == commit_sha
        )

    def supersede_graph_artifacts(
        self, slug: str, *, keep_commit_sha: str
    ) -> dict[str, int]:
        """Drop prior-commit graph + entity artifacts, preserving ``None``-stamped rows.

        A row survives iff its ``commit_sha`` is ``None`` (a QA-minted or
        pre-isolation record) OR equals *keep_commit_sha*. Everything else — the
        artifacts of a superseded commit — is dropped.
        """
        counts: dict[str, int] = {}
        with self._lock:
            counts["nodes"] = self._retain_jsonl(
                self._nodes_path(slug), self._load_graph_nodes, keep_commit_sha
            )
            counts["edges"] = self._retain_jsonl(
                self._edges_path(slug),
                lambda p: self._load_jsonl(p, GraphEdge),
                keep_commit_sha,
            )
            counts["embeddings"] = self._retain_jsonl(
                self._embeddings_path(slug),
                lambda p: self._load_jsonl(p, Embedding),
                keep_commit_sha,
            )
            counts["entities"] = self._retain_jsonl(
                self._entities_path(slug),
                lambda p: self._load_jsonl(p, Entity),
                keep_commit_sha,
            )
            counts["entity_edges"] = self._retain_jsonl(
                self._entity_edges_path(slug),
                lambda p: self._load_jsonl(p, EntityRelation),
                keep_commit_sha,
            )
            counts["entity_embeddings"] = self._retain_jsonl(
                self._entity_embeddings_path(slug),
                lambda p: self._load_jsonl(p, EntityEmbedding),
                keep_commit_sha,
            )
        return counts

    def restamp_graph_artifacts(
        self, slug: str, *, from_commit: str, to_commit: str
    ) -> dict[str, int]:
        """See ``WikiStoreBase.restamp_graph_artifacts``."""
        counts: dict[str, int] = {}
        with self._lock:
            for key, path, loader in (
                ("nodes", self._nodes_path(slug), self._load_graph_nodes),
                ("edges", self._edges_path(slug),
                 lambda p: self._load_jsonl(p, GraphEdge)),
                ("embeddings", self._embeddings_path(slug),
                 lambda p: self._load_jsonl(p, Embedding)),
                ("entities", self._entities_path(slug),
                 lambda p: self._load_jsonl(p, Entity)),
                ("entity_edges", self._entity_edges_path(slug),
                 lambda p: self._load_jsonl(p, EntityRelation)),
                ("entity_embeddings", self._entity_embeddings_path(slug),
                 lambda p: self._load_jsonl(p, EntityEmbedding)),
            ):
                counts[key] = self._restamp_jsonl(path, loader, from_commit, to_commit)
        return counts

    def _restamp_jsonl(
        self, path: Path, loader: Any, from_commit: str, to_commit: str
    ) -> int:
        """Rewrite *path* moving ``from_commit`` rows to ``to_commit``; return #moved.

        Graph nodes are ``frozen=True``, so a row is REPLACED via ``model_copy``
        rather than mutated in place — the same reason hierarchy stamping happens
        on the wire dict rather than on the node.
        """
        items = loader(path)
        moved = 0
        out = []
        for it in items:
            if it.commit_sha == from_commit:
                out.append(it.model_copy(update={"commit_sha": to_commit}))
                moved += 1
            else:
                out.append(it)
        if moved:
            self._write_jsonl(path, out)
        return moved

    def _retain_jsonl(
        self, path: Path, loader: Any, keep_commit_sha: str
    ) -> int:
        """Rewrite *path* keeping only ``None``/``keep_commit_sha`` rows; return #dropped."""
        items = loader(path)
        kept = [
            it for it in items
            if it.commit_sha is None or it.commit_sha == keep_commit_sha
        ]
        dropped = len(items) - len(kept)
        if dropped:
            self._write_jsonl(path, kept)
        return dropped

    def vector_search(self, slug: str, qvec: list[float], k: int = 10) -> list[Embedding]:
        """Return top-k embeddings for *slug* by cosine similarity."""
        from .embedder import Embedder

        pool = self._load_jsonl(self._embeddings_path(slug), Embedding)
        if not pool:
            return []
        scored = [(emb, Embedder.cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

    # -- Scoped graph deletes (incremental retract) --------------------------

    def delete_nodes_by_file(self, slug: str, file: str) -> int:
        """Delete *file*'s code nodes AND their vectors; return the NODE count."""
        with self._lock:
            nodes = self._load_graph_nodes(self._nodes_path(slug))
            keep = [n for n in nodes if n.file != file]
            removed = len(nodes) - len(keep)
            if not removed:
                return 0
            self._write_jsonl(self._nodes_path(slug), keep)
            doomed = {n.node_id for n in nodes if n.file == file}
            vectors = self._load_jsonl(self._embeddings_path(slug), Embedding)
            kept_vectors = [e for e in vectors if e.node_id not in doomed]
            if len(kept_vectors) != len(vectors):
                self._write_jsonl(self._embeddings_path(slug), kept_vectors)
            return removed

    def delete_edges_by_source_file(self, slug: str, file: str) -> int:
        """Delete edges whose ``source`` node belongs to *file*; return count."""
        with self._lock:
            file_ids = {
                n.node_id
                for n in self._load_graph_nodes(self._nodes_path(slug))
                if n.file == file
            }
            if not file_ids:
                return 0
            edges = self._load_jsonl(self._edges_path(slug), GraphEdge)
            keep = [e for e in edges if e.source not in file_ids]
            removed = len(edges) - len(keep)
            if removed:
                self._write_jsonl(self._edges_path(slug), keep)
            return removed

    # -- Memory layer (multiplex overlay) ------------------------------------

    def _memory_dir(self, slug: str) -> Path:
        """Directory for per-slug memory-layer artefacts."""
        return self.root_dir / "memory" / _slug_to_path(slug)

    def _memory_nodes_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "nodes.jsonl"

    def _memory_edges_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "edges.jsonl"

    def _memory_embeddings_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "embeddings.jsonl"

    def _doc_notes_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "docs.jsonl"

    def _manifest_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "manifest.jsonl"

    def upsert_memory_nodes(self, slug: str, nodes: Iterable[MemoryNode]) -> None:
        """Upsert memory nodes for *slug*; dedup by node_id."""
        with self._lock:
            existing = {
                n.node_id: n
                for n in self._load_jsonl(self._memory_nodes_path(slug), MemoryNode)
            }
            for node in nodes:
                existing[node.node_id] = node
            self._write_jsonl(self._memory_nodes_path(slug), list(existing.values()))

    def get_memory_node(self, slug: str, node_id: str) -> MemoryNode | None:
        """Return a single memory node, or None if absent."""
        for n in self._load_jsonl(self._memory_nodes_path(slug), MemoryNode):
            if n.node_id == node_id:
                return n
        return None

    def delete_memory_node(self, slug: str, node_id: str) -> bool:
        """Delete a memory node + its embedding; return True if one was removed."""
        with self._lock:
            nodes = self._load_jsonl(self._memory_nodes_path(slug), MemoryNode)
            keep = [n for n in nodes if n.node_id != node_id]
            removed = len(keep) != len(nodes)
            if removed:
                self._write_jsonl(self._memory_nodes_path(slug), keep)
                embs = self._load_jsonl(
                    self._memory_embeddings_path(slug), MemoryEmbedding
                )
                kept_embs = [e for e in embs if e.node_id != node_id]
                if len(kept_embs) != len(embs):
                    self._write_jsonl(self._memory_embeddings_path(slug), kept_embs)
            return removed

    def query_memory(
        self, slug: str, *, filt: MemoryFilter | None = None
    ) -> list[MemoryNode]:
        """Return memory nodes matching *filt*'s node-level facets."""
        nodes = self._load_jsonl(self._memory_nodes_path(slug), MemoryNode)
        if filt is None:
            return nodes
        return [n for n in nodes if filt.matches_node(n)]

    def upsert_memory_edges(self, slug: str, edges: Iterable[MemoryEdge]) -> None:
        """Upsert memory edges for *slug*; dedup by (source, target, type)."""
        with self._lock:
            existing = {
                (e.source, e.target, e.type): e
                for e in self._load_jsonl(self._memory_edges_path(slug), MemoryEdge)
            }
            for edge in edges:
                existing[(edge.source, edge.target, edge.type)] = edge
            self._write_jsonl(self._memory_edges_path(slug), list(existing.values()))

    def list_memory_edges(
        self,
        slug: str,
        *,
        node_id: str | None = None,
        include_invalidated: bool = False,
    ) -> list[MemoryEdge]:
        """Return memory edges, optionally scoped to ``source == node_id``."""
        out: list[MemoryEdge] = []
        for e in self._load_jsonl(self._memory_edges_path(slug), MemoryEdge):
            if node_id is not None and e.source != node_id:
                continue
            if e.invalid_at is not None and not include_invalidated:
                continue
            out.append(e)
        return out

    def memories_anchored_to(
        self,
        slug: str,
        entity_keys: Iterable[EntityKey],
        *,
        include_invalidated: bool = False,
    ) -> list[str]:
        """Reverse ANCHORS lookup: entity_keys → distinct memory node_ids."""
        keys = set(entity_keys)
        seen: list[str] = []
        seen_set: set[str] = set()
        for e in self._load_jsonl(self._memory_edges_path(slug), MemoryEdge):
            if e.type != "ANCHORS" or e.target not in keys:
                continue
            if e.invalid_at is not None and not include_invalidated:
                continue
            if e.source not in seen_set:
                seen_set.add(e.source)
                seen.append(e.source)
        return seen

    def _live_anchored_ids(self, slug: str) -> set[str]:
        """Memory node_ids with ≥1 live ANCHORS edge."""
        return {
            e.source
            for e in self._load_jsonl(self._memory_edges_path(slug), MemoryEdge)
            if e.type == "ANCHORS" and e.invalid_at is None
        }

    def upsert_memory_embeddings(
        self, slug: str, items: Iterable[MemoryEmbedding]
    ) -> None:
        """Upsert memory embedding vectors for *slug*; dedup by node_id."""
        with self._lock:
            existing = {
                e.node_id: e
                for e in self._load_jsonl(
                    self._memory_embeddings_path(slug), MemoryEmbedding
                )
            }
            for item in items:
                existing[item.node_id] = item
            self._write_jsonl(
                self._memory_embeddings_path(slug), list(existing.values())
            )

    def memory_vector_search(
        self,
        slug: str,
        qvec: list[float],
        k: int = 10,
        *,
        filt: MemoryFilter | None = None,
    ) -> list[MemoryEmbedding]:
        """Top-k memory embeddings by cosine, after applying *filt*."""
        pool = self._load_jsonl(self._memory_embeddings_path(slug), MemoryEmbedding)
        return self._rank_memory(slug, pool, qvec, k, filt)

    # -- Doc-page notes ------------------------------------------------------

    def upsert_doc_notes(self, slug: str, notes: Iterable[DocPageNote]) -> None:
        """Upsert doc-page notes for *slug*; dedup by page_id."""
        with self._lock:
            existing = {
                d.page_id: d
                for d in self._load_jsonl(self._doc_notes_path(slug), DocPageNote)
            }
            for note in notes:
                existing[note.page_id] = note
            self._write_jsonl(self._doc_notes_path(slug), list(existing.values()))

    def get_doc_note(self, slug: str, page_id: str) -> DocPageNote | None:
        """Return a single doc-page note, or None if absent."""
        for d in self._load_jsonl(self._doc_notes_path(slug), DocPageNote):
            if d.page_id == page_id:
                return d
        return None

    def list_doc_notes(self, slug: str) -> list[DocPageNote]:
        """Return every doc-page note for *slug*."""
        return self._load_jsonl(self._doc_notes_path(slug), DocPageNote)

    def delete_doc_note(self, slug: str, page_id: str) -> bool:
        """Delete a doc-page note; return True if one was removed."""
        with self._lock:
            notes = self._load_jsonl(self._doc_notes_path(slug), DocPageNote)
            keep = [d for d in notes if d.page_id != page_id]
            if len(keep) == len(notes):
                return False
            self._write_jsonl(self._doc_notes_path(slug), keep)
            return True

    # -- File manifest -------------------------------------------------------

    def upsert_file_manifest(
        self, slug: str, entries: Iterable[FileManifest]
    ) -> None:
        """Upsert file-manifest entries for *slug*; dedup by path."""
        with self._lock:
            existing = {
                m.path: m
                for m in self._load_jsonl(self._manifest_path(slug), FileManifest)
            }
            for entry in entries:
                existing[entry.path] = entry
            self._write_jsonl(self._manifest_path(slug), list(existing.values()))

    def get_file_manifest(self, slug: str, path: str) -> FileManifest | None:
        """Return a single file-manifest entry, or None if absent."""
        for m in self._load_jsonl(self._manifest_path(slug), FileManifest):
            if m.path == path:
                return m
        return None

    def list_file_manifest(self, slug: str) -> list[FileManifest]:
        """Return every file-manifest entry for *slug*."""
        return self._load_jsonl(self._manifest_path(slug), FileManifest)

    def delete_file_manifest(self, slug: str, path: str) -> bool:
        """Delete a file-manifest entry; return True if one was removed."""
        with self._lock:
            entries = self._load_jsonl(self._manifest_path(slug), FileManifest)
            keep = [m for m in entries if m.path != path]
            if len(keep) == len(entries):
                return False
            self._write_jsonl(self._manifest_path(slug), keep)
            return True

    # -- Abstract-entity layer (multiplex overlay) ---------------------------
    #
    # Persisted as JSONL under the same per-slug memory dir as memory nodes,
    # reusing the exact ``_load_jsonl`` / ``_write_jsonl`` upsert idiom so the
    # entity overlay can never desync from the memory overlay's conventions.

    def _entities_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "entities.jsonl"

    def _entity_embeddings_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "entity_embeddings.jsonl"

    def _entity_edges_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "entity_edges.jsonl"

    def _entity_recs_path(self, slug: str) -> Path:
        return self._memory_dir(slug) / "entity_recommendations.jsonl"

    def upsert_entities(
        self,
        slug: str,
        entities: Iterable[Entity],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entities for *slug*; dedup by id, stamp attribution."""
        with self._lock:
            existing = {
                e.id: e for e in self._load_jsonl(self._entities_path(slug), Entity)
            }
            for entity in entities:
                existing[entity.id] = self._stamp_attribution(entity, commit_sha, job_id)
            self._write_jsonl(self._entities_path(slug), list(existing.values()))

    def get_entity(self, slug: str, entity_id: str) -> Entity | None:
        """Return a single entity, or None if absent."""
        for e in self._load_jsonl(self._entities_path(slug), Entity):
            if e.id == entity_id:
                return e
        return None

    def query_entities(
        self, slug: str, *, filt: EntityFilter | None = None
    ) -> list[Entity]:
        """Return entities matching *filt*'s facets."""
        entities = self._load_jsonl(self._entities_path(slug), Entity)
        if filt is None:
            return entities
        return [e for e in entities if filt.matches(e)]

    def count_entities(self, slug: str, *, commit_sha: str | None) -> int:
        """Count *slug* entities stamped exactly *commit_sha* (``None`` matches None)."""
        return sum(
            1
            for e in self._load_jsonl(self._entities_path(slug), Entity)
            if e.commit_sha == commit_sha
        )

    def upsert_entity_embeddings(
        self,
        slug: str,
        items: Iterable[EntityEmbedding],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entity embedding vectors for *slug*; dedup by entity_id."""
        with self._lock:
            existing = {
                e.entity_id: e
                for e in self._load_jsonl(
                    self._entity_embeddings_path(slug), EntityEmbedding
                )
            }
            for item in items:
                existing[item.entity_id] = self._stamp_attribution(
                    item, commit_sha, job_id
                )
            self._write_jsonl(
                self._entity_embeddings_path(slug), list(existing.values())
            )

    def entity_vector_search(
        self, slug: str, qvec: list[float], k: int = 10
    ) -> list[EntityEmbedding]:
        """Return top-k entity embeddings for *slug* by cosine similarity."""
        pool = self._load_jsonl(self._entity_embeddings_path(slug), EntityEmbedding)
        return self._rank_embeddings(pool, qvec, k)

    def upsert_entity_edges(
        self,
        slug: str,
        edges: Iterable[EntityRelation],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entity relations for *slug*; dedup by id."""
        with self._lock:
            existing = {
                e.id: e
                for e in self._load_jsonl(self._entity_edges_path(slug), EntityRelation)
            }
            for edge in edges:
                existing[edge.id] = self._stamp_attribution(edge, commit_sha, job_id)
            self._write_jsonl(self._entity_edges_path(slug), list(existing.values()))

    def list_entity_edges(
        self, slug: str, *, source_id: str | None = None
    ) -> list[EntityRelation]:
        """Return entity relations, optionally scoped to ``source_id``."""
        out = self._load_jsonl(self._entity_edges_path(slug), EntityRelation)
        if source_id is not None:
            out = [e for e in out if e.source_id == source_id]
        return out

    def save_entity_recommendation(
        self, slug: str, rec: EntityRecommendation
    ) -> None:
        """Upsert a recommendation for *slug*; dedup by id (a replay converges)."""
        with self._lock:
            existing = {
                r.id: r
                for r in self._load_jsonl(
                    self._entity_recs_path(slug), EntityRecommendation
                )
            }
            existing[rec.id] = rec
            self._write_jsonl(self._entity_recs_path(slug), list(existing.values()))

    def get_entity_recommendations(self, slug: str) -> list[EntityRecommendation]:
        """Return every persisted entity recommendation for *slug*."""
        return self._load_jsonl(self._entity_recs_path(slug), EntityRecommendation)

# ---------------------------------------------------------------------------
# MongoDB driver
# ---------------------------------------------------------------------------


def _strip_mongo_meta(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove MongoDB internal fields (_id) before Pydantic validation."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


# ``wiki_embeddings`` carries each vector twice: the canonical ``vector`` list
# ``Embedding`` declares, and ``vec_f32`` — the same numbers as a packed
# little-endian float32 buffer. The duplication is what makes cosine search
# interactive, and the reason is the BSON array encoding rather than the
# arithmetic: every element of a 3072-d array carries its own index key plus an
# 8-byte double, so one document costs ~42 KB and scoring one project's 61,774
# vectors meant pulling ~2.6 GB off the wire and decoding it into ~190 million
# Python floats before the first comparison. Measured on that project, the two
# legs cost 34.1 s of fetch and 23.4 s of scoring; the same vectors as a packed
# buffer are ~12 KB each and reach NumPy through a single ``frombuffer`` with no
# per-element Python object at all, taking the whole search to 3.9 s.
#
# NumPy alone does NOT fix this — measured at 42.9 s, because the BSON decode is
# the floor and only scoring got faster. The storage change is the load-bearing
# half.
#
# float32 is not a precision compromise worth worrying about: against the
# float64 path over that same project the top-30 was IDENTICAL, with a maximum
# score delta of 2.4e-07.
_VEC_F32 = "vec_f32"


def _pack_f32(vector: Sequence[float]) -> bytes:
    """Pack *vector* as the little-endian float32 buffer stored at ``_VEC_F32``."""
    return struct.pack(f"<{len(vector)}f", *vector)


def _clean_for_model(doc: dict[str, Any], model_cls: type) -> dict[str, Any]:
    """Strip Mongo meta + any extra persisted fields not declared on *model_cls*.

    The job/qa documents persist bookkeeping like ``event_count``, ``submission``,
    ``session_id``, ``plan``, and ``submitted_pages`` alongside the wire-shape
    fields. The wire-shape models (``IndexingJob``, ``QaAnswer``) use
    ``ConfigDict(extra="forbid")``, so we whitelist by the declared field names
    (both Python and alias) at load time instead of mutating each model.
    """
    clean = _strip_mongo_meta(doc)
    allowed: set[str] = set()
    for name, field in getattr(model_cls, "model_fields", {}).items():
        allowed.add(name)
        alias = getattr(field, "alias", None)
        if alias:
            allowed.add(alias)
    return {k: v for k, v in clean.items() if k in allowed}


class MongoWikiStore(WikiStoreBase):
    """MongoDB-backed wiki persistence.

    Collections:

    - ``wiki_projects``     (slug PK)
    - ``wiki_pages``        ((slug, page_id) compound PK)
    - ``wiki_jobs``         (job_id PK; includes ``event_count`` for atomic ``$inc``)
    - ``wiki_job_events``   ((job_id, idx) compound; append-only)
    - ``wiki_qa``           (answer_id PK; includes ``event_count``)
    - ``wiki_qa_events``    ((answer_id, idx) compound; append-only)

    Graph/embeddings collections are not created here — the methods raise
    ``NotImplementedError`` inherited from ``WikiStoreBase``.
    """

    #: Documents per ``bulk_write`` for the graph upserts. Bounded so a write of
    #: a whole repository's edges holds one batch of pending operations in
    #: memory rather than all of them; large enough that the per-round-trip cost
    #: amortises to nothing.
    _BULK_BATCH_SIZE = 1000

    def __init__(
        self,
        *,
        client: Any = None,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Initialize MongoDB connection and ensure indexes exist."""
        if client is None:
            from pymongo import MongoClient

            _uri = uri or get_config_value(
                "storage", "mongodb", "uri", default="mongodb://localhost:27017"
            )
            client = MongoClient(_uri, serverSelectionTimeoutMS=5000)
            # Fail fast — mirrors MongoSessionStore.
            client.admin.command("ping")
        if database is None:
            database = get_config_value(
                "storage", "mongodb", "database", default="mewbo"
            )
        self._client = client
        self._db = client[database]
        self._ensure_indexes()

    # -- helpers -------------------------------------------------------------

    def _col(self, name: str) -> Any:
        """Return a MongoDB collection by name."""
        return self._db[name]

    def _ensure_indexes(self) -> None:
        """Create indexes idempotently on first connection."""
        from pymongo import ASCENDING

        def _idx(col: str, keys: list[tuple[str, Any]], name: str) -> None:
            self._col(col).create_index(keys, name=name, unique=True, background=True)

        _idx("wiki_projects", [("slug", ASCENDING)], "ix_projects_slug")
        _idx(
            "wiki_pages",
            [("slug", ASCENDING), ("page_id", ASCENDING)],
            "ix_pages_slug_pageid",
        )
        _idx("wiki_jobs", [("job_id", ASCENDING)], "ix_jobs_job_id")
        _idx(
            "wiki_job_events",
            [("job_id", ASCENDING), ("idx", ASCENDING)],
            "ix_job_events_job_idx",
        )
        _idx("wiki_qa", [("answer_id", ASCENDING)], "ix_qa_answer_id")
        _idx(
            "wiki_qa_events",
            [("answer_id", ASCENDING), ("idx", ASCENDING)],
            "ix_qa_events_answer_idx",
        )
        _idx("wiki_credentials", [("slug", ASCENDING)], "ix_credentials_slug")
        _idx("wiki_recovery", [("slug", ASCENDING)], "ix_recovery_slug")
        _idx("wiki_settings", [("slug", ASCENDING)], "ix_settings_slug")

    def _atomic_next_idx(self, col: str, owner_field: str, owner_id: str) -> int:
        """Atomically increment event_count on the owner document and return the next idx (0-based).

        Uses ``$inc`` on ``event_count`` and returns ``new_value - 1`` as the
        event's monotonic idx so that the first event gets idx=0.
        """
        from pymongo import ReturnDocument

        doc = self._col(col).find_one_and_update(
            {owner_field: owner_id},
            {"$inc": {"event_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise KeyError(f"No document in '{col}' with {owner_field}={owner_id!r}")
        return int(doc["event_count"]) - 1

    # -- Projects ------------------------------------------------------------

    def create_project(self, project: Project) -> None:
        """Persist a new project record."""
        doc = project.model_dump(by_alias=False)
        self._col("wiki_projects").replace_one(
            {"slug": project.slug}, doc, upsert=True
        )

    def get_project(self, slug: str) -> Project | None:
        """Return the project for *slug*, or None if absent."""
        doc = self._col("wiki_projects").find_one({"slug": slug})
        if doc is None:
            return None
        return Project.model_validate(_strip_mongo_meta(doc))

    def list_projects(self) -> list[Project]:
        """Return all projects sorted by indexed_at descending."""
        cursor = self._col("wiki_projects").find().sort("indexed_at", -1)
        return [Project.model_validate(_strip_mongo_meta(d)) for d in cursor]

    def delete_project(self, slug: str) -> bool:
        """Delete project *slug*; return True if deleted, False if absent."""
        result = self._col("wiki_projects").delete_one({"slug": slug})
        return result.deleted_count > 0

    # -- Project settings (slug-keyed collection) ----------------------------

    def save_project_settings(self, slug: str, settings: ProjectSettings) -> None:
        """Persist (upsert) the editable settings record for *slug*."""
        doc = settings.model_dump(by_alias=False)
        doc["slug"] = slug
        self._col("wiki_settings").replace_one({"slug": slug}, doc, upsert=True)

    def get_project_settings(self, slug: str) -> ProjectSettings | None:
        """Return *slug*'s settings record, or None when never written."""
        doc = self._col("wiki_settings").find_one({"slug": slug})
        if doc is None:
            return None
        try:
            return ProjectSettings.model_validate(_strip_mongo_meta(doc))
        except Exception:
            # A hand-edited / off-schema document must not break the read path —
            # the caller then falls back to the per-job submission scan,
            # exactly as it does for a project that has no record at all.
            logging.warning("Skipping malformed wiki_settings document for {}", slug)
            return None

    def delete_project_settings(self, slug: str) -> bool:
        """Delete *slug*'s settings document; return True if one existed."""
        return self._col("wiki_settings").delete_one({"slug": slug}).deleted_count > 0

    # -- Whole-slug reap (see WikiStoreBase.reap_slug for the family list) ---

    def reap_slug(self, slug: str) -> dict[str, int]:
        """See ``WikiStoreBase.reap_slug``.

        ``wiki_job_events``/``wiki_qa_events`` carry only their owning id
        (``job_id``/``answer_id``), never ``slug`` — so those ids are read from
        ``wiki_jobs``/``wiki_qa`` FIRST, before either collection is touched,
        and the two event collections are then swept by id.
        """
        job_ids = [
            str(d["job_id"])
            for d in self._col("wiki_jobs").find({"slug": slug}, {"job_id": 1})
        ]
        answer_ids = [
            str(d["answer_id"])
            for d in self._col("wiki_qa").find({"slug": slug}, {"answer_id": 1})
        ]

        counts: dict[str, int] = {}
        for key, coll in (
            ("graph_nodes", "wiki_graph_nodes"),
            ("graph_edges", "wiki_graph_edges"),
            ("embeddings", "wiki_embeddings"),
            ("entities", "wiki_entities"),
            ("entity_edges", "wiki_entity_edges"),
            ("entity_embeddings", "wiki_entity_embeddings"),
            ("entity_recommendations", "wiki_entity_recommendations"),
            ("memory_nodes", "wiki_memory_nodes"),
            ("memory_edges", "wiki_memory_edges"),
            ("memory_embeddings", "wiki_memory_embeddings"),
            ("doc_notes", "wiki_doc_notes"),
            ("file_manifest", "wiki_file_manifest"),
            ("pages", "wiki_pages"),
            ("recovery", "wiki_recovery"),
            ("jobs", "wiki_jobs"),
            ("qa", "wiki_qa"),
        ):
            counts[key] = int(self._col(coll).delete_many({"slug": slug}).deleted_count)

        counts["job_events"] = (
            int(
                self._col("wiki_job_events")
                .delete_many({"job_id": {"$in": job_ids}})
                .deleted_count
            )
            if job_ids
            else 0
        )
        counts["qa_events"] = (
            int(
                self._col("wiki_qa_events")
                .delete_many({"answer_id": {"$in": answer_ids}})
                .deleted_count
            )
            if answer_ids
            else 0
        )
        return counts

    # -- Pages ---------------------------------------------------------------

    # Page attribution rides two store columns, never ``WikiPage`` fields, so the
    # console wire type stays byte-identical; both are stripped before validation.
    _PAGE_STORE_COLS = ("slug", "page_id", "commit_sha", "job_id")

    def save_page(
        self,
        slug: str,
        page: WikiPage,
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Persist *page* for the project *slug*; overwrites if same page_id."""
        doc = {
            "slug": slug,
            "page_id": page.id,
            "commit_sha": commit_sha,
            "job_id": job_id,
            **page.model_dump(by_alias=False),
        }
        self._col("wiki_pages").replace_one(
            {"slug": slug, "page_id": page.id}, doc, upsert=True
        )

    def _get_page_raw(self, slug: str, page_id: str) -> WikiPage | None:
        """Return a single wiki page, or None if absent (no doc-guard)."""
        doc = self._col("wiki_pages").find_one({"slug": slug, "page_id": page_id})
        if doc is None:
            return None
        clean = _strip_mongo_meta(doc)
        for col in self._PAGE_STORE_COLS:
            clean.pop(col, None)
        return WikiPage.model_validate(clean)

    def list_pages(self, slug: str) -> list[WikiPage]:
        """Return all pages for project *slug*."""
        pages: list[WikiPage] = []
        for doc in self._col("wiki_pages").find({"slug": slug}):
            clean = _strip_mongo_meta(doc)
            for col in self._PAGE_STORE_COLS:
                clean.pop(col, None)
            page = WikiPage.model_validate(clean)
            pages.append(page)
        return pages

    def delete_page(self, slug: str, page_id: str) -> bool:
        """Delete a single wiki page document. Returns True on a hit."""
        result = self._col("wiki_pages").delete_one(
            {"slug": slug, "page_id": page_id}
        )
        return result.deleted_count > 0

    def prune_pages(self, slug: str, keep: Iterable[str]) -> int:
        """Bulk-drop pages not in *keep* in a single Mongo round-trip."""
        keep_list = list(keep)
        result = self._col("wiki_pages").delete_many(
            {"slug": slug, "page_id": {"$nin": keep_list}}
        )
        return int(result.deleted_count)

    # -- Indexing jobs -------------------------------------------------------

    def create_job(self, job: IndexingJob) -> None:
        """Persist a new indexing job."""
        doc = {"event_count": 0, **job.model_dump(by_alias=False)}
        self._col("wiki_jobs").replace_one({"job_id": job.job_id}, doc, upsert=True)

    def get_job(self, job_id: str) -> IndexingJob | None:
        """Return the indexing job, or None if absent."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id})
        if doc is None:
            return None
        return IndexingJob.model_validate(_clean_for_model(doc, IndexingJob))

    def _write_job_patch(self, job_id: str, patch: JobPatch) -> IndexingJob:
        """``$set`` exactly the patch's fields in ONE server-side update.

        No lock, and none would help: the API runs several worker processes, so
        a process-local lock proves nothing about the writer next door. Mongo's
        per-document atomicity is the mechanism instead — a ``$set`` naming only
        these fields leaves every field it does not name exactly as another
        writer left it, which removes the lost update without any locking at
        all. A ``$set`` of the whole dumped model instead is what makes two
        writers collide on fields neither has touched.

        Returning the AFTER document is part of the same property: the caller
        gets what is actually stored, a concurrent writer's fields included,
        rather than a locally merged guess that would report them reverted.
        """
        from pymongo import ReturnDocument

        doc = self._col("wiki_jobs").find_one_and_update(
            {"job_id": job_id},
            {"$set": patch.fields},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise KeyError(f"Job not found: {job_id}")
        return IndexingJob.model_validate(_clean_for_model(doc, IndexingJob))

    def list_jobs(self, slug: str | None = None) -> list[IndexingJob]:
        """Return all jobs, newest first by ``phase_started_at``, filtered to *slug*.

        Mongo ``find()`` has no inherent order, and sorting by ``job_id`` for
        reproducibility would not fix that: ``job_id`` is a ``uuid4`` hex, which
        sorts RANDOMLY with respect to when a job actually ran, and at least one
        caller (``resolve_qa_clone_dir``) assumes this returns most-recent-first
        and picks the FIRST ``complete`` hit — under such a sort an arbitrary
        old checkout. Sorted in Python
        rather than via a Mongo-side ``.sort()`` so both drivers apply the
        IDENTICAL rule (ISO-8601 ``phase_started_at``, so lexicographic ==
        chronological; a job with no timestamp sorts last) instead of relying
        on each backend's own null-ordering semantics to happen to agree.
        """
        query: dict[str, Any] = {}
        if slug is not None:
            query["slug"] = slug
        jobs: list[IndexingJob] = []
        for doc in self._col("wiki_jobs").find(query):
            jobs.append(IndexingJob.model_validate(_clean_for_model(doc, IndexingJob)))
        return sorted(jobs, key=lambda j: j.phase_started_at or "", reverse=True)

    def _append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append one validated event to this Mongo job timeline. Cost: ``O(1)``."""
        idx = self._atomic_next_idx("wiki_jobs", "job_id", job_id)
        self._col("wiki_job_events").insert_one({"job_id": job_id, "idx": idx, **event})
        return idx

    def load_job_events(
        self, job_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return job events with idx > *after_idx* (-1 returns all)."""
        query: dict[str, Any] = {"job_id": job_id, "idx": {"$gt": after_idx}}
        results: list[dict[str, Any]] = []
        for doc in self._col("wiki_job_events").find(query).sort("idx", 1):
            clean = _strip_mongo_meta(doc)
            clean.pop("job_id", None)
            results.append(clean)
        return results

    def cancel_job(self, job_id: str) -> bool:
        """Cancel *job_id*; return True on first cancel, False if already cancelled."""
        job = self.get_job(job_id)
        if job is None:
            return False
        if job.status == "cancelled":
            return False
        self.update_job(job_id, status="cancelled")
        self.append_job_event(job_id, {"type": "cancelled"})
        return True

    def attach_job_session(self, job_id: str, session_id: str) -> None:
        """Associate a Mewbo session_id with an indexing job."""
        self._col("wiki_jobs").update_one(
            {"job_id": job_id},
            {"$set": {"session_id": session_id}},
        )

    def get_job_session(self, job_id: str) -> str | None:
        """Return the session_id attached to *job_id*, or None."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"session_id": 1})
        if doc is None:
            return None
        val = doc.get("session_id")
        return str(val) if val else None

    def find_job_by_session(self, session_id: str) -> str | None:
        """Reverse lookup: return the job_id for *session_id*, or None."""
        doc = self._col("wiki_jobs").find_one(
            {"session_id": session_id}, {"job_id": 1}
        )
        if doc is None:
            return None
        val = doc.get("job_id")
        return str(val) if val else None

    def save_job_plan(self, job_id: str, plan: list[dict[str, Any]]) -> None:
        """Persist the page-plan list for *job_id*; overwrites any previous plan."""
        self._col("wiki_jobs").update_one(
            {"job_id": job_id},
            {"$set": {"plan": plan}},
        )

    def get_job_plan(self, job_id: str) -> list[dict[str, Any]] | None:
        """Return the page-plan list, or None if no plan has been committed yet."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"plan": 1})
        if doc is None:
            return None
        plan = doc.get("plan")
        return plan if isinstance(plan, list) else None

    def save_resume_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the resume-plan dict on the job doc; overwrites any previous one."""
        self._col("wiki_jobs").update_one(
            {"job_id": job_id},
            {"$set": {"resume_plan": plan}},
        )

    def get_resume_plan(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted resume-plan dict, or None if the job isn't resuming."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"resume_plan": 1})
        if doc is None:
            return None
        val = doc.get("resume_plan")
        return val if isinstance(val, dict) else None

    def save_act_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the act-stage record on the job doc; overwrites any previous one."""
        self._col("wiki_jobs").update_one(
            {"job_id": job_id},
            {"$set": {"act_plan": plan}},
        )

    def get_act_plan(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted act-stage record, or None if stage 1 hasn't finished."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"act_plan": 1})
        if doc is None:
            return None
        val = doc.get("act_plan")
        return val if isinstance(val, dict) else None

    def get_job_submitted_count(self, job_id: str) -> int:
        """Return the number of pages submitted so far for *job_id*."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"submitted_pages": 1})
        if doc is None:
            return 0
        return int(doc.get("submitted_pages", 0))

    def claim_job_page(self, slug: str, job_id: str, page_id: str) -> PageClaim:
        """Claim + count in ONE conditional update, so racing writers can't double-count."""
        from pymongo import ReturnDocument

        self._seed_claim_record(slug, job_id)
        doc = self._col("wiki_jobs").find_one_and_update(
            {"job_id": job_id, "submitted_page_ids": {"$ne": page_id}},
            {"$addToSet": {"submitted_page_ids": page_id}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            # No match means one of two things, and they are not
            # interchangeable: the id is already claimed (a re-submit — the
            # common case), or the job does not exist (a programming error every
            # other job-keyed write raises on). Read once more to tell them
            # apart rather than reporting a missing job as a quiet no-op claim.
            doc = self._col("wiki_jobs").find_one(
                {"job_id": job_id}, {"submitted_page_ids": 1}
            )
            if doc is None:
                raise KeyError(f"Job not found: {job_id}")
            return PageClaim(count=len(doc.get("submitted_page_ids") or []), is_new=False)
        count = len(doc.get("submitted_page_ids") or [])
        # Mirrored, never authoritative — the SET is the count, so the cheap
        # ``get_job_submitted_count`` read can never disagree with it.
        self._col("wiki_jobs").update_one(
            {"job_id": job_id}, {"$set": {"submitted_pages": count}}
        )
        return PageClaim(count=count, is_new=True)

    def _seed_claim_record(self, slug: str, job_id: str) -> None:
        """Give a pre-claim job its claim set from attribution, once.

        ``{"submitted_page_ids": {"$ne": <id>}}`` matches a document that lacks
        the field ENTIRELY, so without this a job written before claims existed
        would claim its way up from zero while its already-written pages sat
        unaccounted — reporting a fraction of its real progress and, on resume,
        regenerating pages it had already produced correctly.

        Idempotent and race-safe: the filter requires the field to be absent, so
        a second caller seeding concurrently either writes the same derived list
        or does nothing.
        """
        seed = self.page_ids_for_job(slug, job_id)
        if not seed:
            return
        self._col("wiki_jobs").update_one(
            {"job_id": job_id, "submitted_page_ids": {"$exists": False}},
            {"$set": {"submitted_page_ids": sorted(seed)}},
        )

    def get_job_page_ids(self, slug: str, job_id: str) -> frozenset[str]:
        """Return the page ids *job_id* wrote (claim record, else attribution)."""
        doc = self._col("wiki_jobs").find_one(
            {"job_id": job_id}, {"submitted_page_ids": 1}
        )
        if doc is None:
            return frozenset()
        ids = doc.get("submitted_page_ids")
        if ids is None:
            return self.page_ids_for_job(slug, job_id)
        return frozenset(str(p) for p in ids)

    def page_ids_for_job(self, slug: str, job_id: str) -> frozenset[str]:
        """Page ids whose stored attribution column names *job_id*."""
        return frozenset(
            str(d["page_id"])
            for d in self._col("wiki_pages").find(
                {"slug": slug, "job_id": job_id}, {"page_id": 1}
            )
            if d.get("page_id")
        )

    def save_job_submission(self, job_id: str, submission: dict[str, Any]) -> None:
        """Persist the wizard submission dict for *job_id* (token must be absent)."""
        self._col("wiki_jobs").update_one(
            {"job_id": job_id},
            {"$set": {"submission": submission}},
        )

    def get_job_submission(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted submission dict, or None if not yet saved."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"submission": 1})
        if doc is None:
            return None
        val = doc.get("submission")
        return val if isinstance(val, dict) else None

    # -- Repository credentials (isolated collection) ------------------------

    def save_credentials(self, slug: str, blob: dict[str, Any]) -> None:
        """Persist the encoded credential blob for *slug* (slug PK, upsert)."""
        self._col("wiki_credentials").replace_one(
            {"slug": slug}, {"slug": slug, "blob": blob}, upsert=True
        )

    def get_credentials(self, slug: str) -> dict[str, Any] | None:
        """Return the encoded credential blob for *slug*, or None."""
        doc = self._col("wiki_credentials").find_one({"slug": slug}, {"blob": 1})
        if doc is None:
            return None
        val = doc.get("blob")
        return val if isinstance(val, dict) else None

    def delete_credentials(self, slug: str) -> bool:
        """Delete *slug*'s credential document; return True if one existed."""
        result = self._col("wiki_credentials").delete_one({"slug": slug})
        return result.deleted_count > 0

    def list_credentials(self) -> dict[str, dict[str, Any]]:
        """Return every stored credential blob keyed by scope (full scan).

        Prefers the blob's own ``scope`` field (stamped by
        ``CredentialStore.save``, matching the JSON driver's precedence); falls
        back to the document's ``slug`` key for a blob missing that field.
        """
        out: dict[str, dict[str, Any]] = {}
        for doc in self._col("wiki_credentials").find({}, {"slug": 1, "blob": 1}):
            slug = doc.get("slug")
            blob = doc.get("blob")
            if isinstance(blob, dict):
                scope = blob["scope"] if isinstance(blob.get("scope"), str) else slug
                if isinstance(scope, str):
                    out[scope] = blob
        return out

    # -- Restart-recovery counter (slug-keyed collection) --------------------

    def get_recovery_attempts(self, slug: str) -> int:
        """Return the recovery-attempt count for *slug* (0 if never recovered)."""
        doc = self._col("wiki_recovery").find_one({"slug": slug}, {"attempts": 1})
        return int(doc.get("attempts", 0)) if doc else 0

    def bump_recovery_attempts(self, slug: str) -> int:
        """Atomically increment *slug*'s recovery counter; return the new value."""
        from pymongo import ReturnDocument

        doc = self._col("wiki_recovery").find_one_and_update(
            {"slug": slug},
            {"$inc": {"attempts": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["attempts"])

    def reset_recovery_attempts(self, slug: str) -> None:
        """Clear *slug*'s recovery counter document (user-initiated resume fresh budget)."""
        self._col("wiki_recovery").delete_one({"slug": slug})

    # -- QA ------------------------------------------------------------------

    def save_qa(self, answer: QaAnswer) -> None:
        """Persist a QA answer record (creation: resets event_count, no session yet)."""
        doc = {"event_count": 0, **answer.model_dump(by_alias=False)}
        self._col("wiki_qa").replace_one(
            {"answer_id": answer.answer_id}, doc, upsert=True
        )

    def update_qa_fields(self, answer: QaAnswer) -> None:
        """In-place ``$set`` of the QaAnswer fields only.

        Leaves ``event_count`` + ``session_id`` (this backend packs both into the
        same doc) intact, unlike ``save_qa``'s full replace.
        """
        self._col("wiki_qa").update_one(
            {"answer_id": answer.answer_id},
            {"$set": answer.model_dump(by_alias=False)},
        )

    def get_qa(self, answer_id: str) -> QaAnswer | None:
        """Return the QA answer, or None if absent."""
        doc = self._col("wiki_qa").find_one({"answer_id": answer_id})
        if doc is None:
            return None
        return QaAnswer.model_validate(_clean_for_model(doc, QaAnswer))

    def list_qa(self, status: str | None = None) -> list[QaAnswer]:
        """Return all QA answers, optionally filtered to *status* (server-side)."""
        query: dict[str, Any] = {} if status is None else {"status": status}
        return [
            QaAnswer.model_validate(_clean_for_model(doc, QaAnswer))
            for doc in self._col("wiki_qa").find(query)
        ]

    def attach_qa_session(self, answer_id: str, session_id: str) -> None:
        """Associate a Mewbo session_id with a QA answer."""
        self._col("wiki_qa").update_one(
            {"answer_id": answer_id},
            {"$set": {"session_id": session_id}},
        )

    def get_qa_session(self, answer_id: str) -> str | None:
        """Return the session_id attached to *answer_id*, or None."""
        doc = self._col("wiki_qa").find_one({"answer_id": answer_id}, {"session_id": 1})
        if doc is None:
            return None
        val = doc.get("session_id")
        return str(val) if val else None

    def find_qa_by_session(self, session_id: str) -> str | None:
        """Reverse lookup: return the answer_id for *session_id*, or None."""
        doc = self._col("wiki_qa").find_one(
            {"session_id": session_id}, {"answer_id": 1}
        )
        if doc is None:
            return None
        val = doc.get("answer_id")
        return str(val) if val else None

    def append_qa_event(self, answer_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the QA event log; return the monotonic idx."""
        idx = self._atomic_next_idx("wiki_qa", "answer_id", answer_id)
        self._col("wiki_qa_events").insert_one(
            {"answer_id": answer_id, "idx": idx, **event}
        )
        return idx

    def load_qa_events(
        self, answer_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return QA events with idx > *after_idx* (-1 returns all)."""
        query: dict[str, Any] = {"answer_id": answer_id, "idx": {"$gt": after_idx}}
        results: list[dict[str, Any]] = []
        for doc in self._col("wiki_qa_events").find(query).sort("idx", 1):
            clean = _strip_mongo_meta(doc)
            clean.pop("answer_id", None)
            results.append(clean)
        return results

    # -- Graph + embeddings --------------------------------------------------

    def _ensure_graph_indexes(self) -> None:
        """Create graph collection indexes (called lazily on first upsert)."""
        if getattr(self, "_graph_idx_done", False):
            return
        from pymongo import ASCENDING

        self._col("wiki_graph_nodes").create_index(
            [("slug", ASCENDING), ("node_id", ASCENDING)],
            name="ix_graph_nodes_slug_nid",
            unique=True,
            background=True,
        )
        self._col("wiki_graph_edges").create_index(
            [
                ("slug", ASCENDING),
                ("source", ASCENDING),
                ("target", ASCENDING),
                ("type", ASCENDING),
            ],
            name="ix_graph_edges_slug_src_tgt_type",
            unique=True,
            background=True,
        )
        self._col("wiki_embeddings").create_index(
            [("slug", ASCENDING), ("node_id", ASCENDING)],
            name="ix_embeddings_slug_nid",
            unique=True,
            background=True,
        )
        # Non-unique (slug, commit_sha) indexes back the commit-scoped count the
        # resume predicate keys on AND the per-commit supersede sweep. The unique
        # keys above are unchanged — a node_id still identifies ONE row per slug,
        # so a re-index overwrites the shared symbol and supersede reaps only the
        # commit-only stragglers (deleted files).
        for coll in ("wiki_graph_nodes", "wiki_graph_edges", "wiki_embeddings"):
            self._col(coll).create_index(
                [("slug", ASCENDING), ("commit_sha", ASCENDING)],
                name="ix_" + coll + "_slug_commit",
                background=True,
            )
        self._graph_idx_done = True

    def _bulk_upsert(
        self,
        collection: Any,
        ops: Iterable[tuple[dict[str, Any], dict[str, Any]]],
        *,
        total_batches: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Apply ``(filter, document)`` ``$set`` upserts in bounded unordered batches.

        Cost: ``O(documents written)`` in server work, but
        ``ceil(n / _BULK_BATCH_SIZE)`` round-trips rather than ``n`` — a graph
        phase writes ~130k documents, and one round-trip apiece dominated the
        phase's write leg on a local socket, worse still over a network. Each
        op is the same single-document ``$set`` upsert a per-document loop
        issues, so the unique indexes still do the dedup and re-writing a
        document is still idempotent.

        The batch bound is what keeps memory ``O(_BULK_BATCH_SIZE)`` instead of
        ``O(all documents)``: *ops* is consumed lazily, so a caller streaming
        79k edges never materialises 79k pending operations.

        **A batch is keyed by its filter, keeping the LAST write.** A
        per-document loop applied two updates to one key in input order, so the
        last one won; ``ordered=False`` explicitly permits out-of-order or
        parallel execution, and MongoDB does not document that two operations
        sharing a filter in one batch resolve in list order. Folding here keeps
        last-write-wins a property of THIS code rather than of a server
        version — the same guarantee the loop gave, at the cost of one dict.
        Across batches it needs no help: batches are issued sequentially, so a
        later batch's write lands after an earlier one's.
        """
        from pymongo import UpdateOne

        batch: dict[tuple[tuple[str, Any], ...], Any] = {}
        completed = 0
        for filt, doc in ops:
            batch[tuple(sorted(filt.items()))] = UpdateOne(
                filt, {"$set": doc}, upsert=True
            )
            if len(batch) >= self._BULK_BATCH_SIZE:
                collection.bulk_write(list(batch.values()), ordered=False)
                completed += 1
                if on_progress is not None and total_batches is not None:
                    on_progress(completed, total_batches)
                batch = {}
        if batch:
            collection.bulk_write(list(batch.values()), ordered=False)
            completed += 1
            if on_progress is not None and total_batches is not None:
                on_progress(completed, total_batches)

    def upsert_nodes(
        self,
        slug: str,
        nodes: Iterable[GraphNode],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upsert graph nodes for *slug*; dedup by (slug, node_id), stamp attribution.

        Cost: ``O(nodes)`` — offline, the ``graph`` phase. Batched through
        ``_bulk_upsert``, so the round-trip count is ``O(nodes / batch)``.
        """
        self._ensure_graph_indexes()
        total_batches = (
            math.ceil(len(nodes) / self._BULK_BATCH_SIZE)
            if isinstance(nodes, Collection)
            else None
        )
        self._bulk_upsert(
            self._col("wiki_graph_nodes"),
            (
                (
                    {"slug": slug, "node_id": node.node_id},
                    self._stamp_attribution(node, commit_sha, job_id).model_dump(by_alias=False),
                )
                for node in nodes
            ),
            total_batches=total_batches,
            on_progress=on_progress,
        )

    def upsert_edges(
        self,
        slug: str,
        edges: Iterable[GraphEdge],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upsert graph edges for *slug*; dedup by (slug, source, target, type).

        Cost: ``O(edges)`` — offline, the ``graph`` phase. Batched through
        ``_bulk_upsert``, so the round-trip count is ``O(edges / batch)``.
        """
        self._ensure_graph_indexes()
        total_batches = (
            math.ceil(len(edges) / self._BULK_BATCH_SIZE)
            if isinstance(edges, Collection)
            else None
        )
        self._bulk_upsert(
            self._col("wiki_graph_edges"),
            (
                (
                    {
                        "slug": slug,
                        "source": edge.source,
                        "target": edge.target,
                        "type": edge.type,
                    },
                    self._stamp_attribution(edge, commit_sha, job_id).model_dump(by_alias=False),
                )
                for edge in edges
            ),
            total_batches=total_batches,
            on_progress=on_progress,
        )

    def upsert_embeddings(
        self,
        slug: str,
        items: Iterable[Embedding],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert embedding vectors for *slug*; dedup by (slug, node_id).

        Cost: ``O(vectors)`` — offline, the ``graph`` phase. Batched through
        ``_bulk_upsert``, so the round-trip count is ``O(vectors / batch)``.
        """
        self._ensure_graph_indexes()
        self._bulk_upsert(
            self._col("wiki_embeddings"),
            (
                (
                    {"slug": slug, "node_id": item.node_id},
                    self._embedding_doc(item, commit_sha, job_id),
                )
                for item in items
            ),
        )

    def _embedding_doc(
        self, item: Embedding, commit_sha: str | None, job_id: str | None
    ) -> dict[str, Any]:
        """The stored embedding document: the model dump plus the packed vector."""
        stamped = self._stamp_attribution(item, commit_sha, job_id)
        doc = stamped.model_dump(by_alias=False)
        # Written alongside the canonical list so ``vector_search`` can skip
        # the BSON-array decode entirely — see ``_VEC_F32``.
        doc[_VEC_F32] = _pack_f32(stamped.vector)
        return doc

    def query_graph(
        self,
        slug: str,
        *,
        scope: CommitScope,
        node_type: str | None = None,
        name_match: str | None = None,
        neighbors_of: str | None = None,
        node_ids: Collection[str] | None = None,
    ) -> list[GraphNode]:
        """See ``WikiStoreBase.query_graph``."""
        import re

        commit = scope.filter_fields()
        if neighbors_of is not None:
            # The seed's own generation bounds the walk — see the JSON driver.
            edge_query = {
                "slug": slug,
                "$or": [{"source": neighbors_of}, {"target": neighbors_of}],
                **commit,
            }
            related_ids: set[str] = set()
            for edge_doc in self._col("wiki_graph_edges").find(edge_query):
                src = edge_doc.get("source")
                tgt = edge_doc.get("target")
                if src == neighbors_of:
                    related_ids.add(tgt)
                else:
                    related_ids.add(src)
            if not related_ids:
                return []
            cursor = self._col("wiki_graph_nodes").find(
                {"slug": slug, "node_id": {"$in": list(related_ids)}, **commit}
            )
            return [GraphNodeAdapter.validate_python(_strip_mongo_meta(d)) for d in cursor]
        query: dict[str, Any] = {"slug": slug, **commit}
        if node_ids is not None:
            query["node_id"] = {"$in": list(node_ids)}
        if node_type is not None:
            query["type"] = node_type
        if name_match is not None:
            query["name"] = {"$regex": re.escape(name_match), "$options": "i"}
        cursor = self._col("wiki_graph_nodes").find(query)
        return [GraphNodeAdapter.validate_python(_strip_mongo_meta(d)) for d in cursor]

    def list_edges(self, slug: str, *, scope: CommitScope) -> list[GraphEdge]:
        """See ``WikiStoreBase.list_edges``."""
        cursor = self._col("wiki_graph_edges").find({"slug": slug, **scope.filter_fields()})
        return [GraphEdge.model_validate(_strip_mongo_meta(d)) for d in cursor]

    def count_graph_nodes(self, slug: str, *, commit_sha: str | None) -> int:
        """Count *slug* nodes stamped exactly *commit_sha* (``None`` matches None)."""
        self._ensure_graph_indexes()
        return int(
            self._col("wiki_graph_nodes").count_documents(
                {"slug": slug, "commit_sha": commit_sha}
            )
        )

    def supersede_graph_artifacts(
        self, slug: str, *, keep_commit_sha: str
    ) -> dict[str, int]:
        """Drop prior-commit graph + entity artifacts, preserving ``None``-stamped rows.

        ``{"$nin": [None, keep]}`` matches a REAL commit other than *keep* while
        leaving both ``None``-valued and field-absent rows untouched — the
        QA-minted / pre-isolation records supersede must not reap.
        """
        self._ensure_graph_indexes()
        self._ensure_memory_indexes()
        stale = {"slug": slug, "commit_sha": {"$nin": [None, keep_commit_sha]}}
        counts: dict[str, int] = {}
        for key, coll in (
            ("nodes", "wiki_graph_nodes"),
            ("edges", "wiki_graph_edges"),
            ("embeddings", "wiki_embeddings"),
            ("entities", "wiki_entities"),
            ("entity_edges", "wiki_entity_edges"),
            ("entity_embeddings", "wiki_entity_embeddings"),
        ):
            counts[key] = int(self._col(coll).delete_many(stale).deleted_count)
        return counts

    def restamp_graph_artifacts(
        self, slug: str, *, from_commit: str, to_commit: str
    ) -> dict[str, int]:
        """See ``WikiStoreBase.restamp_graph_artifacts``.

        An EXACT ``commit_sha`` match, deliberately not the ``$nin`` shape
        ``supersede_graph_artifacts`` uses: supersede asks "everything except
        the keeper", which would here sweep up older generations and
        field-absent rows and stamp genuinely dead code as live.
        """
        self._ensure_graph_indexes()
        self._ensure_memory_indexes()
        prior = {"slug": slug, "commit_sha": from_commit}
        patch = {"$set": {"commit_sha": to_commit}}
        counts: dict[str, int] = {}
        for key, coll in (
            ("nodes", "wiki_graph_nodes"),
            ("edges", "wiki_graph_edges"),
            ("embeddings", "wiki_embeddings"),
            ("entities", "wiki_entities"),
            ("entity_edges", "wiki_entity_edges"),
            ("entity_embeddings", "wiki_entity_embeddings"),
        ):
            counts[key] = int(self._col(coll).update_many(prior, patch).modified_count)
        return counts

    def vector_search(self, slug: str, qvec: list[float], k: int = 10) -> list[Embedding]:
        """Return top-k embeddings for *slug* by cosine similarity.

        Cost: ``O(embeddings for the slug)`` — every stored vector is still
        scored, so this remains the documented scale seam. What the packed path
        removes is the per-element Python cost of getting there: it reads only
        ``node_id`` + the ``_VEC_F32`` buffer, scores the whole project as one
        NumPy matrix product, and materialises exactly *k* ``Embedding`` models.

        Falls back to the pure-Python scan when NumPy is absent or any row
        lacks ``_VEC_F32`` — never to a PARTIAL result.
        """
        from .embedder import Embedder

        packed = self._vector_search_packed(slug, qvec, k)
        if packed is not None:
            return packed
        pool = [
            Embedding.model_validate(_clean_for_model(d, Embedding))
            for d in self._col("wiki_embeddings").find({"slug": slug})
        ]
        if not pool:
            return []
        scored = [(emb, Embedder.cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

    def _vector_search_packed(
        self, slug: str, qvec: list[float], k: int
    ) -> list[Embedding] | None:
        """Top-k over the packed ``_VEC_F32`` buffers, or ``None`` if unusable.

        ``None`` means "this path cannot answer" — the caller falls back. It is
        returned rather than a short result whenever ANY row is missing or
        mis-sized, because scoring the subset that happens to be packed would
        silently search part of a project and still look like a complete answer.
        """
        try:
            import numpy as np  # noqa: PLC0415
        except ImportError:  # pragma: no cover - numpy ships with the retrieval extra
            return None
        if not qvec:
            return None
        width = len(qvec) * 4
        col = self._col("wiki_embeddings")
        ids: list[str] = []
        bufs: list[bytes] = []
        for d in col.find({"slug": slug}, {"node_id": 1, _VEC_F32: 1}):
            buf = d.get(_VEC_F32)
            if not isinstance(buf, bytes | bytearray) or len(buf) != width:
                return None
            ids.append(str(d["node_id"]))
            bufs.append(bytes(buf))
        if not ids:
            return None
        mat = np.frombuffer(b"".join(bufs), dtype="<f4").reshape(len(ids), len(qvec))
        q = np.asarray(qvec, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1) * float(np.linalg.norm(q))
        sims = np.divide(
            mat @ q, norms, out=np.zeros(len(ids), dtype=np.float32), where=norms != 0
        )
        # STABLE sort, matching the pure-Python path's ``sorted(..., reverse=True)``.
        # This project's vectors do produce exact score ties, and an unstable sort
        # reorders them — the two paths would then disagree on identical data.
        order = np.argsort(-sims, kind="stable")[:k]
        top = [ids[int(i)] for i in order]
        rank = {node_id: r for r, node_id in enumerate(top)}
        found = [
            Embedding.model_validate(_clean_for_model(d, Embedding))
            for d in col.find({"slug": slug, "node_id": {"$in": top}})
        ]
        found.sort(key=lambda e: rank[e.node_id])
        return found

    # -- Scoped graph deletes (incremental retract) --------------------------

    def delete_nodes_by_file(self, slug: str, file: str) -> int:
        """Delete *file*'s code nodes AND their vectors; return the NODE count."""
        # Read the ids BEFORE the delete — the same node→file join
        # ``delete_edges_by_source_file`` does, for the same reason: after the
        # nodes are gone nothing relates a vector back to a file.
        doomed = [
            d["node_id"]
            for d in self._col("wiki_graph_nodes").find(
                {"slug": slug, "file": file}, {"node_id": 1}
            )
        ]
        if not doomed:
            return 0
        result = self._col("wiki_graph_nodes").delete_many({"slug": slug, "file": file})
        self._col("wiki_embeddings").delete_many(
            {"slug": slug, "node_id": {"$in": doomed}}
        )
        return int(result.deleted_count)

    def delete_edges_by_source_file(self, slug: str, file: str) -> int:
        """Delete edges whose ``source`` node belongs to *file*; return count."""
        file_ids = [
            d["node_id"]
            for d in self._col("wiki_graph_nodes").find(
                {"slug": slug, "file": file}, {"node_id": 1}
            )
        ]
        if not file_ids:
            return 0
        result = self._col("wiki_graph_edges").delete_many(
            {"slug": slug, "source": {"$in": file_ids}}
        )
        return int(result.deleted_count)

    # -- Memory layer (multiplex overlay) ------------------------------------

    def _ensure_memory_indexes(self) -> None:
        """Create memory-layer collection indexes (lazy, on first upsert)."""
        if getattr(self, "_mem_idx_done", False):
            return
        from pymongo import ASCENDING

        self._col("wiki_memory_nodes").create_index(
            [("slug", ASCENDING), ("node_id", ASCENDING)],
            name="ix_mem_nodes_slug_nid", unique=True, background=True,
        )
        self._col("wiki_memory_edges").create_index(
            [("slug", ASCENDING), ("source", ASCENDING), ("target", ASCENDING),
             ("type", ASCENDING)],
            name="ix_mem_edges_key", unique=True, background=True,
        )
        self._col("wiki_memory_edges").create_index(
            [("slug", ASCENDING), ("type", ASCENDING), ("target", ASCENDING)],
            name="ix_mem_edges_anchor", background=True,
        )
        self._col("wiki_memory_embeddings").create_index(
            [("slug", ASCENDING), ("node_id", ASCENDING)],
            name="ix_mem_emb_slug_nid", unique=True, background=True,
        )
        self._col("wiki_doc_notes").create_index(
            [("slug", ASCENDING), ("page_id", ASCENDING)],
            name="ix_doc_notes_slug_pid", unique=True, background=True,
        )
        self._col("wiki_file_manifest").create_index(
            [("slug", ASCENDING), ("path", ASCENDING)],
            name="ix_manifest_slug_path", unique=True, background=True,
        )
        # Abstract-entity overlay collections (same lazy-index pattern).
        self._col("wiki_entities").create_index(
            [("slug", ASCENDING), ("id", ASCENDING)],
            name="ix_entities_slug_id", unique=True, background=True,
        )
        self._col("wiki_entity_embeddings").create_index(
            [("slug", ASCENDING), ("entity_id", ASCENDING)],
            name="ix_entity_emb_slug_eid", unique=True, background=True,
        )
        self._col("wiki_entity_edges").create_index(
            [("slug", ASCENDING), ("id", ASCENDING)],
            name="ix_entity_edges_slug_id", unique=True, background=True,
        )
        self._col("wiki_entity_recommendations").create_index(
            [("slug", ASCENDING)],
            name="ix_entity_recs_slug", background=True,
        )
        # Backs the keyed recommendation upsert. Deliberately NOT unique, unlike
        # its entity/edge siblings: recommendations were appended unkeyed for
        # long enough that a live collection can already hold duplicate rows,
        # and a unique index build fails outright on those — taking every other
        # index in this method down with it. The upsert converges new writes; a
        # dedup of the historical rows is a migration, not an index.
        self._col("wiki_entity_recommendations").create_index(
            [("slug", ASCENDING), ("id", ASCENDING)],
            name="ix_entity_recs_slug_id", background=True,
        )
        # Non-unique (slug, commit_sha) indexes back the commit-scoped count the
        # resume predicate keys on AND the per-commit supersede sweep, mirroring
        # the graph collections in _ensure_graph_indexes. The unique keys above
        # are unchanged — an entity id still identifies ONE row per slug, so a
        # re-index overwrites the shared entity and supersede reaps only the
        # commit-only stragglers.
        for coll in ("wiki_entities", "wiki_entity_edges", "wiki_entity_embeddings"):
            self._col(coll).create_index(
                [("slug", ASCENDING), ("commit_sha", ASCENDING)],
                name="ix_" + coll + "_slug_commit",
                background=True,
            )
        self._mem_idx_done = True

    def upsert_memory_nodes(self, slug: str, nodes: Iterable[MemoryNode]) -> None:
        """Upsert memory nodes for *slug*; dedup by (slug, node_id)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_memory_nodes")
        for node in nodes:
            col.update_one(
                {"slug": slug, "node_id": node.node_id},
                {"$set": node.model_dump(by_alias=False)},
                upsert=True,
            )

    def get_memory_node(self, slug: str, node_id: str) -> MemoryNode | None:
        """Return a single memory node, or None if absent."""
        doc = self._col("wiki_memory_nodes").find_one({"slug": slug, "node_id": node_id})
        if doc is None:
            return None
        return MemoryNode.model_validate(_strip_mongo_meta(doc))

    def delete_memory_node(self, slug: str, node_id: str) -> bool:
        """Delete a memory node + its embedding; return True if one was removed."""
        result = self._col("wiki_memory_nodes").delete_one(
            {"slug": slug, "node_id": node_id}
        )
        self._col("wiki_memory_embeddings").delete_one(
            {"slug": slug, "node_id": node_id}
        )
        return result.deleted_count > 0

    def query_memory(
        self, slug: str, *, filt: MemoryFilter | None = None
    ) -> list[MemoryNode]:
        """Return memory nodes matching *filt*'s node-level facets."""
        nodes = [
            MemoryNode.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_memory_nodes").find({"slug": slug})
        ]
        if filt is None:
            return nodes
        return [n for n in nodes if filt.matches_node(n)]

    def upsert_memory_edges(self, slug: str, edges: Iterable[MemoryEdge]) -> None:
        """Upsert memory edges for *slug*; dedup by (slug, source, target, type)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_memory_edges")
        for edge in edges:
            col.update_one(
                {"slug": slug, "source": edge.source, "target": edge.target,
                 "type": edge.type},
                {"$set": edge.model_dump(by_alias=False)},
                upsert=True,
            )

    def list_memory_edges(
        self,
        slug: str,
        *,
        node_id: str | None = None,
        include_invalidated: bool = False,
    ) -> list[MemoryEdge]:
        """Return memory edges, optionally scoped to ``source == node_id``."""
        query: dict[str, Any] = {"slug": slug}
        if node_id is not None:
            query["source"] = node_id
        if not include_invalidated:
            query["invalid_at"] = None
        return [
            MemoryEdge.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_memory_edges").find(query)
        ]

    def memories_anchored_to(
        self,
        slug: str,
        entity_keys: Iterable[EntityKey],
        *,
        include_invalidated: bool = False,
    ) -> list[str]:
        """Reverse ANCHORS lookup: entity_keys → distinct memory node_ids."""
        query: dict[str, Any] = {
            "slug": slug, "type": "ANCHORS", "target": {"$in": list(entity_keys)}
        }
        if not include_invalidated:
            query["invalid_at"] = None
        seen: list[str] = []
        seen_set: set[str] = set()
        for d in self._col("wiki_memory_edges").find(query):
            src = d.get("source")
            if src not in seen_set:
                seen_set.add(src)
                seen.append(src)
        return seen

    def _live_anchored_ids(self, slug: str) -> set[str]:
        """Memory node_ids with ≥1 live ANCHORS edge."""
        return {
            d["source"]
            for d in self._col("wiki_memory_edges").find(
                {"slug": slug, "type": "ANCHORS", "invalid_at": None}, {"source": 1}
            )
        }

    def upsert_memory_embeddings(
        self, slug: str, items: Iterable[MemoryEmbedding]
    ) -> None:
        """Upsert memory embedding vectors for *slug*; dedup by (slug, node_id)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_memory_embeddings")
        for item in items:
            col.update_one(
                {"slug": slug, "node_id": item.node_id},
                {"$set": item.model_dump(by_alias=False)},
                upsert=True,
            )

    def memory_vector_search(
        self,
        slug: str,
        qvec: list[float],
        k: int = 10,
        *,
        filt: MemoryFilter | None = None,
    ) -> list[MemoryEmbedding]:
        """Top-k memory embeddings by cosine, after applying *filt*."""
        pool = [
            MemoryEmbedding.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_memory_embeddings").find({"slug": slug})
        ]
        return self._rank_memory(slug, pool, qvec, k, filt)

    # -- Doc-page notes ------------------------------------------------------

    def upsert_doc_notes(self, slug: str, notes: Iterable[DocPageNote]) -> None:
        """Upsert doc-page notes for *slug*; dedup by (slug, page_id)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_doc_notes")
        for note in notes:
            col.update_one(
                {"slug": slug, "page_id": note.page_id},
                {"$set": note.model_dump(by_alias=False)},
                upsert=True,
            )

    def get_doc_note(self, slug: str, page_id: str) -> DocPageNote | None:
        """Return a single doc-page note, or None if absent."""
        doc = self._col("wiki_doc_notes").find_one({"slug": slug, "page_id": page_id})
        if doc is None:
            return None
        return DocPageNote.model_validate(_strip_mongo_meta(doc))

    def list_doc_notes(self, slug: str) -> list[DocPageNote]:
        """Return every doc-page note for *slug*."""
        return [
            DocPageNote.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_doc_notes").find({"slug": slug})
        ]

    def delete_doc_note(self, slug: str, page_id: str) -> bool:
        """Delete a doc-page note; return True if one was removed."""
        result = self._col("wiki_doc_notes").delete_one(
            {"slug": slug, "page_id": page_id}
        )
        return result.deleted_count > 0

    # -- File manifest -------------------------------------------------------

    def upsert_file_manifest(
        self, slug: str, entries: Iterable[FileManifest]
    ) -> None:
        """Upsert file-manifest entries for *slug*; dedup by (slug, path)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_file_manifest")
        for entry in entries:
            col.update_one(
                {"slug": slug, "path": entry.path},
                {"$set": entry.model_dump(by_alias=False)},
                upsert=True,
            )

    def get_file_manifest(self, slug: str, path: str) -> FileManifest | None:
        """Return a single file-manifest entry, or None if absent."""
        doc = self._col("wiki_file_manifest").find_one({"slug": slug, "path": path})
        if doc is None:
            return None
        return FileManifest.model_validate(_strip_mongo_meta(doc))

    def list_file_manifest(self, slug: str) -> list[FileManifest]:
        """Return every file-manifest entry for *slug*."""
        return [
            FileManifest.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_file_manifest").find({"slug": slug})
        ]

    def delete_file_manifest(self, slug: str, path: str) -> bool:
        """Delete a file-manifest entry; return True if one was removed."""
        result = self._col("wiki_file_manifest").delete_one(
            {"slug": slug, "path": path}
        )
        return result.deleted_count > 0

    # -- Abstract-entity layer (multiplex overlay) ---------------------------
    #
    # Mirrors the memory-node Mongo block exactly: per-(slug, key) upsert,
    # ``slug`` carried as a store-internal field and stripped on read.

    def upsert_entities(
        self,
        slug: str,
        entities: Iterable[Entity],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entities for *slug*; dedup by (slug, id), stamp attribution."""
        self._ensure_memory_indexes()
        col = self._col("wiki_entities")
        for entity in entities:
            stamped = self._stamp_attribution(entity, commit_sha, job_id)
            col.update_one(
                {"slug": slug, "id": entity.id},
                {"$set": {"slug": slug, **stamped.model_dump(by_alias=False)}},
                upsert=True,
            )

    def get_entity(self, slug: str, entity_id: str) -> Entity | None:
        """Return a single entity, or None if absent."""
        doc = self._col("wiki_entities").find_one({"slug": slug, "id": entity_id})
        if doc is None:
            return None
        clean = _strip_mongo_meta(doc)
        clean.pop("slug", None)
        return Entity.model_validate(clean)

    def query_entities(
        self, slug: str, *, filt: EntityFilter | None = None
    ) -> list[Entity]:
        """Return entities matching *filt*'s facets."""
        out: list[Entity] = []
        for doc in self._col("wiki_entities").find({"slug": slug}):
            clean = _strip_mongo_meta(doc)
            clean.pop("slug", None)
            out.append(Entity.model_validate(clean))
        if filt is None:
            return out
        return [e for e in out if filt.matches(e)]

    def count_entities(self, slug: str, *, commit_sha: str | None) -> int:
        """Count *slug* entities stamped exactly *commit_sha* (``None`` matches None)."""
        self._ensure_memory_indexes()
        return int(
            self._col("wiki_entities").count_documents(
                {"slug": slug, "commit_sha": commit_sha}
            )
        )

    def upsert_entity_embeddings(
        self,
        slug: str,
        items: Iterable[EntityEmbedding],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entity embedding vectors for *slug*; dedup by (slug, entity_id)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_entity_embeddings")
        for item in items:
            stamped = self._stamp_attribution(item, commit_sha, job_id)
            col.update_one(
                {"slug": slug, "entity_id": item.entity_id},
                {"$set": stamped.model_dump(by_alias=False)},
                upsert=True,
            )

    def entity_vector_search(
        self, slug: str, qvec: list[float], k: int = 10
    ) -> list[EntityEmbedding]:
        """Return top-k entity embeddings for *slug* by cosine (in-memory scoring)."""
        pool = [
            EntityEmbedding.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_entity_embeddings").find({"slug": slug})
        ]
        return self._rank_embeddings(pool, qvec, k)

    def upsert_entity_edges(
        self,
        slug: str,
        edges: Iterable[EntityRelation],
        *,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Upsert entity relations for *slug*; dedup by (slug, id)."""
        self._ensure_memory_indexes()
        col = self._col("wiki_entity_edges")
        for edge in edges:
            stamped = self._stamp_attribution(edge, commit_sha, job_id)
            col.update_one(
                {"slug": slug, "id": edge.id},
                {"$set": {"slug": slug, **stamped.model_dump(by_alias=False)}},
                upsert=True,
            )

    def list_entity_edges(
        self, slug: str, *, source_id: str | None = None
    ) -> list[EntityRelation]:
        """Return entity relations, optionally scoped to ``source_id``."""
        query: dict[str, Any] = {"slug": slug}
        if source_id is not None:
            query["source_id"] = source_id
        out: list[EntityRelation] = []
        for d in self._col("wiki_entity_edges").find(query):
            clean = _strip_mongo_meta(d)
            clean.pop("slug", None)
            out.append(EntityRelation.model_validate(clean))
        return out

    def save_entity_recommendation(
        self, slug: str, rec: EntityRecommendation
    ) -> None:
        """Upsert a recommendation for *slug*; dedup by (slug, id)."""
        self._ensure_memory_indexes()
        self._col("wiki_entity_recommendations").update_one(
            {"slug": slug, "id": rec.id},
            {"$set": {"slug": slug, **rec.model_dump(by_alias=False)}},
            upsert=True,
        )

    def get_entity_recommendations(self, slug: str) -> list[EntityRecommendation]:
        """Return every persisted entity recommendation for *slug*."""
        out: list[EntityRecommendation] = []
        for d in self._col("wiki_entity_recommendations").find({"slug": slug}):
            clean = _strip_mongo_meta(d)
            clean.pop("slug", None)
            out.append(EntityRecommendation.model_validate(clean))
        return out

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_wiki_store() -> WikiStoreBase:
    """Return the configured wiki store driver.

    Reads ``storage.driver`` from the app config. Defaults to ``"json"``
    (filesystem). Set to ``"mongodb"`` to use MongoDB.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        return MongoWikiStore()
    return JsonWikiStore()


# ---------------------------------------------------------------------------
# Process-wide singleton (DI seam shared by API + relocated plugins)
# ---------------------------------------------------------------------------

_WIKI_STORE: WikiStoreBase | None = None


def get_wiki_store() -> WikiStoreBase:
    """Return the process-wide wiki store, constructing it on first use.

    The single instance both the API routes and the wiki SessionTools share
    — the same singleton+factory+``reset_for_tests`` shape as the SCG store
    and the run store. It lets the relocated plugins reach the store **down**
    through this factory instead of up through the API runtime; the JSON/Mongo
    backend is config-addressed, so a fresh instance still sees the same data.
    """
    global _WIKI_STORE
    if _WIKI_STORE is None:
        _WIKI_STORE = create_wiki_store()
    return _WIKI_STORE


def set_wiki_store(store: WikiStoreBase | None) -> None:
    """Pin the process-wide wiki store (API startup wiring / test injection)."""
    global _WIKI_STORE
    _WIKI_STORE = store


def reset_for_tests(root_dir: str | Path | None = None) -> WikiStoreBase:
    """Swap in a fresh JSON store (under *root_dir* if given) for test isolation."""
    store = JsonWikiStore(root_dir=root_dir) if root_dir is not None else JsonWikiStore()
    set_wiki_store(store)
    return store


__all__ = [
    "WikiStoreBase",
    "JsonWikiStore",
    "MongoWikiStore",
    "create_wiki_store",
    "get_wiki_store",
    "set_wiki_store",
    "reset_for_tests",
]
