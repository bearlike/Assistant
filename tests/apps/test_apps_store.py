"""Contract tests for the Mewbo Apps stores (JSON + Mongo drivers, real I/O).

Drives the store base's public methods from the caller site (routes/lifecycle
use exactly these); the JSON driver is exercised end-to-end on a tempdir, the
Mongo driver end-to-end against ``mongomock`` (both are the one true I/O
boundary for their backend). Injected timestamps everywhere (no wall clock).

Production runs ``storage.driver=mongodb`` — the JSON suite alone cannot catch
a regression in the Mongo drivers (e.g. a stale reference in a code path only
Mongo takes), so ``TestMongoAppDataStore`` mirrors the JSON contract tests
against the real class, not a fake.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jsonschema
import mongomock
import pytest
from mewbo_api.apps.models import (
    AppDataDoc,
    AppFrontend,
    AppSpec,
    AppVersion,
    CollectionSpec,
    PipelineRun,
    WorkspaceRef,
)
from mewbo_api.apps.store import (
    CollectionCapExceeded,
    JsonAppDataStore,
    JsonAppStore,
    JsonPipelineRunStore,
    MongoAppDataStore,
)

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


def _frontend() -> AppFrontend:
    return AppFrontend(entrypoint="app.py", files={"app.py": "import streamlit as st\n"})


def _app(app_id: str, *, status: str = "live", created_at: datetime = NOW) -> AppSpec:
    return AppSpec(
        app_id=app_id,
        title=f"App {app_id}",
        owner_session_id="owner-1",
        workspace_ref=WorkspaceRef(kind="own", key="default"),
        frontend=_frontend(),
        status=status,  # type: ignore[arg-type]
        created_at=created_at,
        updated_at=created_at,
    )


# ---------------------------------------------------------------------------
# App manifest + version history
# ---------------------------------------------------------------------------


class TestJsonAppStore:
    def test_save_get_roundtrip(self, tmp_path):
        store = JsonAppStore(root_dir=tmp_path)
        spec = _app("app-1")
        store.save(spec)
        got = store.get("app-1")
        assert got is not None
        assert got.app_id == "app-1"
        assert got.status == "live"

    def test_get_absent_is_none(self, tmp_path):
        assert JsonAppStore(root_dir=tmp_path).get("nope") is None

    def test_list_hides_archived_by_default(self, tmp_path):
        store = JsonAppStore(root_dir=tmp_path)
        store.save(_app("app-live", status="live"))
        store.save(_app("app-arch", status="archived"))
        ids = {a.app_id for a in store.list_apps()}
        assert ids == {"app-live"}
        ids_all = {a.app_id for a in store.list_apps(include_archived=True)}
        assert ids_all == {"app-live", "app-arch"}

    def test_list_newest_first(self, tmp_path):
        store = JsonAppStore(root_dir=tmp_path)
        store.save(_app("old", created_at=NOW))
        store.save(_app("new", created_at=NOW + timedelta(hours=1)))
        assert [a.app_id for a in store.list_apps()] == ["new", "old"]

    def test_delete_removes_app_and_versions(self, tmp_path):
        store = JsonAppStore(root_dir=tmp_path)
        spec = _app("app-1")
        store.save(spec)
        store.save_version(AppVersion(app_id="app-1", version=1, spec=spec, author="builder"))
        assert store.delete("app-1") is True
        assert store.get("app-1") is None
        assert store.list_versions("app-1") == []
        assert store.delete("app-1") is False

    def test_version_history_and_latest(self, tmp_path):
        store = JsonAppStore(root_dir=tmp_path)
        spec = _app("app-1")
        assert store.latest_version("app-1") == 0
        store.save_version(AppVersion(app_id="app-1", version=1, spec=spec, author="builder"))
        store.save_version(AppVersion(app_id="app-1", version=2, spec=spec, author="user"))
        assert [v.version for v in store.list_versions("app-1")] == [1, 2]
        assert store.latest_version("app-1") == 2
        assert store.get_version("app-1", 2).author == "user"
        assert store.get_version("app-1", 9) is None


# ---------------------------------------------------------------------------
# Pipeline-run provenance ledger
# ---------------------------------------------------------------------------


class TestJsonPipelineRunStore:
    def test_open_get_open_and_close(self, tmp_path):
        store = JsonPipelineRunStore(root_dir=tmp_path)
        run = PipelineRun.open(
            run_key="run-1", app_id="app-1", pipeline_name="ingest", now=NOW
        )
        store.open_run(run)
        assert store.get("run-1") is not None
        assert store.get_open("app-1", "ingest").run_key == "run-1"
        # Close it → no longer "open".
        run.close(now=NOW + timedelta(seconds=5), status="succeeded")
        store.save(run)
        assert store.get_open("app-1", "ingest") is None
        assert store.get("run-1").status == "succeeded"

    def test_list_newest_first_and_limit(self, tmp_path):
        store = JsonPipelineRunStore(root_dir=tmp_path)
        for i in range(3):
            store.open_run(
                PipelineRun.open(
                    run_key=f"run-{i}",
                    app_id="app-1",
                    pipeline_name="ingest",
                    now=NOW + timedelta(minutes=i),
                )
            )
        keys = [r.run_key for r in store.list_runs("app-1")]
        assert keys == ["run-2", "run-1", "run-0"]
        assert [r.run_key for r in store.list_runs("app-1", limit=1)] == ["run-2"]

    def test_freshness_seconds_counts_only_succeeded(self, tmp_path):
        store = JsonPipelineRunStore(root_dir=tmp_path)
        failed = PipelineRun.open(run_key="f", app_id="app-1", pipeline_name="p", now=NOW)
        failed.close(now=NOW + timedelta(minutes=1), status="failed", error="boom")
        store.save(failed)
        assert store.freshness_seconds("app-1", now=NOW + timedelta(minutes=2)) is None
        ok = PipelineRun.open(run_key="ok", app_id="app-1", pipeline_name="p", now=NOW)
        ok.close(now=NOW + timedelta(minutes=1), status="succeeded")
        store.save(ok)
        fresh = store.freshness_seconds("app-1", now=NOW + timedelta(minutes=2))
        assert fresh == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# App data plane
# ---------------------------------------------------------------------------


class TestJsonAppDataStore:
    def test_upsert_get_delete(self, tmp_path):
        store = JsonAppDataStore(root_dir=tmp_path)
        store.upsert("app-1", "tasks", "t1", {"title": "hi", "done": False})
        got = store.get("app-1", "tasks", "t1")
        assert isinstance(got, AppDataDoc)
        assert got.doc == {"title": "hi", "done": False}
        # Upsert is idempotent by compound key (replace, not append).
        store.upsert("app-1", "tasks", "t1", {"title": "bye", "done": True})
        assert store.get("app-1", "tasks", "t1").doc["title"] == "bye"
        assert store.delete("app-1", "tasks", "t1") is True
        assert store.get("app-1", "tasks", "t1") is None
        assert store.delete("app-1", "tasks", "t1") is False

    def test_query_filter_sort_limit(self, tmp_path):
        store = JsonAppDataStore(root_dir=tmp_path)
        store.upsert("app-1", "tasks", "a", {"done": False, "priority": 2})
        store.upsert("app-1", "tasks", "b", {"done": True, "priority": 5})
        store.upsert("app-1", "tasks", "c", {"done": False, "priority": 9})
        # Equality filter on a doc field.
        open_keys = {d.key for d in store.query("app-1", "tasks", filter={"done": False})}
        assert open_keys == {"a", "c"}
        # Descending sort by a doc field.
        ordered = store.query("app-1", "tasks", sort="-priority")
        assert [d.doc["priority"] for d in ordered] == [9, 5, 2]
        # Limit caps the result.
        assert len(store.query("app-1", "tasks", limit=1)) == 1

    def test_query_scoped_to_app_and_collection(self, tmp_path):
        store = JsonAppDataStore(root_dir=tmp_path)
        store.upsert("app-1", "tasks", "a", {"n": 1})
        store.upsert("app-2", "tasks", "a", {"n": 2})
        store.upsert("app-1", "notes", "a", {"n": 3})
        rows = store.query("app-1", "tasks")
        assert [d.doc["n"] for d in rows] == [1]

    def test_upsert_validates_against_collection_schema(self, tmp_path):
        store = JsonAppDataStore(root_dir=tmp_path)
        spec = CollectionSpec(
            name="tasks",
            json_schema={
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            },
        )
        store.upsert("app-1", "tasks", "ok", {"title": "hi"}, collection_spec=spec)
        assert store.get("app-1", "tasks", "ok") is not None
        with pytest.raises(jsonschema.ValidationError):
            store.upsert("app-1", "tasks", "bad", {"done": True}, collection_spec=spec)
        # The rejected doc was never written.
        assert store.get("app-1", "tasks", "bad") is None

    def test_max_docs_caps_new_keys_but_never_updates(self, tmp_path):
        store = JsonAppDataStore(root_dir=tmp_path)
        store.upsert("app-1", "tasks", "t1", {"n": 1}, max_docs=2)
        store.upsert("app-1", "tasks", "t2", {"n": 2}, max_docs=2)
        # A THIRD new key breaches the 2-doc cap.
        with pytest.raises(CollectionCapExceeded):
            store.upsert("app-1", "tasks", "t3", {"n": 3}, max_docs=2)
        assert store.get("app-1", "tasks", "t3") is None
        # An UPDATE to an existing key is always allowed, even at the cap.
        store.upsert("app-1", "tasks", "t1", {"n": 11}, max_docs=2)
        assert store.get("app-1", "tasks", "t1").doc == {"n": 11}
        # The cap is per (app_id, collection): another collection is independent.
        store.upsert("app-1", "notes", "n1", {"n": 1}, max_docs=2)
        assert store.get("app-1", "notes", "n1") is not None


# ---------------------------------------------------------------------------
# App data plane — Mongo driver (mongomock, the same class production runs)
# ---------------------------------------------------------------------------


class TestMongoAppDataStore:
    """Mirrors ``TestJsonAppDataStore`` against ``MongoAppDataStore`` for real.

    ``client=`` is the constructor's own DI seam (used for exactly this — no
    patching an import site needed), so this drives the SAME ``query``/
    ``upsert``/``delete`` methods routes and the ``app_data`` tool call in
    production, backed by ``mongomock`` instead of a live server.
    """

    @staticmethod
    def _store() -> MongoAppDataStore:
        return MongoAppDataStore(client=mongomock.MongoClient(), database="test_apps")

    def test_upsert_get_delete(self) -> None:
        store = self._store()
        store.upsert("app-1", "tasks", "t1", {"title": "hi", "done": False})
        got = store.get("app-1", "tasks", "t1")
        assert isinstance(got, AppDataDoc)
        assert got.doc == {"title": "hi", "done": False}
        # Upsert is idempotent by compound key (replace, not append).
        store.upsert("app-1", "tasks", "t1", {"title": "bye", "done": True})
        assert store.get("app-1", "tasks", "t1").doc["title"] == "bye"
        assert store.delete("app-1", "tasks", "t1") is True
        assert store.get("app-1", "tasks", "t1") is None
        assert store.delete("app-1", "tasks", "t1") is False

    def test_query_filter_sort_limit(self) -> None:
        store = self._store()
        store.upsert("app-1", "tasks", "a", {"done": False, "priority": 2})
        store.upsert("app-1", "tasks", "b", {"done": True, "priority": 5})
        store.upsert("app-1", "tasks", "c", {"done": False, "priority": 9})
        # Equality filter on a doc field (the Mongo driver's own dotted-path query,
        # never routed through the JSON driver's _matches_filter helper).
        open_keys = {d.key for d in store.query("app-1", "tasks", filter={"done": False})}
        assert open_keys == {"a", "c"}
        # Descending sort by a doc field (native Mongo cursor.sort, not _sorted_docs).
        ordered = store.query("app-1", "tasks", sort="-priority")
        assert [d.doc["priority"] for d in ordered] == [9, 5, 2]
        # Limit caps the result.
        assert len(store.query("app-1", "tasks", limit=1)) == 1

    def test_query_scoped_to_app_and_collection(self) -> None:
        store = self._store()
        store.upsert("app-1", "tasks", "a", {"n": 1})
        store.upsert("app-2", "tasks", "a", {"n": 2})
        store.upsert("app-1", "notes", "a", {"n": 3})
        rows = store.query("app-1", "tasks")
        assert [d.doc["n"] for d in rows] == [1]

    def test_upsert_validates_against_collection_schema(self) -> None:
        store = self._store()
        spec = CollectionSpec(
            name="tasks",
            json_schema={
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            },
        )
        store.upsert("app-1", "tasks", "ok", {"title": "hi"}, collection_spec=spec)
        assert store.get("app-1", "tasks", "ok") is not None
        with pytest.raises(jsonschema.ValidationError):
            store.upsert("app-1", "tasks", "bad", {"done": True}, collection_spec=spec)
        assert store.get("app-1", "tasks", "bad") is None

    def test_max_docs_caps_new_keys_but_never_updates(self) -> None:
        store = self._store()
        store.upsert("app-1", "tasks", "t1", {"n": 1}, max_docs=2)
        store.upsert("app-1", "tasks", "t2", {"n": 2}, max_docs=2)
        with pytest.raises(CollectionCapExceeded):
            store.upsert("app-1", "tasks", "t3", {"n": 3}, max_docs=2)
        assert store.get("app-1", "tasks", "t3") is None
        # An UPDATE to an existing key is always allowed, even at the cap.
        store.upsert("app-1", "tasks", "t1", {"n": 11}, max_docs=2)
        assert store.get("app-1", "tasks", "t1").doc == {"n": 11}
