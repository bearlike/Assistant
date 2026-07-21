"""Tests for the app_data SessionTool (mewbo_api.apps.plugin.app_data).

Drives the tool with FAKE stores (Protocol satisfiers). Covers the three
guarantees the tool owns: SCOPE (only this session's app; foreign/unknown reads
not_found uniformly), SCHEMA (upsert validated by the store via the passed
collection_spec), and PROVENANCE (a pipeline write increments the open ledger
entry: get_open -> record_write -> save). Plus query/delete, the collection_spec
and sort passthroughs, arg validation, factory resolution, the
store-unavailable degrade, and the terminal-free contract.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone

from mewbo_api.apps import store as store_mod
from mewbo_api.apps.models import (
    AppDataDoc,
    AppFrontend,
    AppSpec,
    CollectionSpec,
    PipelineRun,
    WorkspaceRef,
)
from mewbo_api.apps.plugin.app_data import AppDataTool
from mewbo_core.classes import ActionStep

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "s1"
APP_ID = "app1"

_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "sender": {"type": "string"}},
    "required": ["subject"],
    "additionalProperties": False,
}


def _app(*, maintainer=SESSION_ID) -> AppSpec:
    return AppSpec(
        app_id=APP_ID,
        title="App One",
        owner_session_id="owner",
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(files={"app.py": "import streamlit as st"}),
        collections=[CollectionSpec(name="emails", json_schema=_EMAIL_SCHEMA)],
        maintainer_session_id=maintainer,
    )


class FakeAppStore:
    def __init__(self, *apps: AppSpec) -> None:
        self._apps = {a.app_id: a for a in apps}

    def get(self, app_id: str) -> AppSpec | None:
        return self._apps.get(app_id)


class FakeDataStore:
    """Mirrors A's store: validates against ``collection_spec`` when one is given."""

    def __init__(self, *, rows: list[AppDataDoc] | None = None) -> None:
        self.upserts: list[tuple] = []  # (app_id, collection, key, doc, collection_spec)
        self.deletes: list[tuple] = []
        self.query_calls: list[tuple] = []  # (app_id, collection, filter, limit, sort)
        self._rows = rows or []
        self._present: set[tuple] = set()

    def upsert(self, app_id, collection, key, doc, *, collection_spec=None, max_docs=None) -> None:
        if collection_spec is not None:
            collection_spec.validate_doc(doc)  # raises jsonschema.ValidationError (A's contract)
        if max_docs is not None and (app_id, collection, key) not in self._present:
            from mewbo_api.apps.store import CollectionCapExceeded

            current = sum(1 for (a, c, _k) in self._present if a == app_id and c == collection)
            if current >= max_docs:
                raise CollectionCapExceeded(collection, max_docs)
        self.upserts.append((app_id, collection, key, doc, collection_spec))
        self._present.add((app_id, collection, key))

    def query(self, app_id, collection, *, filter=None, limit=100, sort=None):  # noqa: A002
        self.query_calls.append((app_id, collection, filter, limit, sort))
        return list(self._rows)

    def delete(self, app_id: str, collection: str, key: str) -> bool:
        self.deletes.append((app_id, collection, key))
        existed = (app_id, collection, key) in self._present
        self._present.discard((app_id, collection, key))
        return existed


class FakeRunStore:
    def __init__(self, *, open_run: PipelineRun | None = None) -> None:
        self._open = open_run
        self.get_open_calls: list[tuple] = []
        self.saved: list[PipelineRun] = []

    def get_open(self, app_id: str, pipeline_name: str) -> PipelineRun | None:
        self.get_open_calls.append((app_id, pipeline_name))
        return self._open

    def save(self, run: PipelineRun) -> None:
        self.saved.append(run)


def _tool(app_store, data_store, run_store, *, session_id=SESSION_ID) -> AppDataTool:
    return AppDataTool(
        session_id=session_id,
        app_store=app_store,
        data_store=data_store,
        run_store=run_store,
    )


def _run(tool: AppDataTool, tool_input: dict):
    step = ActionStep(tool_id="app_data", operation="execute", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


def _payload(speaker) -> dict:
    return ast.literal_eval(speaker.content)


def _is_error(speaker) -> bool:
    payload = _payload(speaker)
    return isinstance(payload, dict) and "error" in payload


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_tool_id_modes_and_terminal_free():
    assert AppDataTool.tool_id == "app_data"
    assert AppDataTool.modes == frozenset({"act"})
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), FakeRunStore())
    assert tool.should_terminate_run() is False


