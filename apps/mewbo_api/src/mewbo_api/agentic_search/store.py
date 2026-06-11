"""Persistence for Agentic Search — workspaces + runs (JSON or MongoDB).

The substitution boundary the original mock promised, now real. Mirrors the
project/wiki dual-backend pattern: an abstract base + a filesystem driver + a
Mongo driver + a config-driven factory. Two entity families, in storage
namespaces kept **separate from session transcripts** (the issue's hard
requirement):

* **Workspaces** — saved multi-source search configs (CRUD + bounded
  ``past_queries`` history).
* **Runs** — durable :class:`RunRecord` snapshots + an append-only,
  idx-keyed event log per run (the same shape the SSE stream replays).
* **Map jobs** — durable :class:`MapJobRecord` snapshots of an SCG indexing
  run (spec #19 §16.2) + their own append-only, idx-keyed event log. They live
  here, not in the SCG structure store, so they reuse the run event-log +
  ``RunSseGenerator`` plumbing verbatim.

JSON layout under ``<cache_dir>/agentic_search/``::

    workspaces/<id>.json
    runs/<run_id>/run.json
    runs/<run_id>/events.jsonl         (append-only, monotonic idx)
    map_jobs/<job_id>/job.json
    map_jobs/<job_id>/events.jsonl     (append-only, monotonic idx)

Mongo collections: ``agentic_search_workspaces``, ``agentic_search_runs``
(carries ``event_count`` for atomic idx), ``agentic_search_run_events``,
``agentic_search_map_jobs`` (carries ``event_count``),
``agentic_search_map_job_events``.
"""

from __future__ import annotations

import abc
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value

from . import fixtures
from .schemas import (
    TERMINAL_RUN_STATUSES,
    MapJobRecord,
    PastQuery,
    RunRecord,
    Workspace,
    WorkspaceInput,
    clean_for_model,
    utc_now_iso,
)

logging = get_logger(name="api.agentic_search.store")

# Per-workspace recent-query cap — keep history bounded (issue §7).
PAST_QUERY_CAP = 15

# Seed the demo workspaces on first run so a fresh install shows a populated
# console. Set ``MEWBO_AGENTIC_SEARCH_SEED=0`` to start empty in production.
_SEED_ENV = "MEWBO_AGENTIC_SEARCH_SEED"


def _new_workspace_id() -> str:
    """Server-generated workspace id so the console never invents one."""
    return f"ws-{uuid.uuid4().hex[:8]}"


def _new_run_id() -> str:
    """Server-generated run id."""
    return f"run-{uuid.uuid4().hex[:10]}"


