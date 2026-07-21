"""Persistence for Mewbo Apps — manifests, versions, run ledger, and data plane.

Three storage families, each the same shape the rest of the codebase uses
(``agentic_search/store.py``, ``triggers/store*.py``): an abstract base + a
filesystem driver + a Mongo driver + a config-driven factory (``storage.driver``,
default ``json`` — read from CONFIG, never a ``MEWBO_*`` env, per spec §8):

* :class:`AppStoreBase` — the durable :class:`AppSpec` manifest keyed by
  ``app_id`` plus its append-only :class:`AppVersion` history (rollback repoints;
  history is never rewritten).
* :class:`PipelineRunStoreBase` — the :class:`PipelineRun` provenance ledger
  (opened at trigger fire, closed at maintainer run end); it is the freshness
  signal + the ``system`` namespace backing + the repair-loop input (spec §2.8).
* :class:`AppDataStoreBase` — the app-ID-keyed data plane, ONE logical
  collection compound-keyed ``(app_id, collection, key)`` (spec §2.5). Ingress
  is agent-side only (the ``app_data`` SessionTool, workstream B); egress is the
  read-only REST surface. ``upsert`` optionally validates a document against the
  owning :class:`CollectionSpec`'s JSON Schema before writing.

JSON layout under ``<cache_dir>/apps/``::

    manifests/<app_id>.json
    versions/<app_id>/<version>.json
    runs/<app_id>/<run_key>.json
    data/<app_id>/<collection>.json        ({key: AppDataDoc} map)

Mongo collections: ``apps`` (app_id PK), ``app_versions`` ((app_id, version)),
``app_pipeline_runs`` (run_key PK), ``app_data`` ((app_id, collection, key)
compound-unique).
"""

from __future__ import annotations

import abc
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value

from .models import AppDataDoc, AppSpec, AppVersion, CollectionSpec, PipelineRun

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

logging = get_logger(name="api.apps.store")


class CollectionCapExceeded(Exception):
    """A NEW-key insert would push a collection past ``max_docs_per_collection``.

    Raised by :meth:`AppDataStoreBase.upsert` only when a ``max_docs`` cap is
    supplied AND the key is new (spec §2.11 ``max_docs_per_collection``). An
    update to an EXISTING key never trips it. The ``app_data`` SessionTool maps it
    to a clean agent-visible error the agent self-corrects on.
    """

    def __init__(self, collection: str, cap: int) -> None:
        """Record which collection hit which cap, for the agent-facing message."""
        self.collection = collection
        self.cap = cap
        super().__init__(
            f"collection {collection!r} is at its {cap}-document cap; "
            "delete a document or raise max_docs_per_collection to add a new one"
        )


def new_app_id() -> str:
    """Server-generated app id — the console never invents one (agentic-search idiom)."""
    return f"app-{uuid.uuid4().hex[:12]}"


def new_run_key() -> str:
    """Server-generated pipeline-run key."""
    return f"run-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# App manifest + version history
# ---------------------------------------------------------------------------


class AppStoreBase(abc.ABC):
    """Abstract base for app-manifest + version-history persistence."""

    @abc.abstractmethod
    def get(self, app_id: str) -> AppSpec | None:
        """Return the current manifest for *app_id*, or ``None`` if absent."""

    @abc.abstractmethod
    def save(self, spec: AppSpec) -> None:
        """Persist *spec* verbatim (upsert by ``app_id``) — the one write primitive."""

    @abc.abstractmethod
    def list_apps(self, *, include_archived: bool = False) -> list[AppSpec]:
        """Return all manifests, newest-first by ``created_at``.

        ``archived`` apps are hidden from the gallery by default; pass
        ``include_archived=True`` for an admin/audit view.
        """

    @abc.abstractmethod
    def delete(self, app_id: str) -> bool:
        """Hard-delete an app + its version history; ``True`` if it existed.

        Product flows ARCHIVE (a status), not delete — this exists for
        test cleanup and operator purge, mirroring ``delete_workspace``.
        """

    @abc.abstractmethod
    def save_version(self, version: AppVersion) -> None:
        """Append an immutable :class:`AppVersion` snapshot to the history."""

    @abc.abstractmethod
    def list_versions(self, app_id: str) -> list[AppVersion]:
        """Return the app's version snapshots, oldest-first by ``version``."""

    @abc.abstractmethod
    def get_version(self, app_id: str, version: int) -> AppVersion | None:
        """Return one snapshot by version number, or ``None``."""

    def latest_version(self, app_id: str) -> int:
        """Highest recorded version number for *app_id* (0 if none).

        Concrete on the base — it depends only on :meth:`list_versions`, so both
        backends share the rollback/edit "next version" arithmetic (DRY).
        """
        versions = self.list_versions(app_id)
        return max((v.version for v in versions), default=0)