# ---------------------------------------------------------------------------
# Scope — only this session's app; foreign/unknown => not_found uniformly
# ---------------------------------------------------------------------------


def test_unknown_app_is_not_found():
    tool = _tool(FakeAppStore(), FakeDataStore(), FakeRunStore())
    result = _run(tool, {"operation": "query", "app_id": "ghost", "collection": "emails"})
    assert _payload(result)["error"]["code"] == "not_found"


def test_foreign_app_is_not_found_no_leak():
    # A real app owned by ANOTHER session reads identically to a missing one.
    tool = _tool(FakeAppStore(_app(maintainer="other")), FakeDataStore(), FakeRunStore())
    result = _run(tool, {"operation": "query", "app_id": APP_ID, "collection": "emails"})
    payload = _payload(result)
    assert payload["error"]["code"] == "not_found"
    assert "other" not in payload["error"]["message"]


# ---------------------------------------------------------------------------
# Schema — the store validates the upsert via the passed collection_spec
# ---------------------------------------------------------------------------


def test_upsert_schema_rejection():
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), FakeRunStore())
    # missing required "subject" -> the store's validate_doc raises -> clean error
    result = _run(
        tool,
        {"operation": "upsert", "app_id": APP_ID, "collection": "emails", "key": "k1",
         "doc": {"sender": "x@y"}},
    )
    assert _payload(result)["error"]["code"] == "schema"


def test_upsert_passes_collection_spec():
    data_store = FakeDataStore()
    app = _app()
    tool = _tool(FakeAppStore(app), data_store, FakeRunStore())
    _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails", "key": "k1",
                "doc": {"subject": "hi"}})
    # the tool hands the collection's CollectionSpec to the store so IT validates
    assert data_store.upserts[0][4] is app.collections[0]


def test_unknown_collection_is_validation_error():
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), FakeRunStore())
    result = _run(
        tool,
        {"operation": "upsert", "app_id": APP_ID, "collection": "ghosts", "key": "k1",
         "doc": {"subject": "s"}},
    )
    assert _payload(result)["error"]["code"] == "validation"


# ---------------------------------------------------------------------------
# Provenance — a pipeline write increments the OPEN ledger entry
# ---------------------------------------------------------------------------


def test_upsert_with_pipeline_increments_open_run():
    data_store = FakeDataStore()
    open_run = PipelineRun.open(run_key="r1", app_id=APP_ID, pipeline_name="daily", now=NOW)
    run_store = FakeRunStore(open_run=open_run)
    tool = _tool(FakeAppStore(_app()), data_store, run_store)

    result = _run(
        tool,
        {"operation": "upsert", "app_id": APP_ID, "collection": "emails", "key": "k1",
         "doc": {"subject": "hello"}, "pipeline": "daily"},
    )

    assert not _is_error(result)
    assert data_store.upserts[0][:4] == (APP_ID, "emails", "k1", {"subject": "hello"})
    assert run_store.get_open_calls == [(APP_ID, "daily")]
    assert run_store.saved == [open_run]
    assert open_run.docs_written == {"emails": 1}


def test_upsert_without_pipeline_leaves_ledger_untouched():
    run_store = FakeRunStore(open_run=PipelineRun.open(
        run_key="r1", app_id=APP_ID, pipeline_name="daily", now=NOW))
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), run_store)

    _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails", "key": "k1",
                "doc": {"subject": "hi"}})

    assert run_store.get_open_calls == []
    assert run_store.saved == []


def test_upsert_with_pipeline_but_no_open_run_does_not_save():
    run_store = FakeRunStore(open_run=None)
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), run_store)

    _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails", "key": "k1",
                "doc": {"subject": "hi"}, "pipeline": "daily"})

    assert run_store.get_open_calls == [(APP_ID, "daily")]
    assert run_store.saved == []


# ---------------------------------------------------------------------------
# query / delete
# ---------------------------------------------------------------------------