def seed_workspaces() -> list[Workspace]:
    """Build the demo workspaces from fixtures as validated models."""
    out: list[Workspace] = []
    now = utc_now_iso()
    for raw in fixtures.DEMO_WORKSPACES:
        out.append(
            Workspace(
                id=raw["id"],
                name=raw["name"],
                desc=raw.get("desc", ""),
                sources=list(raw.get("sources", [])),
                instructions=raw.get("instructions", ""),
                created=raw.get("created") or "",
                created_at=now,
                updated_at=now,
                past_queries=[PastQuery(**pq) for pq in raw.get("past_queries", [])],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class AgenticSearchStoreBase(abc.ABC):
    """Abstract base for Agentic Search persistence backends."""

    # -- Workspaces ---------------------------------------------------------

    @abc.abstractmethod
    def list_workspaces(self) -> list[Workspace]:
        """Return all workspaces in stable (created_at) order."""

    def search_workspaces(self, query: str) -> list[Workspace]:
        """Filter workspaces by case-insensitive substring match on *query*.

        Matches over the name, the description, and each past-query's text.
        Concrete on the base — one load-and-filter over ``list_workspaces`` so
        both backends share the matching rule (fine at this scale). A blank
        *query* returns everything.
        """
        needle = query.strip().lower()
        if not needle:
            return self.list_workspaces()
        return [
            ws
            for ws in self.list_workspaces()
            if needle in ws.name.lower()
            or needle in ws.desc.lower()
            or any(needle in pq.q.lower() for pq in ws.past_queries)
        ]

    @abc.abstractmethod
    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Return one workspace, or None if absent."""

    @abc.abstractmethod
    def save_workspace(self, workspace: Workspace) -> None:
        """Persist *workspace* verbatim — the create + seed write primitive."""

    def create_workspace(self, data: WorkspaceInput) -> Workspace:
        """Build a workspace with a server-generated id and persist it."""
        now = utc_now_iso()
        ws = Workspace(
            id=_new_workspace_id(),
            name=data.name,
            desc=data.desc,
            sources=list(data.sources),
            instructions=data.instructions,
            created_at=now,
            updated_at=now,
            past_queries=[],
        )
        self.save_workspace(ws)
        return ws

    @abc.abstractmethod
    def update_workspace(
        self, workspace_id: str, fields: dict[str, Any]
    ) -> Workspace | None:
        """Apply a partial update (name/desc/sources/instructions); return new state."""

    @abc.abstractmethod
    def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace; return True if it existed."""

    @abc.abstractmethod
    def append_past_query(self, workspace_id: str, entry: PastQuery) -> None:
        """Prepend *entry* to the workspace history, capped at ``PAST_QUERY_CAP``."""

    # -- Virtual MCP config (per workspace, secrets behind the encode seam) --

    @abc.abstractmethod
    def save_workspace_mcp_config(
        self, workspace_id: str, blob: dict[str, Any]
    ) -> None:
        """Persist the encoded virtual-MCP-config *blob* for *workspace_id*.

        The blob is opaque here (the :class:`WorkspaceMcpConfig` encode seam owns
        its shape); the store only persists/returns it. Secret-bearing — kept in
        its own isolated surface (mode-0600 JSON file / dedicated Mongo
        collection), the :class:`CredentialStore` stance.
        """

    @abc.abstractmethod
    def get_workspace_mcp_config(self, workspace_id: str) -> dict[str, Any] | None:
        """Return the encoded virtual-MCP-config blob for *workspace_id*, or None."""

    @abc.abstractmethod
    def delete_workspace_mcp_config(self, workspace_id: str) -> bool:
        """Delete *workspace_id*'s virtual MCP config; True if one existed."""

    @abc.abstractmethod
    def update_past_query(
        self, workspace_id: str, run_id: str, *, status: str, results: int
    ) -> None:
        """Patch the history entry for *run_id* in place (status/result count)."""

    # -- Runs ---------------------------------------------------------------

    @abc.abstractmethod
    def create_run(self, run: RunRecord) -> None:
        """Persist a new run record."""

    @abc.abstractmethod
    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record, or None if absent."""

    @abc.abstractmethod
    def update_run(self, run_id: str, **fields: Any) -> RunRecord:
        """Partially update *run_id*; return the updated record."""

    @abc.abstractmethod
    def list_runs(self, workspace_id: str | None = None) -> list[RunRecord]:
        """Return runs, optionally filtered to *workspace_id*."""

    def append_run_event(self, run_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the run event log; return the monotonic idx.

        Concrete on the base so BOTH backends share the one idempotency guard
        (issue #82): a ``result`` event whose id is already present in the run's
        event log is a no-op — the existing idx is returned, nothing is written.
        This is the single honest seam where result de-duplication lives. The
        run event log IS the normalized search-event stream the SSE transport
        replays and the console reducer merges, so a re-drive, an SSE
        replay+tail boundary, or a settle-time reconciliation can never land the
        same result twice (the "duplicate result cards / linked hover" symptom).
        Every other event type passes straight through to the raw primitive.
        """
        if event.get("type") == "result":
            result_id = (event.get("result") or {}).get("id")
            if result_id is not None:
                existing = self._existing_result_idx(run_id, result_id)
                if existing is not None:
                    return existing
        return self._append_run_event_raw(run_id, event)

    def _existing_result_idx(self, run_id: str, result_id: str) -> int | None:
        """Return the idx of an already-logged ``result`` with *result_id*, else None.

        Reads the run's event log (the same source the SSE stream replays) and
        scans for a ``result`` event carrying *result_id*. Concrete on the base
        so the dedup rule never drifts between backends; the per-event-type read
        keeps the scan cheap (result counts are small — a handful per run).
        """
        for ev in self.load_run_events(run_id):
            if ev.get("type") == "result" and (ev.get("result") or {}).get("id") == result_id:
                idx = ev.get("idx")
                return int(idx) if idx is not None else None
        return None

    @abc.abstractmethod
    def _append_run_event_raw(self, run_id: str, event: dict[str, Any]) -> int:
        """Append *event* unconditionally; return the monotonic idx.

        The per-backend write primitive. Callers use :meth:`append_run_event`
        (which carries the result-dedup guard); this raw form is the override
        point only.
        """

    @abc.abstractmethod
    def load_run_events(
        self, run_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return run events with idx > *after_idx* (-1 returns all)."""

    def cancel_run(self, run_id: str) -> bool:
        """Cancel *run_id*; return True on first cancel, False if already terminal.

        Concrete on the base — it depends only on the abstract ``get_run`` /
        ``update_run`` / ``append_run_event`` operations, so both backends share
        one cancel path (no drift on the terminal-status check).
        """
        run = self.get_run(run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return False
        self.update_run(run_id, status="cancelled", completed_at=utc_now_iso())
        self.append_run_event(run_id, {"type": "cancelled"})
        return True

    # -- Map jobs (SCG indexing) -------------------------------------------

    @abc.abstractmethod
    def create_map_job(self, job: MapJobRecord) -> None:
        """Persist a new map-source (SCG indexing) job record."""

    @abc.abstractmethod
    def get_map_job(self, job_id: str) -> MapJobRecord | None:
        """Return the map-job record, or None if absent."""

    @abc.abstractmethod
    def update_map_job(self, job_id: str, **fields: Any) -> MapJobRecord:
        """Partially update *job_id*; return the updated record."""

    @abc.abstractmethod
    def list_map_jobs(self, source_id: str | None = None) -> list[MapJobRecord]:
        """Return map jobs (newest-first), optionally filtered to *source_id*."""

    @abc.abstractmethod
    def append_map_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the map-job event log; return the monotonic idx."""

    @abc.abstractmethod
    def load_map_job_events(
        self, job_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return map-job events with idx > *after_idx* (-1 returns all)."""


# ---------------------------------------------------------------------------
# JSON / filesystem driver
# ---------------------------------------------------------------------------


class JsonAgenticSearchStore(AgenticSearchStoreBase):
    """Filesystem-backed store under ``<cache_dir>/agentic_search/``."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        """Initialise + create the directory tree."""
        if root_dir is None:
            home = get_config_value("runtime", "cache_dir", default="") or ".mewbo"
            root_dir = Path(home) / "agentic_search"
        self.root_dir = Path(root_dir)
        (self.root_dir / "workspaces").mkdir(parents=True, exist_ok=True)
        (self.root_dir / "runs").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    def _ws_path(self, workspace_id: str) -> Path:
        return self.root_dir / "workspaces" / f"{workspace_id}.json"

    def _run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def _run_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _events_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events.jsonl"

    def _map_job_dir(self, job_id: str) -> Path:
        return self.root_dir / "map_jobs" / job_id

    def _map_job_path(self, job_id: str) -> Path:
        return self._map_job_dir(job_id) / "job.json"

    def _map_job_events_path(self, job_id: str) -> Path:
        return self._map_job_dir(job_id) / "events.jsonl"

    def _mcp_config_dir(self) -> Path:
        """Directory holding per-workspace virtual MCP config (mode 0700)."""
        d = self.root_dir / "mcp_configs"
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:  # pragma: no cover — best-effort on exotic filesystems
            pass
        return d

    def _mcp_config_path(self, workspace_id: str) -> Path:
        return self._mcp_config_dir() / f"{workspace_id}.json"

    def _save_ws(self, ws: Workspace) -> None:
        self._ws_path(ws.id).write_text(
            ws.model_dump_json(indent=2), encoding="utf-8"
        )

    def _load_ws(self, path: Path) -> Workspace | None:
        if not path.exists():
            return None
        try:
            return Workspace.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("Skipping malformed workspace at %s", path)
            return None

    # -- Workspaces ---------------------------------------------------------

    def list_workspaces(self) -> list[Workspace]:
        """Return all workspaces, oldest-first by created_at."""
        with self._lock:
            out: list[Workspace] = []
            for p in (self.root_dir / "workspaces").glob("*.json"):
                ws = self._load_ws(p)
                if ws is not None:
                    out.append(ws)
        return sorted(out, key=lambda w: w.created_at)

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Return one workspace, or None if absent."""
        with self._lock:
            return self._load_ws(self._ws_path(workspace_id))

    def save_workspace(self, workspace: Workspace) -> None:
        """Persist *workspace* verbatim."""
        with self._lock:
            self._save_ws(workspace)

    def update_workspace(
        self, workspace_id: str, fields: dict[str, Any]
    ) -> Workspace | None:
        """Apply a partial update; bump updated_at; return the new state."""
        with self._lock:
            ws = self._load_ws(self._ws_path(workspace_id))
            if ws is None:
                return None
            updates: dict[str, Any] = {}
            for key in ("name", "desc", "sources", "instructions"):
                if fields.get(key) is not None:
                    updates[key] = fields[key]
            updates["updated_at"] = utc_now_iso()
            new_ws = ws.model_copy(update=updates)
            self._save_ws(new_ws)
            return new_ws

    def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace; return True if it existed."""
        with self._lock:
            path = self._ws_path(workspace_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    def append_past_query(self, workspace_id: str, entry: PastQuery) -> None:
        """Prepend *entry* to the history, capped at PAST_QUERY_CAP."""
        with self._lock:
            ws = self._load_ws(self._ws_path(workspace_id))
            if ws is None:
                return
            history = [entry, *ws.past_queries][:PAST_QUERY_CAP]
            self._save_ws(ws.model_copy(update={"past_queries": history}))

    def update_past_query(
        self, workspace_id: str, run_id: str, *, status: str, results: int
    ) -> None:
        """Patch the history entry for *run_id* in place."""
        with self._lock:
            ws = self._load_ws(self._ws_path(workspace_id))
            if ws is None:
                return
            history = [
                pq.model_copy(update={"status": status, "results": results})
                if pq.run_id == run_id
                else pq
                for pq in ws.past_queries
            ]
            self._save_ws(ws.model_copy(update={"past_queries": history}))

    # -- Virtual MCP config -------------------------------------------------

    def save_workspace_mcp_config(
        self, workspace_id: str, blob: dict[str, Any]
    ) -> None:
        """Persist the encoded virtual MCP config *blob* at mode 0600."""
        path = self._mcp_config_path(workspace_id)
        path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass

    def get_workspace_mcp_config(self, workspace_id: str) -> dict[str, Any] | None:
        """Return the encoded virtual MCP config blob for *workspace_id*, or None."""
        path = self._mcp_config_path(workspace_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            logging.warning("Skipping malformed workspace MCP config at %s", path)
            return None

    def delete_workspace_mcp_config(self, workspace_id: str) -> bool:
        """Delete *workspace_id*'s virtual MCP config; True if one existed."""
        path = self._mcp_config_path(workspace_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- Runs ---------------------------------------------------------------

    def create_run(self, run: RunRecord) -> None:
        """Persist a new run record."""
        self._run_dir(run.run_id).mkdir(parents=True, exist_ok=True)
        self._run_path(run.run_id).write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record, or None if absent."""
        path = self._run_path(run_id)
        if not path.exists():
            return None
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("Skipping malformed run at %s", path)
            return None

    def update_run(self, run_id: str, **fields: Any) -> RunRecord:
        """Partially update *run_id*; return the updated record."""
        with self._lock:
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(f"Run not found: {run_id}")
            new_run = run.model_copy(update=fields)
            self._run_path(run_id).write_text(
                new_run.model_dump_json(indent=2), encoding="utf-8"
            )
            return new_run

    def list_runs(self, workspace_id: str | None = None) -> list[RunRecord]:
        """Return runs (newest-first), optionally filtered to *workspace_id*."""
        runs_root = self.root_dir / "runs"
        if not runs_root.exists():
            return []
        out: list[RunRecord] = []
        for run_dir in runs_root.iterdir():
            if not run_dir.is_dir():
                continue
            run = self.get_run(run_dir.name)
            if run is None:
                continue
            if workspace_id is None or run.workspace_id == workspace_id:
                out.append(run)
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    # -- Append-only JSONL event-log primitive (shared by runs + map jobs) --

    def _append_jsonl_event(self, path: Path, event: dict[str, Any]) -> int:
        """Append *event* to a JSONL log at *path*; return the monotonic idx.

        The idx is the count of existing non-blank lines, kept monotonic under
        ``self._lock``. The single write primitive both the run event log and
        the map-job event log ride (DRY).
        """
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            idx = 0
            if path.exists():
                idx = sum(
                    1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
                )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({**event, "idx": idx}) + "\n")
            return idx

    @staticmethod
    def _load_jsonl_events(path: Path, after_idx: int) -> list[dict[str, Any]]:
        """Return JSONL events at *path* with idx > *after_idx* (-1 = all)."""
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("idx", -1) > after_idx:
                out.append(rec)
        return out

    def _append_run_event_raw(self, run_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the run event log; return the monotonic idx."""
        return self._append_jsonl_event(self._events_path(run_id), event)

    def load_run_events(
        self, run_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return run events with idx > *after_idx* (-1 returns all)."""
        return self._load_jsonl_events(self._events_path(run_id), after_idx)

    # -- Map jobs (SCG indexing) -------------------------------------------

    def create_map_job(self, job: MapJobRecord) -> None:
        """Persist a new map-job record."""
        self._map_job_dir(job.job_id).mkdir(parents=True, exist_ok=True)
        self._map_job_path(job.job_id).write_text(
            job.model_dump_json(indent=2), encoding="utf-8"
        )

    def get_map_job(self, job_id: str) -> MapJobRecord | None:
        """Return the map-job record, or None if absent."""
        path = self._map_job_path(job_id)
        if not path.exists():
            return None
        try:
            return MapJobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("Skipping malformed map job at %s", path)
            return None

    def update_map_job(self, job_id: str, **fields: Any) -> MapJobRecord:
        """Partially update *job_id*; return the updated record."""
        with self._lock:
            job = self.get_map_job(job_id)
            if job is None:
                raise KeyError(f"Map job not found: {job_id}")
            new_job = job.model_copy(update=fields)
            self._map_job_path(job_id).write_text(
                new_job.model_dump_json(indent=2), encoding="utf-8"
            )
            return new_job

    def list_map_jobs(self, source_id: str | None = None) -> list[MapJobRecord]:
        """Return map jobs (newest-first), optionally filtered to *source_id*."""
        jobs_root = self.root_dir / "map_jobs"
        if not jobs_root.exists():
            return []
        out: list[MapJobRecord] = []
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            job = self.get_map_job(job_dir.name)
            if job is None:
                continue
            if source_id is None or job.source_id == source_id:
                out.append(job)
        return sorted(out, key=lambda j: j.created_at, reverse=True)

    def append_map_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the map-job event log; return the monotonic idx."""
        return self._append_jsonl_event(self._map_job_events_path(job_id), event)

    def load_map_job_events(
        self, job_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return map-job events with idx > *after_idx* (-1 returns all)."""
        return self._load_jsonl_events(self._map_job_events_path(job_id), after_idx)


# ---------------------------------------------------------------------------
# MongoDB driver
# ---------------------------------------------------------------------------


class MongoAgenticSearchStore(AgenticSearchStoreBase):
    """MongoDB-backed store.

    Collections: ``agentic_search_workspaces`` (id PK),
    ``agentic_search_runs`` (run_id PK; carries ``event_count`` for atomic idx),
    ``agentic_search_run_events`` ((run_id, idx) compound; append-only).
    """

    WS = "agentic_search_workspaces"
    RUNS = "agentic_search_runs"
    EVENTS = "agentic_search_run_events"
    MAP_JOBS = "agentic_search_map_jobs"
    MAP_JOB_EVENTS = "agentic_search_map_job_events"
    MCP_CONFIGS = "agentic_search_workspace_mcp_configs"

    def __init__(
        self,
        *,
        client: Any = None,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Connect + ensure indexes."""
        if client is None:
            from pymongo import MongoClient

            _uri = uri or get_config_value(
                "storage", "mongodb", "uri", default="mongodb://localhost:27017"
            )
            client = MongoClient(_uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="mewbo")
        self._client = client
        self._db = client[database]
        self._ensure_indexes()

    def _col(self, name: str) -> Any:
        return self._db[name]

    def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING

        self._col(self.WS).create_index(
            [("id", ASCENDING)], name="ix_ws_id", unique=True, background=True
        )
        self._col(self.RUNS).create_index(
            [("run_id", ASCENDING)], name="ix_runs_run_id", unique=True, background=True
        )
        self._col(self.RUNS).create_index(
            [("workspace_id", ASCENDING)], name="ix_runs_ws", background=True
        )
        self._col(self.EVENTS).create_index(
            [("run_id", ASCENDING), ("idx", ASCENDING)],
            name="ix_run_events_run_idx",
            unique=True,
            background=True,
        )
        self._col(self.MAP_JOBS).create_index(
            [("job_id", ASCENDING)],
            name="ix_map_jobs_job_id",
            unique=True,
            background=True,
        )
        self._col(self.MAP_JOBS).create_index(
            [("source_id", ASCENDING)], name="ix_map_jobs_source", background=True
        )
        self._col(self.MAP_JOB_EVENTS).create_index(
            [("job_id", ASCENDING), ("idx", ASCENDING)],
            name="ix_map_job_events_job_idx",
            unique=True,
            background=True,
        )
        self._col(self.MCP_CONFIGS).create_index(
            [("workspace_id", ASCENDING)],
            name="ix_ws_mcp_config_ws",
            unique=True,
            background=True,
        )

    def _atomic_next_idx(
        self, collection: str, key_field: str, key_value: str
    ) -> int:
        """Atomically ``$inc`` ``event_count`` on a parent doc; return prior idx.

        Shared by the run event log (``RUNS``/``run_id``) and the map-job event
        log (``MAP_JOBS``/``job_id``) so idx stays monotonic per parent (DRY).
        """
        from pymongo import ReturnDocument

        doc = self._col(collection).find_one_and_update(
            {key_field: key_value},
            {"$inc": {"event_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise KeyError(f"{key_field} not found: {key_value}")
        return int(doc["event_count"]) - 1

    # -- Workspaces ---------------------------------------------------------

    def list_workspaces(self) -> list[Workspace]:
        """Return all workspaces, oldest-first by created_at."""
        cursor = self._col(self.WS).find({}, {"_id": 0}).sort("created_at", 1)
        return [Workspace.model_validate(clean_for_model(d, Workspace)) for d in cursor]

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Return one workspace, or None if absent."""
        d = self._col(self.WS).find_one({"id": workspace_id}, {"_id": 0})
        return Workspace.model_validate(clean_for_model(d, Workspace)) if d else None

    def save_workspace(self, workspace: Workspace) -> None:
        """Persist *workspace* verbatim (upsert by id)."""
        self._col(self.WS).replace_one(
            {"id": workspace.id}, workspace.model_dump(), upsert=True
        )

    def update_workspace(
        self, workspace_id: str, fields: dict[str, Any]
    ) -> Workspace | None:
        """Apply a partial update; bump updated_at; return the new state."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            return None
        updates: dict[str, Any] = {}
        for key in ("name", "desc", "sources", "instructions"):
            if fields.get(key) is not None:
                updates[key] = fields[key]
        updates["updated_at"] = utc_now_iso()
        new_ws = ws.model_copy(update=updates)
        self._col(self.WS).replace_one({"id": workspace_id}, new_ws.model_dump())
        return new_ws

    def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace; return True if it existed."""
        return self._col(self.WS).delete_one({"id": workspace_id}).deleted_count > 0

    def append_past_query(self, workspace_id: str, entry: PastQuery) -> None:
        """Prepend *entry* to the history, capped at PAST_QUERY_CAP."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            return
        history = [entry, *ws.past_queries][:PAST_QUERY_CAP]
        self._col(self.WS).update_one(
            {"id": workspace_id},
            {"$set": {"past_queries": [pq.model_dump() for pq in history]}},
        )

    def update_past_query(
        self, workspace_id: str, run_id: str, *, status: str, results: int
    ) -> None:
        """Patch the history entry for *run_id* in place."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            return
        history = [
            pq.model_copy(update={"status": status, "results": results})
            if pq.run_id == run_id
            else pq
            for pq in ws.past_queries
        ]
        self._col(self.WS).update_one(
            {"id": workspace_id},
            {"$set": {"past_queries": [pq.model_dump() for pq in history]}},
        )

    # -- Virtual MCP config -------------------------------------------------

    def save_workspace_mcp_config(
        self, workspace_id: str, blob: dict[str, Any]
    ) -> None:
        """Persist the encoded virtual MCP config *blob* (upsert by workspace_id)."""
        self._col(self.MCP_CONFIGS).replace_one(
            {"workspace_id": workspace_id},
            {"workspace_id": workspace_id, "blob": blob},
            upsert=True,
        )

    def get_workspace_mcp_config(self, workspace_id: str) -> dict[str, Any] | None:
        """Return the encoded virtual MCP config blob for *workspace_id*, or None."""
        doc = self._col(self.MCP_CONFIGS).find_one(
            {"workspace_id": workspace_id}, {"_id": 0, "blob": 1}
        )
        blob = doc.get("blob") if doc else None
        return blob if isinstance(blob, dict) else None

    def delete_workspace_mcp_config(self, workspace_id: str) -> bool:
        """Delete *workspace_id*'s virtual MCP config; True if one existed."""
        return (
            self._col(self.MCP_CONFIGS)
            .delete_one({"workspace_id": workspace_id})
            .deleted_count
            > 0
        )

    # -- Runs ---------------------------------------------------------------

    def create_run(self, run: RunRecord) -> None:
        """Persist a new run record (with the atomic event_count counter)."""
        doc = {"event_count": 0, **run.model_dump()}
        self._col(self.RUNS).replace_one({"run_id": run.run_id}, doc, upsert=True)

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record, or None if absent."""
        d = self._col(self.RUNS).find_one({"run_id": run_id}, {"_id": 0})
        return RunRecord.model_validate(clean_for_model(d, RunRecord)) if d else None

    def update_run(self, run_id: str, **fields: Any) -> RunRecord:
        """Partially update *run_id*; return the updated record."""
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Run not found: {run_id}")
        new_run = run.model_copy(update=fields)
        self._col(self.RUNS).update_one(
            {"run_id": run_id}, {"$set": new_run.model_dump()}
        )
        return new_run

    def list_runs(self, workspace_id: str | None = None) -> list[RunRecord]:
        """Return runs (newest-first), optionally filtered to *workspace_id*."""
        query: dict[str, Any] = {}
        if workspace_id is not None:
            query["workspace_id"] = workspace_id
        cursor = self._col(self.RUNS).find(query, {"_id": 0}).sort("created_at", -1)
        return [RunRecord.model_validate(clean_for_model(d, RunRecord)) for d in cursor]

    def _append_run_event_raw(self, run_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the run event log; return the monotonic idx."""
        idx = self._atomic_next_idx(self.RUNS, "run_id", run_id)
        self._col(self.EVENTS).insert_one({"run_id": run_id, "idx": idx, **event})
        return idx

    def load_run_events(
        self, run_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return run events with idx > *after_idx* (-1 returns all)."""
        cursor = (
            self._col(self.EVENTS)
            .find({"run_id": run_id, "idx": {"$gt": after_idx}}, {"_id": 0})
            .sort("idx", 1)
        )
        out: list[dict[str, Any]] = []
        for doc in cursor:
            doc.pop("run_id", None)
            out.append(doc)
        return out

    # -- Map jobs (SCG indexing) -------------------------------------------

    def create_map_job(self, job: MapJobRecord) -> None:
        """Persist a new map-job record (with the atomic event_count counter)."""
        doc = {"event_count": 0, **job.model_dump()}
        self._col(self.MAP_JOBS).replace_one({"job_id": job.job_id}, doc, upsert=True)

    def get_map_job(self, job_id: str) -> MapJobRecord | None:
        """Return the map-job record, or None if absent."""
        d = self._col(self.MAP_JOBS).find_one({"job_id": job_id}, {"_id": 0})
        return MapJobRecord.model_validate(clean_for_model(d, MapJobRecord)) if d else None

    def update_map_job(self, job_id: str, **fields: Any) -> MapJobRecord:
        """Partially update *job_id*; return the updated record."""
        job = self.get_map_job(job_id)
        if job is None:
            raise KeyError(f"Map job not found: {job_id}")
        new_job = job.model_copy(update=fields)
        self._col(self.MAP_JOBS).update_one(
            {"job_id": job_id}, {"$set": new_job.model_dump()}
        )
        return new_job

    def list_map_jobs(self, source_id: str | None = None) -> list[MapJobRecord]:
        """Return map jobs (newest-first), optionally filtered to *source_id*."""
        query: dict[str, Any] = {}
        if source_id is not None:
            query["source_id"] = source_id
        cursor = self._col(self.MAP_JOBS).find(query, {"_id": 0}).sort("created_at", -1)
        return [
            MapJobRecord.model_validate(clean_for_model(d, MapJobRecord)) for d in cursor
        ]

    def append_map_job_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append *event* to the map-job event log; return the monotonic idx."""
        idx = self._atomic_next_idx(self.MAP_JOBS, "job_id", job_id)
        self._col(self.MAP_JOB_EVENTS).insert_one(
            {"job_id": job_id, "idx": idx, **event}
        )
        return idx

    def load_map_job_events(
        self, job_id: str, after_idx: int = -1
    ) -> list[dict[str, Any]]:
        """Return map-job events with idx > *after_idx* (-1 returns all)."""
        cursor = (
            self._col(self.MAP_JOB_EVENTS)
            .find({"job_id": job_id, "idx": {"$gt": after_idx}}, {"_id": 0})
            .sort("idx", 1)
        )
        out: list[dict[str, Any]] = []
        for doc in cursor:
            doc.pop("job_id", None)
            out.append(doc)
        return out


# ---------------------------------------------------------------------------
# Factory + module singleton
# ---------------------------------------------------------------------------


def create_agentic_search_store() -> AgenticSearchStoreBase:
    """Return the configured store driver (``storage.driver``; default JSON)."""
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        return MongoAgenticSearchStore()
    return JsonAgenticSearchStore()


def seeding_enabled() -> bool:
    """True unless demo seeding is explicitly disabled (``MEWBO_AGENTIC_SEARCH_SEED=0``).

    The one gate for *all* demo data — the demo workspaces here and the
    fixtures-backed source-tool fallback in ``catalog.py`` — so a production
    install (seeding off) shows only what's really configured.
    """
    return os.environ.get(_SEED_ENV, "1") != "0"


def seed_workspaces_if_empty(store: AgenticSearchStoreBase) -> None:
    """Seed demo workspaces when the store is empty + seeding is enabled."""
    if not seeding_enabled():
        return
    if store.list_workspaces():
        return
    for ws in seed_workspaces():
        store.save_workspace(ws)  # verbatim — keeps seed ids stable


_store_singleton: AgenticSearchStoreBase | None = None
_singleton_lock = threading.Lock()


def get_store() -> AgenticSearchStoreBase:
    """Return the process-wide store, creating + seeding it on first use."""
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is None:
            _store_singleton = create_agentic_search_store()
            seed_workspaces_if_empty(_store_singleton)
        return _store_singleton


def set_store(store: AgenticSearchStoreBase | None) -> None:
    """Override the process-wide store (used by tests)."""
    global _store_singleton
    with _singleton_lock:
        _store_singleton = store


def reset_for_tests() -> None:
    """Swap in a fresh, seeded JSON store under a throwaway temp dir.

    Keeps unit tests isolated from real data while still exercising the JSON
    backend end-to-end through the routes.
    """
    tmp = Path(tempfile.mkdtemp(prefix="mewbo-agentic-search-"))
    store = JsonAgenticSearchStore(root_dir=tmp)
    for ws in seed_workspaces():
        store.save_workspace(ws)
    set_store(store)


__all__ = [
    "PAST_QUERY_CAP",
    "AgenticSearchStoreBase",
    "JsonAgenticSearchStore",
    "MongoAgenticSearchStore",
    "create_agentic_search_store",
    "seed_workspaces",
    "seed_workspaces_if_empty",
    "seeding_enabled",
    "get_store",
    "set_store",
    "reset_for_tests",
]