class JsonAppStore(AppStoreBase):
    """Filesystem-backed app store under ``<cache_dir>/apps/``."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        """Initialise + create the manifest/version directory tree."""
        if root_dir is None:
            home = get_config_value("runtime", "cache_dir", default="") or ".mewbo"
            root_dir = Path(home) / "apps"
        self.root_dir = Path(root_dir)
        (self.root_dir / "manifests").mkdir(parents=True, exist_ok=True)
        (self.root_dir / "versions").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _manifest_path(self, app_id: str) -> Path:
        return self.root_dir / "manifests" / f"{app_id}.json"

    def _versions_dir(self, app_id: str) -> Path:
        return self.root_dir / "versions" / app_id

    def get(self, app_id: str) -> AppSpec | None:
        """Return the manifest for *app_id*, or ``None``."""
        with self._lock:
            path = self._manifest_path(app_id)
            if not path.exists():
                return None
            try:
                return AppSpec.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                logging.warning("Skipping malformed app manifest at {}", path)
                return None

    def save(self, spec: AppSpec) -> None:
        """Persist *spec* verbatim (overwrite by ``app_id``)."""
        with self._lock:
            self._manifest_path(spec.app_id).write_text(
                spec.model_dump_json(indent=2), encoding="utf-8"
            )

    def list_apps(self, *, include_archived: bool = False) -> list[AppSpec]:
        """Return all manifests newest-first (archived hidden by default)."""
        with self._lock:
            out: list[AppSpec] = []
            for p in (self.root_dir / "manifests").glob("*.json"):
                try:
                    spec = AppSpec.model_validate_json(p.read_text(encoding="utf-8"))
                except Exception:
                    logging.warning("Skipping malformed app manifest at {}", p)
                    continue
                if include_archived or spec.status != "archived":
                    out.append(spec)
        return sorted(out, key=lambda s: s.created_at, reverse=True)

    def delete(self, app_id: str) -> bool:
        """Hard-delete the app + its version history; ``True`` if it existed."""
        with self._lock:
            path = self._manifest_path(app_id)
            existed = path.exists()
            if existed:
                path.unlink()
            vdir = self._versions_dir(app_id)
            if vdir.exists():
                for vp in vdir.glob("*.json"):
                    vp.unlink()
                vdir.rmdir()
            return existed

    def save_version(self, version: AppVersion) -> None:
        """Append an immutable version snapshot."""
        with self._lock:
            vdir = self._versions_dir(version.app_id)
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / f"{version.version}.json").write_text(
                version.model_dump_json(indent=2), encoding="utf-8"
            )

    def list_versions(self, app_id: str) -> list[AppVersion]:
        """Return the app's snapshots, oldest-first by version."""
        with self._lock:
            vdir = self._versions_dir(app_id)
            if not vdir.exists():
                return []
            out: list[AppVersion] = []
            for p in vdir.glob("*.json"):
                try:
                    out.append(AppVersion.model_validate_json(p.read_text(encoding="utf-8")))
                except Exception:
                    logging.warning("Skipping malformed app version at {}", p)
        return sorted(out, key=lambda v: v.version)

    def get_version(self, app_id: str, version: int) -> AppVersion | None:
        """Return one snapshot by version number, or ``None``."""
        with self._lock:
            path = self._versions_dir(app_id) / f"{version}.json"
            if not path.exists():
                return None
            try:
                return AppVersion.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                logging.warning("Skipping malformed app version at {}", path)
                return None