def test_query_returns_rows():
    row = AppDataDoc(app_id=APP_ID, collection="emails", key="k1",
                     doc={"subject": "hi"}, updated_at=NOW)
    data_store = FakeDataStore(rows=[row])
    tool = _tool(FakeAppStore(_app()), data_store, FakeRunStore())

    result = _run(tool, {"operation": "query", "app_id": APP_ID, "collection": "emails",
                         "filter": {"subject": "hi"}, "limit": 10})

    payload = _payload(result)
    assert payload["count"] == 1
    assert payload["documents"][0]["key"] == "k1"
    assert payload["documents"][0]["doc"] == {"subject": "hi"}
    assert payload["documents"][0]["updated_at"] == NOW.isoformat()
    assert data_store.query_calls == [(APP_ID, "emails", {"subject": "hi"}, 10, None)]


def test_query_passes_sort():
    data_store = FakeDataStore()
    tool = _tool(FakeAppStore(_app()), data_store, FakeRunStore())
    _run(tool, {"operation": "query", "app_id": APP_ID, "collection": "emails",
                "sort": "-received_at"})
    assert data_store.query_calls == [(APP_ID, "emails", None, 100, "-received_at")]


def test_delete_reports_existence():
    data_store = FakeDataStore()
    data_store.upsert(APP_ID, "emails", "k1", {"subject": "hi"})
    tool = _tool(FakeAppStore(_app()), data_store, FakeRunStore())

    base = {"operation": "delete", "app_id": APP_ID, "collection": "emails"}
    hit = _run(tool, {**base, "key": "k1"})
    miss = _run(tool, {**base, "key": "gone"})

    assert _payload(hit)["deleted"] is True
    assert _payload(miss)["deleted"] is False


# ---------------------------------------------------------------------------
# Arg validation + store resolution
# ---------------------------------------------------------------------------


def test_upsert_without_key_is_validation_error():
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), FakeRunStore())
    result = _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails",
                         "doc": {"subject": "s"}})
    assert _payload(result)["error"]["code"] == "validation"


def test_extra_field_is_validation_error():
    tool = _tool(FakeAppStore(_app()), FakeDataStore(), FakeRunStore())
    result = _run(tool, {"operation": "query", "app_id": APP_ID, "collection": "emails",
                         "bogus": 1})
    assert _payload(result)["error"]["code"] == "validation"


def test_factory_resolves_stores_when_not_injected():
    data_store = FakeDataStore()
    store_mod.set_stores_for_tests(
        app_store=FakeAppStore(_app()), data_store=data_store, run_store=FakeRunStore()
    )
    try:
        tool = AppDataTool(session_id=SESSION_ID)  # no explicit stores -> A's factories
        result = _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails",
                             "key": "k1", "doc": {"subject": "hi"}})
        assert not _is_error(result)
        assert data_store.upserts[0][:4] == (APP_ID, "emails", "k1", {"subject": "hi"})
    finally:
        store_mod.set_stores_for_tests(app_store=None, data_store=None, run_store=None)


def test_store_resolution_failure_is_clean_error(monkeypatch):
    def _boom() -> None:
        raise RuntimeError("store backend unreachable")

    monkeypatch.setattr("mewbo_api.apps.plugin.app_data.get_app_store", _boom)
    tool = AppDataTool(session_id=SESSION_ID)  # no injected stores -> factory -> raises
    result = _run(tool, {"operation": "query", "app_id": APP_ID, "collection": "emails"})
    assert _payload(result)["error"]["code"] == "unavailable"


# ---------------------------------------------------------------------------
# I5 — policies.max_docs_per_collection (a new key at the cap is refused)
# ---------------------------------------------------------------------------


def test_upsert_new_key_at_cap_is_refused_but_update_is_allowed(tmp_path):
    from mewbo_api.apps.models import AppPolicies
    from mewbo_api.apps.store import JsonAppDataStore

    app = _app().model_copy(update={"policies": AppPolicies(max_docs_per_collection=1)})
    data_store = JsonAppDataStore(root_dir=tmp_path / "apps")  # real store owns the cap
    tool = _tool(FakeAppStore(app), data_store, FakeRunStore())

    first = _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails",
                        "key": "e1", "doc": {"subject": "a"}})
    assert not _is_error(first)

    # A NEW key at the cap is refused with a clean, agent-visible cap error.
    capped = _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails",
                         "key": "e2", "doc": {"subject": "b"}})
    assert _payload(capped)["error"]["code"] == "cap"

    # Updating an EXISTING key is always allowed, even while at the cap.
    updated = _run(tool, {"operation": "upsert", "app_id": APP_ID, "collection": "emails",
                          "key": "e1", "doc": {"subject": "a2"}})
    assert not _is_error(updated)
