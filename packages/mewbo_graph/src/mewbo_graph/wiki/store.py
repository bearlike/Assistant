#!/usr/bin/env python3
"""Wiki persistence layer.

JSON-file backed implementation (default) + abstract base for the
MongoDB impl that lands in Task 1.4. Layout under ``$MEWBO_HOME/wiki/``:

    projects/<slug>.json                    (Project model)
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
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from pydantic import BaseModel

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
    Embedding,
    GraphEdge,
    GraphNode,
    IndexingJob,
    Project,
    QaAnswer,
    WikiPage,
)

logging = get_logger(name="api.wiki.store")

_M = TypeVar("_M", bound=BaseModel)


def _slug_to_path(slug: str) -> str:
    """Escape a slug so it maps safely to a single filesystem segment."""
    return slug.replace("/", "__")


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

    # Pages

    @abc.abstractmethod
    def save_page(self, slug: str, page: WikiPage) -> None:
        """Persist *page* for the project *slug*; overwrites if same page_id."""

    @abc.abstractmethod
    def get_page(self, slug: str, page_id: str) -> WikiPage | None:
        """Return a single wiki page, or None if absent."""

    @abc.abstractmethod
    def list_pages(self, slug: str) -> list[WikiPage]:
        """Return all pages for project *slug*."""

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

    @abc.abstractmethod
    def update_job(self, job_id: str, **fields: Any) -> IndexingJob:
        """Partially update *job_id* with *fields*; return the updated record."""

    @abc.abstractmethod
    def list_jobs(self, slug: str | None = None) -> list[IndexingJob]:
        """Return all jobs, optionally filtered to *slug*."""

    @abc.abstractmethod
    def append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the job event log; return the monotonic idx."""

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

    @abc.abstractmethod
    def get_job_submitted_count(self, job_id: str) -> int:
        """Return the number of pages submitted so far for *job_id*."""

    @abc.abstractmethod
    def increment_job_submitted_count(self, job_id: str) -> int:
        """Atomically increment the submitted-pages counter; return new count."""

    @abc.abstractmethod
    def save_job_submission(self, job_id: str, submission: dict[str, Any]) -> None:
        """Persist the wizard submission dict for *job_id* (token must be absent)."""

    @abc.abstractmethod
    def get_job_submission(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted submission dict, or None if not yet saved."""

    # QA

    @abc.abstractmethod
    def save_qa(self, answer: QaAnswer) -> None:
        """Persist a QA answer record."""

    @abc.abstractmethod
    def get_qa(self, answer_id: str) -> QaAnswer | None:
        """Return the QA answer, or None if absent."""

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

    # Graph + embeddings (Phase 3 — raise NotImplementedError in v1)

    def upsert_nodes(self, slug: str, nodes: Iterable[GraphNode]) -> None:
        """Upsert code-graph nodes (Phase 3)."""
        raise NotImplementedError("Graph backend lands in Phase 3")

    def upsert_edges(self, slug: str, edges: Iterable[GraphEdge]) -> None:
        """Upsert code-graph edges (Phase 3)."""
        raise NotImplementedError("Graph backend lands in Phase 3")

    def upsert_embeddings(self, slug: str, items: Iterable[Embedding]) -> None:
        """Upsert dense embedding vectors (Phase 3)."""
        raise NotImplementedError("Embeddings lands in Phase 3")

    def query_graph(
        self,
        slug: str,
        *,
        node_type: str | None = None,
        name_match: str | None = None,
        neighbors_of: str | None = None,
    ) -> list[GraphNode]:
        """Query the code graph (Phase 3)."""
        raise NotImplementedError("Graph backend lands in Phase 3")

    def list_edges(self, slug: str) -> list[GraphEdge]:
        """Return every edge for *slug* (graph-viewer endpoint)."""
        raise NotImplementedError("Graph backend lands in Phase 3")

    def vector_search(
        self, slug: str, qvec: list[float], k: int = 10
    ) -> list[Embedding]:
        """Nearest-neighbour vector search (Phase 3)."""
        raise NotImplementedError("Embeddings lands in Phase 3")

    # Scoped graph deletes (used by the incremental GraphDeltaIndexer)

    def delete_nodes_by_file(self, slug: str, file: str) -> int:
        """Delete every code node whose ``file`` equals *file*; return count."""
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

    def _rank_memory(
        self,
        slug: str,
        pool: list[MemoryEmbedding],
        qvec: list[float],
        k: int,
        filt: MemoryFilter | None,
    ) -> list[MemoryEmbedding]:
        """Facet-filter + cosine-rank a backend-loaded embedding pool (top-k).

        The single ranking core both drivers share: each loads its own pool,
        then delegates here so facet/validity intersection and cosine scoring
        can never desync across backends.
        """
        from .embedder import Embedder

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
        scored = [(emb, Embedder.cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

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
            logging.warning("Skipping malformed JSON at %s", path)
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
                logging.warning("Skipping malformed event line in %s", path)
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

    def _load_index(self, slug: str) -> dict[str, str]:
        """Load the page-id→title index; returns {} if absent."""
        idx_path = self._index_path(slug)
        if not idx_path.exists():
            return {}
        try:
            return json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_page(self, slug: str, page: WikiPage) -> None:
        """Persist *page* for the project *slug*; overwrites if same page_id."""
        pages_dir = self._pages_dir(slug)
        pages_dir.mkdir(parents=True, exist_ok=True)
        self._save_json(self._page_path(slug, page.id), page)
        index = self._load_index(slug)
        index[page.id] = page.title
        self._index_path(slug).write_text(json.dumps(index, indent=2), encoding="utf-8")

    def get_page(self, slug: str, page_id: str) -> WikiPage | None:
        """Return a single wiki page, or None if absent."""
        return self._load_json(self._page_path(slug, page_id), WikiPage)

    def list_pages(self, slug: str) -> list[WikiPage]:
        """Return all pages for project *slug*."""
        pages_dir = self._pages_dir(slug)
        if not pages_dir.exists():
            return []
        pages: list[WikiPage] = []
        for p in pages_dir.glob("*.json"):
            if p.name == "_index.json":
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

    def update_job(self, job_id: str, **fields: Any) -> IndexingJob:
        """Partially update *job_id* with *fields*; return the updated record."""
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        current = job.model_dump(by_alias=False)
        current.update(fields)
        updated = IndexingJob.model_validate(current)
        self._save_json(self._job_path(job_id), updated)
        return updated

    def list_jobs(self, slug: str | None = None) -> list[IndexingJob]:
        """Return all jobs, optionally filtered to *slug*."""
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
        return jobs

    def append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the job event log; return the monotonic idx."""
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

    def get_job_submitted_count(self, job_id: str) -> int:
        """Return the number of pages submitted so far for *job_id*."""
        meta = self._load_job_meta(job_id)
        return int(meta.get("submitted_pages", 0))

    def increment_job_submitted_count(self, job_id: str) -> int:
        """Atomically increment the submitted-pages counter; return new count."""
        with self._lock:
            meta = self._load_job_meta(job_id)
            count = int(meta.get("submitted_pages", 0)) + 1
            meta["submitted_pages"] = count
            path = self._job_meta_path(job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return count

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

    def get_qa(self, answer_id: str) -> QaAnswer | None:
        """Return the QA answer, or None if absent."""
        return self._load_json(self._qa_path(answer_id), QaAnswer)

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
                logging.warning("Skipping malformed line in %s", path)
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

    def upsert_nodes(self, slug: str, nodes: Iterable[GraphNode]) -> None:
        """Upsert graph nodes for *slug*; dedup by node_id."""
        with self._lock:
            existing = {n.node_id: n for n in self._load_jsonl(self._nodes_path(slug), GraphNode)}
            for node in nodes:
                existing[node.node_id] = node
            self._write_jsonl(self._nodes_path(slug), list(existing.values()))

    def upsert_edges(self, slug: str, edges: Iterable[GraphEdge]) -> None:
        """Upsert graph edges for *slug*; dedup by (source, target, type)."""
        with self._lock:
            existing = {
                (e.source, e.target, e.type): e
                for e in self._load_jsonl(self._edges_path(slug), GraphEdge)
            }
            for edge in edges:
                existing[(edge.source, edge.target, edge.type)] = edge
            self._write_jsonl(self._edges_path(slug), list(existing.values()))

    def upsert_embeddings(self, slug: str, items: Iterable[Embedding]) -> None:
        """Upsert embedding vectors for *slug*; dedup by node_id."""
        with self._lock:
            existing = {
                e.node_id: e
                for e in self._load_jsonl(self._embeddings_path(slug), Embedding)
            }
            for item in items:
                existing[item.node_id] = item
            self._write_jsonl(self._embeddings_path(slug), list(existing.values()))

    def query_graph(
        self,
        slug: str,
        *,
        node_type: str | None = None,
        name_match: str | None = None,
        neighbors_of: str | None = None,
    ) -> list[GraphNode]:
        """Query graph nodes for *slug* with optional filters."""
        if neighbors_of is not None:
            edges = self._load_jsonl(self._edges_path(slug), GraphEdge)
            related_ids: set[str] = set()
            for edge in edges:
                if edge.source == neighbors_of:
                    related_ids.add(edge.target)
                elif edge.target == neighbors_of:
                    related_ids.add(edge.source)
            all_nodes = self._load_jsonl(self._nodes_path(slug), GraphNode)
            return [n for n in all_nodes if n.node_id in related_ids]
        nodes = self._load_jsonl(self._nodes_path(slug), GraphNode)
        if node_type is not None:
            nodes = [n for n in nodes if n.type == node_type]
        if name_match is not None:
            lower = name_match.lower()
            nodes = [n for n in nodes if lower in n.name.lower()]
        return nodes

    def list_edges(self, slug: str) -> list[GraphEdge]:
        """Return every edge for *slug* (graph-viewer endpoint)."""
        return self._load_jsonl(self._edges_path(slug), GraphEdge)

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
        """Delete every code node whose ``file`` equals *file*; return count."""
        with self._lock:
            nodes = self._load_jsonl(self._nodes_path(slug), GraphNode)
            keep = [n for n in nodes if n.file != file]
            removed = len(nodes) - len(keep)
            if removed:
                self._write_jsonl(self._nodes_path(slug), keep)
            return removed

    def delete_edges_by_source_file(self, slug: str, file: str) -> int:
        """Delete edges whose ``source`` node belongs to *file*; return count."""
        with self._lock:
            file_ids = {
                n.node_id
                for n in self._load_jsonl(self._nodes_path(slug), GraphNode)
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

# ---------------------------------------------------------------------------
# MongoDB driver
# ---------------------------------------------------------------------------


def _strip_mongo_meta(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove MongoDB internal fields (_id) before Pydantic validation."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


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

    Phase-3 collections (graph/embeddings) are not created here — the
    methods raise ``NotImplementedError`` inherited from ``WikiStoreBase``.
    """

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

    # -- Pages ---------------------------------------------------------------

    def save_page(self, slug: str, page: WikiPage) -> None:
        """Persist *page* for the project *slug*; overwrites if same page_id."""
        doc = {"slug": slug, "page_id": page.id, **page.model_dump(by_alias=False)}
        self._col("wiki_pages").replace_one(
            {"slug": slug, "page_id": page.id}, doc, upsert=True
        )

    def get_page(self, slug: str, page_id: str) -> WikiPage | None:
        """Return a single wiki page, or None if absent."""
        doc = self._col("wiki_pages").find_one({"slug": slug, "page_id": page_id})
        if doc is None:
            return None
        clean = _strip_mongo_meta(doc)
        # Remove store-internal keys before Pydantic validation
        clean.pop("slug", None)
        clean.pop("page_id", None)
        return WikiPage.model_validate(clean)

    def list_pages(self, slug: str) -> list[WikiPage]:
        """Return all pages for project *slug*."""
        pages: list[WikiPage] = []
        for doc in self._col("wiki_pages").find({"slug": slug}):
            clean = _strip_mongo_meta(doc)
            clean.pop("slug", None)
            clean.pop("page_id", None)
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

    def update_job(self, job_id: str, **fields: Any) -> IndexingJob:
        """Partially update *job_id* with *fields*; return the updated record."""
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        current = job.model_dump(by_alias=False)
        current.update(fields)
        updated = IndexingJob.model_validate(current)
        self._col("wiki_jobs").update_one(
            {"job_id": job_id},
            {"$set": updated.model_dump(by_alias=False)},
        )
        return updated

    def list_jobs(self, slug: str | None = None) -> list[IndexingJob]:
        """Return all jobs, optionally filtered to *slug*."""
        query: dict[str, Any] = {}
        if slug is not None:
            query["slug"] = slug
        jobs: list[IndexingJob] = []
        for doc in self._col("wiki_jobs").find(query):
            jobs.append(IndexingJob.model_validate(_clean_for_model(doc, IndexingJob)))
        return jobs

    def append_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the job event log; return the monotonic idx."""
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

    def get_job_submitted_count(self, job_id: str) -> int:
        """Return the number of pages submitted so far for *job_id*."""
        doc = self._col("wiki_jobs").find_one({"job_id": job_id}, {"submitted_pages": 1})
        if doc is None:
            return 0
        return int(doc.get("submitted_pages", 0))

    def increment_job_submitted_count(self, job_id: str) -> int:
        """Atomically increment the submitted-pages counter; return new count."""
        from pymongo import ReturnDocument

        doc = self._col("wiki_jobs").find_one_and_update(
            {"job_id": job_id},
            {"$inc": {"submitted_pages": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise KeyError(f"Job not found: {job_id}")
        return int(doc.get("submitted_pages", 1))

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

    # -- QA ------------------------------------------------------------------

    def save_qa(self, answer: QaAnswer) -> None:
        """Persist a QA answer record."""
        doc = {"event_count": 0, **answer.model_dump(by_alias=False)}
        self._col("wiki_qa").replace_one(
            {"answer_id": answer.answer_id}, doc, upsert=True
        )

    def get_qa(self, answer_id: str) -> QaAnswer | None:
        """Return the QA answer, or None if absent."""
        doc = self._col("wiki_qa").find_one({"answer_id": answer_id})
        if doc is None:
            return None
        return QaAnswer.model_validate(_clean_for_model(doc, QaAnswer))

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
        self._graph_idx_done = True

    def upsert_nodes(self, slug: str, nodes: Iterable[GraphNode]) -> None:
        """Upsert graph nodes for *slug*; dedup by (slug, node_id)."""
        self._ensure_graph_indexes()
        col = self._col("wiki_graph_nodes")
        for node in nodes:
            doc = node.model_dump(by_alias=False)
            col.update_one({"slug": slug, "node_id": node.node_id}, {"$set": doc}, upsert=True)

    def upsert_edges(self, slug: str, edges: Iterable[GraphEdge]) -> None:
        """Upsert graph edges for *slug*; dedup by (slug, source, target, type)."""
        self._ensure_graph_indexes()
        col = self._col("wiki_graph_edges")
        for edge in edges:
            doc = edge.model_dump(by_alias=False)
            col.update_one(
                {"slug": slug, "source": edge.source, "target": edge.target, "type": edge.type},
                {"$set": doc},
                upsert=True,
            )

    def upsert_embeddings(self, slug: str, items: Iterable[Embedding]) -> None:
        """Upsert embedding vectors for *slug*; dedup by (slug, node_id)."""
        self._ensure_graph_indexes()
        col = self._col("wiki_embeddings")
        for item in items:
            doc = item.model_dump(by_alias=False)
            col.update_one({"slug": slug, "node_id": item.node_id}, {"$set": doc}, upsert=True)

    def query_graph(
        self,
        slug: str,
        *,
        node_type: str | None = None,
        name_match: str | None = None,
        neighbors_of: str | None = None,
    ) -> list[GraphNode]:
        """Query graph nodes for *slug* with optional filters."""
        import re

        if neighbors_of is not None:
            edge_query = {"slug": slug, "$or": [{"source": neighbors_of}, {"target": neighbors_of}]}
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
                {"slug": slug, "node_id": {"$in": list(related_ids)}}
            )
            return [GraphNode.model_validate(_strip_mongo_meta(d)) for d in cursor]
        query: dict[str, Any] = {"slug": slug}
        if node_type is not None:
            query["type"] = node_type
        if name_match is not None:
            query["name"] = {"$regex": re.escape(name_match), "$options": "i"}
        cursor = self._col("wiki_graph_nodes").find(query)
        return [GraphNode.model_validate(_strip_mongo_meta(d)) for d in cursor]

    def list_edges(self, slug: str) -> list[GraphEdge]:
        """Return every edge for *slug* (graph-viewer endpoint)."""
        cursor = self._col("wiki_graph_edges").find({"slug": slug})
        return [GraphEdge.model_validate(_strip_mongo_meta(d)) for d in cursor]

    def vector_search(self, slug: str, qvec: list[float], k: int = 10) -> list[Embedding]:
        """Return top-k embeddings for *slug* by cosine similarity (in-memory scoring)."""
        from .embedder import Embedder

        pool = [
            Embedding.model_validate(_strip_mongo_meta(d))
            for d in self._col("wiki_embeddings").find({"slug": slug})
        ]
        if not pool:
            return []
        scored = [(emb, Embedder.cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

    # -- Scoped graph deletes (incremental retract) --------------------------

    def delete_nodes_by_file(self, slug: str, file: str) -> int:
        """Delete every code node whose ``file`` equals *file*; return count."""
        result = self._col("wiki_graph_nodes").delete_many({"slug": slug, "file": file})
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