class MongoAppStore(AppStoreBase):
    """MongoDB-backed app store (collections ``apps`` + ``app_versions``)."""

    APPS = "apps"
    VERSIONS = "app_versions"

    def __init__(
        self, *, client: Any = None, uri: str | None = None, database: str | None = None
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

        self._col(self.APPS).create_index(
            [("app_id", ASCENDING)], name="ix_apps_app_id", unique=True, background=True
        )
        self._col(self.VERSIONS).create_index(
            [("app_id", ASCENDING), ("version", ASCENDING)],
            name="ix_app_versions_app_version",
            unique=True,
            background=True,
        )

    def get(self, app_id: str) -> AppSpec | None:
        """Return the manifest for *app_id*, or ``None``."""
        d = self._col(self.APPS).find_one({"app_id": app_id}, {"_id": 0})
        return AppSpec.model_validate(d) if d else None

    def save(self, spec: AppSpec) -> None:
        """Persist *spec* (upsert by ``app_id``)."""
        self._col(self.APPS).replace_one(
            {"app_id": spec.app_id}, spec.model_dump(mode="json"), upsert=True
        )

    def list_apps(self, *, include_archived: bool = False) -> list[AppSpec]:
        """Return all manifests newest-first (archived hidden by default)."""
        query: dict[str, Any] = {} if include_archived else {"status": {"$ne": "archived"}}
        cursor = self._col(self.APPS).find(query, {"_id": 0}).sort("created_at", -1)
        return [AppSpec.model_validate(d) for d in cursor]

    def delete(self, app_id: str) -> bool:
        """Hard-delete the app + its version history; ``True`` if it existed."""
        deleted = self._col(self.APPS).delete_one({"app_id": app_id}).deleted_count > 0
        self._col(self.VERSIONS).delete_many({"app_id": app_id})
        return deleted

    def save_version(self, version: AppVersion) -> None:
        """Append (or replace by ``(app_id, version)``) a version snapshot."""
        self._col(self.VERSIONS).replace_one(
            {"app_id": version.app_id, "version": version.version},
            version.model_dump(mode="json"),
            upsert=True,
        )

    def list_versions(self, app_id: str) -> list[AppVersion]:
        """Return the app's snapshots, oldest-first by version."""
        cursor = self._col(self.VERSIONS).find({"app_id": app_id}, {"_id": 0}).sort("version", 1)
        return [AppVersion.model_validate(d) for d in cursor]

    def get_version(self, app_id: str, version: int) -> AppVersion | None:
        """Return one snapshot by version number, or ``None``."""
        d = self._col(self.VERSIONS).find_one(
            {"app_id": app_id, "version": version}, {"_id": 0}
        )
        return AppVersion.model_validate(d) if d else None


# ---------------------------------------------------------------------------
# Pipeline-run provenance ledger
# ---------------------------------------------------------------------------


class PipelineRunStoreBase(abc.ABC):
    """Abstract base for the :class:`PipelineRun` provenance ledger (spec §2.8)."""

    @abc.abstractmethod
    def open_run(self, run: PipelineRun) -> None:
        """Persist a newly-opened (``running``) ledger entry — the trigger-fire write."""

    @abc.abstractmethod
    def get(self, run_key: str) -> PipelineRun | None:
        """Return one ledger entry by ``run_key``, or ``None``."""

    @abc.abstractmethod
    def get_open(self, app_id: str, pipeline_name: str) -> PipelineRun | None:
        """Return the single open (``running``) entry for a pipeline, or ``None``.

        The ``app_data`` write hook (workstream B) reads this to increment
        ``docs_written`` on the run a fire opened. At most one entry per
        ``(app_id, pipeline_name)`` is ever ``running`` at a time.
        """

    @abc.abstractmethod
    def save(self, run: PipelineRun) -> None:
        """Full-document upsert by ``run_key`` (increment / close write)."""

    @abc.abstractmethod
    def list_runs(
        self, app_id: str, *, pipeline_name: str | None = None, limit: int | None = None
    ) -> list[PipelineRun]:
        """Return an app's ledger entries, newest-first by ``started_at``."""

    def freshness_seconds(
        self, app_id: str, *, now: datetime, pipeline_name: str | None = None
    ) -> float | None:
        """Age (seconds) of the most recent succeeded run, or ``None`` if never.

        Delegates the "which run counts" rule to :meth:`PipelineRun.freshness`
        (the model owns freshness semantics), so both backends + the system
        endpoint read one implementation. ``now`` is a method argument.
        """
        delta = PipelineRun.freshness(self.list_runs(app_id, pipeline_name=pipeline_name), now=now)
        return delta.total_seconds() if delta is not None else None


class JsonPipelineRunStore(PipelineRunStoreBase):
    """Filesystem-backed ledger under ``<cache_dir>/apps/runs/``."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        """Initialise + create the run directory tree."""
        if root_dir is None:
            home = get_config_value("runtime", "cache_dir", default="") or ".mewbo"
            root_dir = Path(home) / "apps"
        self.root_dir = Path(root_dir)
        (self.root_dir / "runs").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _run_dir(self, app_id: str) -> Path:
        return self.root_dir / "runs" / app_id

    def _run_path(self, app_id: str, run_key: str) -> Path:
        return self._run_dir(app_id) / f"{run_key}.json"

    def _write(self, run: PipelineRun) -> None:
        self._run_dir(run.app_id).mkdir(parents=True, exist_ok=True)
        self._run_path(run.app_id, run.run_key).write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )

    def open_run(self, run: PipelineRun) -> None:
        """Persist a newly-opened ledger entry."""
        with self._lock:
            self._write(run)

    def save(self, run: PipelineRun) -> None:
        """Persist an updated ledger entry (increment / close)."""
        with self._lock:
            self._write(run)

    def get(self, run_key: str) -> PipelineRun | None:
        """Return one ledger entry by ``run_key`` (scanning app buckets), or ``None``."""
        with self._lock:
            for app_dir in (self.root_dir / "runs").glob("*"):
                path = app_dir / f"{run_key}.json"
                if path.exists():
                    try:
                        return PipelineRun.model_validate_json(path.read_text(encoding="utf-8"))
                    except Exception:
                        logging.warning("Skipping malformed pipeline run at {}", path)
                        return None
        return None

    def get_open(self, app_id: str, pipeline_name: str) -> PipelineRun | None:
        """Return the single running entry for a pipeline, or ``None``."""
        for run in self.list_runs(app_id, pipeline_name=pipeline_name):
            if run.status == "running":
                return run
        return None

    def list_runs(
        self, app_id: str, *, pipeline_name: str | None = None, limit: int | None = None
    ) -> list[PipelineRun]:
        """Return an app's ledger entries, newest-first (optional pipeline/limit)."""
        with self._lock:
            rdir = self._run_dir(app_id)
            if not rdir.exists():
                return []
            out: list[PipelineRun] = []
            for p in rdir.glob("*.json"):
                try:
                    run = PipelineRun.model_validate_json(p.read_text(encoding="utf-8"))
                except Exception:
                    logging.warning("Skipping malformed pipeline run at {}", p)
                    continue
                if pipeline_name is None or run.pipeline_name == pipeline_name:
                    out.append(run)
        out.sort(key=lambda r: r.started_at, reverse=True)
        return out[:limit] if limit is not None else out


class MongoPipelineRunStore(PipelineRunStoreBase):
    """MongoDB-backed ledger (collection ``app_pipeline_runs``)."""

    RUNS = "app_pipeline_runs"

    def __init__(
        self, *, client: Any = None, uri: str | None = None, database: str | None = None
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

    def _col(self) -> Any:
        return self._db[self.RUNS]

    def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING

        self._col().create_index(
            [("run_key", ASCENDING)], name="ix_app_runs_run_key", unique=True, background=True
        )
        self._col().create_index(
            [("app_id", ASCENDING), ("pipeline_name", ASCENDING), ("status", ASCENDING)],
            name="ix_app_runs_app_pipeline_status",
            background=True,
        )

    def open_run(self, run: PipelineRun) -> None:
        """Persist a newly-opened ledger entry."""
        self.save(run)

    def save(self, run: PipelineRun) -> None:
        """Persist an updated ledger entry (upsert by ``run_key``)."""
        self._col().replace_one(
            {"run_key": run.run_key}, run.model_dump(mode="json"), upsert=True
        )

    def get(self, run_key: str) -> PipelineRun | None:
        """Return one ledger entry by ``run_key``, or ``None``."""
        d = self._col().find_one({"run_key": run_key}, {"_id": 0})
        return PipelineRun.model_validate(d) if d else None

    def get_open(self, app_id: str, pipeline_name: str) -> PipelineRun | None:
        """Return the single running entry for a pipeline, or ``None``."""
        d = self._col().find_one(
            {"app_id": app_id, "pipeline_name": pipeline_name, "status": "running"},
            {"_id": 0},
        )
        return PipelineRun.model_validate(d) if d else None

    def list_runs(
        self, app_id: str, *, pipeline_name: str | None = None, limit: int | None = None
    ) -> list[PipelineRun]:
        """Return an app's ledger entries, newest-first (optional pipeline/limit)."""
        query: dict[str, Any] = {"app_id": app_id}
        if pipeline_name is not None:
            query["pipeline_name"] = pipeline_name
        cursor = self._col().find(query, {"_id": 0}).sort("started_at", -1)
        if limit is not None:
            cursor = cursor.limit(limit)
        return [PipelineRun.model_validate(d) for d in cursor]


# ---------------------------------------------------------------------------
# App data plane (app-ID-keyed, compound (app_id, collection, key))
# ---------------------------------------------------------------------------


class AppDataStoreBase(abc.ABC):
    """Abstract base for the per-app data plane (spec §2.5).

    Compound key ``(app_id, collection, key)``. Ingress is agent-side only (the
    ``app_data`` SessionTool); the REST egress is read-only. ``upsert`` accepts
    an optional owning :class:`CollectionSpec` and, when given, validates the
    document against its JSON Schema before writing — so a schema-violating
    write fails at the store boundary as well as at the tool.
    """

    @staticmethod
    def _validate(collection_spec: CollectionSpec | None, doc: Mapping[str, Any]) -> None:
        """Validate *doc* against *collection_spec* when one is supplied.

        Raises ``jsonschema.ValidationError`` (the ``structured_response.py``
        convention the model follows) — callers catch that type. A ``None``
        spec skips validation (the store persists verbatim).
        """
        if collection_spec is not None:
            collection_spec.validate_doc(doc)

    @abc.abstractmethod
    def upsert(
        self,
        app_id: str,
        collection: str,
        key: str,
        doc: dict[str, Any],
        *,
        collection_spec: CollectionSpec | None = None,
        max_docs: int | None = None,
    ) -> None:
        """Insert-or-replace the document at ``(app_id, collection, key)``.

        Idempotent by the compound key. When *collection_spec* is given, the
        document is schema-validated first (see :meth:`_validate`). When
        *max_docs* is given, a NEW key that would push the collection past that
        cap raises :class:`CollectionCapExceeded` — an update to an existing key
        is always allowed (spec §2.11). Both are additive optional kwargs over the
        frozen positional signature.
        """

    @abc.abstractmethod
    def get(self, app_id: str, collection: str, key: str) -> AppDataDoc | None:
        """Return one document, or ``None`` if absent."""

    @abc.abstractmethod
    def query(
        self,
        app_id: str,
        collection: str,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 100,
        sort: str | None = None,
    ) -> list[AppDataDoc]:
        """Return documents in a collection.

        *filter* is equality matches on top-level ``doc`` fields. *sort* is a
        ``doc`` field name, optionally ``-``-prefixed for descending; the
        default order is newest-first by ``updated_at``. *limit* caps the
        result. (``sort`` is an additive extension over the frozen
        ``filter``/``limit`` signature — a caller that omits it gets the
        default order.)
        """

    @abc.abstractmethod
    def delete(self, app_id: str, collection: str, key: str) -> bool:
        """Delete one document; ``True`` if it existed."""

    @staticmethod
    def _matches_filter(doc: dict[str, Any], flt: dict[str, Any] | None) -> bool:
        """Whether *doc* satisfies every equality clause in *flt* (empty/None ⇒ all)."""
        if not flt:
            return True
        return all(doc.get(k) == v for k, v in flt.items())

    @staticmethod
    def _sorted_docs(docs: list[AppDataDoc], sort: str | None) -> list[AppDataDoc]:
        """Order *docs* by a ``doc`` field (``-`` = descending), default updated_at desc."""
        if not sort:
            return sorted(docs, key=lambda d: d.updated_at, reverse=True)
        descending = sort.startswith("-")
        field = sort[1:] if descending else sort
        return sorted(
            docs,
            key=lambda d: (d.doc.get(field) is None, d.doc.get(field)),
            reverse=descending,
        )


class JsonAppDataStore(AppDataStoreBase):
    """Filesystem-backed data plane under ``<cache_dir>/apps/data/``.

    One JSON file per ``(app_id, collection)`` holding a ``{key: doc-dump}`` map
    — the whole collection loads/writes under a lock (fine at app-data scale;
    the Mongo driver is the production path for larger sets).
    """

    def __init__(self, root_dir: str | Path | None = None) -> None:
        """Initialise + create the data directory tree."""
        if root_dir is None:
            home = get_config_value("runtime", "cache_dir", default="") or ".mewbo"
            root_dir = Path(home) / "apps"
        self.root_dir = Path(root_dir)
        (self.root_dir / "data").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _collection_path(self, app_id: str, collection: str) -> Path:
        return self.root_dir / "data" / app_id / f"{collection}.json"

    def _load(self, app_id: str, collection: str) -> dict[str, dict[str, Any]]:
        path = self._collection_path(app_id, collection)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logging.warning("Skipping malformed app-data collection at {}", path)
            return {}

    def _store(self, app_id: str, collection: str, rows: dict[str, dict[str, Any]]) -> None:
        path = self._collection_path(app_id, collection)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def upsert(
        self,
        app_id: str,
        collection: str,
        key: str,
        doc: dict[str, Any],
        *,
        collection_spec: CollectionSpec | None = None,
        max_docs: int | None = None,
    ) -> None:
        """Insert-or-replace the document at ``(app_id, collection, key)``."""
        self._validate(collection_spec, doc)
        record = AppDataDoc(app_id=app_id, collection=collection, key=key, doc=doc)
        with self._lock:
            rows = self._load(app_id, collection)
            if max_docs is not None and key not in rows and len(rows) >= max_docs:
                raise CollectionCapExceeded(collection, max_docs)
            rows[key] = record.model_dump(mode="json")
            self._store(app_id, collection, rows)

    def get(self, app_id: str, collection: str, key: str) -> AppDataDoc | None:
        """Return one document, or ``None`` if absent."""
        with self._lock:
            raw = self._load(app_id, collection).get(key)
        return AppDataDoc.model_validate(raw) if raw is not None else None

    def query(
        self,
        app_id: str,
        collection: str,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 100,
        sort: str | None = None,
    ) -> list[AppDataDoc]:
        """Return filtered, sorted, limited documents from a collection."""
        with self._lock:
            rows = self._load(app_id, collection)
        docs = [AppDataDoc.model_validate(r) for r in rows.values()]
        docs = [d for d in docs if self._matches_filter(d.doc, filter)]
        docs = self._sorted_docs(docs, sort)
        return docs[: max(0, limit)]

    def delete(self, app_id: str, collection: str, key: str) -> bool:
        """Delete one document; ``True`` if it existed."""
        with self._lock:
            rows = self._load(app_id, collection)
            if key not in rows:
                return False
            del rows[key]
            self._store(app_id, collection, rows)
            return True


class MongoAppDataStore(AppDataStoreBase):
    """MongoDB-backed data plane (collection ``app_data``, compound-keyed)."""

    DATA = "app_data"

    def __init__(
        self, *, client: Any = None, uri: str | None = None, database: str | None = None
    ) -> None:
        """Connect + ensure the compound-unique index."""
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

    def _col(self) -> Any:
        return self._db[self.DATA]

    def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING

        self._col().create_index(
            [("app_id", ASCENDING), ("collection", ASCENDING), ("key", ASCENDING)],
            name="ix_app_data_app_collection_key",
            unique=True,
            background=True,
        )

    def upsert(
        self,
        app_id: str,
        collection: str,
        key: str,
        doc: dict[str, Any],
        *,
        collection_spec: CollectionSpec | None = None,
        max_docs: int | None = None,
    ) -> None:
        """Insert-or-replace the document at ``(app_id, collection, key)``."""
        self._validate(collection_spec, doc)
        record = AppDataDoc(app_id=app_id, collection=collection, key=key, doc=doc)
        if max_docs is not None:
            existing = self._col().find_one(
                {"app_id": app_id, "collection": collection, "key": key}, {"_id": 1}
            )
            if existing is None:
                count = self._col().count_documents(
                    {"app_id": app_id, "collection": collection}
                )
                if count >= max_docs:
                    raise CollectionCapExceeded(collection, max_docs)
        self._col().replace_one(
            {"app_id": app_id, "collection": collection, "key": key},
            record.model_dump(mode="json"),
            upsert=True,
        )

    def get(self, app_id: str, collection: str, key: str) -> AppDataDoc | None:
        """Return one document, or ``None`` if absent."""
        d = self._col().find_one(
            {"app_id": app_id, "collection": collection, "key": key}, {"_id": 0}
        )
        return AppDataDoc.model_validate(d) if d else None

    def query(
        self,
        app_id: str,
        collection: str,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 100,
        sort: str | None = None,
    ) -> list[AppDataDoc]:
        """Return filtered, sorted, limited documents from a collection."""
        query: dict[str, Any] = {"app_id": app_id, "collection": collection}
        for field, value in (filter or {}).items():
            query[f"doc.{field}"] = value
        cursor = self._col().find(query, {"_id": 0})
        if sort:
            descending = sort.startswith("-")
            field = sort[1:] if descending else sort
            cursor = cursor.sort(f"doc.{field}", -1 if descending else 1)
        else:
            cursor = cursor.sort("updated_at", -1)
        cursor = cursor.limit(max(0, limit))
        return [AppDataDoc.model_validate(d) for d in cursor]

    def delete(self, app_id: str, collection: str, key: str) -> bool:
        """Delete one document; ``True`` if it existed."""
        return (
            self._col()
            .delete_one({"app_id": app_id, "collection": collection, "key": key})
            .deleted_count
            > 0
        )


# ---------------------------------------------------------------------------
# Factories + process singletons
# ---------------------------------------------------------------------------


def _driver() -> str:
    """The configured storage driver (``storage.driver``; default ``json``).

    Read from CONFIG, never a ``MEWBO_*`` env — the divergence spec §8 warns
    about (the wiki/scg surfaces read config too).
    """
    return get_config_value("storage", "driver", default="json")


def create_app_store() -> AppStoreBase:
    """Return the configured app-manifest store driver."""
    return MongoAppStore() if _driver() == "mongodb" else JsonAppStore()


def create_pipeline_run_store() -> PipelineRunStoreBase:
    """Return the configured pipeline-run ledger driver."""
    return MongoPipelineRunStore() if _driver() == "mongodb" else JsonPipelineRunStore()


def create_app_data_store() -> AppDataStoreBase:
    """Return the configured app-data-plane driver."""
    return MongoAppDataStore() if _driver() == "mongodb" else JsonAppDataStore()


_app_store: AppStoreBase | None = None
_run_store: PipelineRunStoreBase | None = None
_data_store: AppDataStoreBase | None = None
_singleton_lock = threading.Lock()


def get_app_store() -> AppStoreBase:
    """Process-wide app store (routes + lifecycle + the plugin all share it)."""
    global _app_store
    with _singleton_lock:
        if _app_store is None:
            _app_store = create_app_store()
        return _app_store


def get_pipeline_run_store() -> PipelineRunStoreBase:
    """Process-wide pipeline-run ledger (routes, lifecycle, the ``app_data`` tool)."""
    global _run_store
    with _singleton_lock:
        if _run_store is None:
            _run_store = create_pipeline_run_store()
        return _run_store


def get_app_data_store() -> AppDataStoreBase:
    """Process-wide app-data plane (the read routes + the ``app_data`` tool)."""
    global _data_store
    with _singleton_lock:
        if _data_store is None:
            _data_store = create_app_data_store()
        return _data_store


def set_stores_for_tests(
    *,
    app_store: AppStoreBase | None = None,
    run_store: PipelineRunStoreBase | None = None,
    data_store: AppDataStoreBase | None = None,
) -> None:
    """Override the process-wide store singletons (test seam)."""
    global _app_store, _run_store, _data_store
    with _singleton_lock:
        _app_store = app_store
        _run_store = run_store
        _data_store = data_store


__all__ = [
    "CollectionCapExceeded",
    "new_app_id",
    "new_run_key",
    "AppStoreBase",
    "JsonAppStore",
    "MongoAppStore",
    "PipelineRunStoreBase",
    "JsonPipelineRunStore",
    "MongoPipelineRunStore",
    "AppDataStoreBase",
    "JsonAppDataStore",
    "MongoAppDataStore",
    "create_app_store",
    "create_pipeline_run_store",
    "create_app_data_store",
    "get_app_store",
    "get_pipeline_run_store",
    "get_app_data_store",
    "set_stores_for_tests",
]
