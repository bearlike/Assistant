"""Tests for the Agentic Search persistence backends (JSON + optional Mongo)."""

# mypy: ignore-errors

import os

import pytest
from mewbo_api.agentic_search.schemas import (
    PastQuery,
    RunRecord,
    Workspace,
    WorkspaceInput,
)
from mewbo_api.agentic_search.store import (
    PAST_QUERY_CAP,
    JsonAgenticSearchStore,
    MongoAgenticSearchStore,
)


def _record(run_id="run-1", workspace_id="ws-1", **kw):
    return RunRecord(
        run_id=run_id,
        session_id=f"sess-{run_id}",
        workspace_id=workspace_id,
        query="q",
        **kw,
    )


# ---------------------------------------------------------------------------
# JSON store — workspaces
# ---------------------------------------------------------------------------


def test_json_workspace_create_get_list(tmp_path):
    """create_workspace persists; get + list return it."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    assert store.list_workspaces() == []
    ws = store.create_workspace(
        WorkspaceInput(name="QA", desc="d", sources=["notion"], instructions="be thorough")
    )
    assert ws.id.startswith("ws-")
    fetched = store.get_workspace(ws.id)
    assert fetched is not None
    assert fetched.name == "QA"
    assert fetched.sources == ["notion"]
    listed = store.list_workspaces()
    assert [w.id for w in listed] == [ws.id]


def test_json_workspace_partial_update_preserves_untouched(tmp_path):
    """update_workspace patches given fields and preserves the rest."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    ws = store.create_workspace(
        WorkspaceInput(name="QA", sources=["notion", "github"], instructions="keep me")
    )
    updated = store.update_workspace(ws.id, {"name": "QA renamed", "sources": ["notion"]})
    assert updated is not None
    assert updated.name == "QA renamed"
    assert updated.sources == ["notion"]
    # Untouched field preserved.
    assert updated.instructions == "keep me"
    # updated_at advances (or at least stays valid).
    assert updated.updated_at
    # None / absent fields are ignored, not blanked.
    again = store.update_workspace(ws.id, {"desc": "now described"})
    assert again.name == "QA renamed"
    assert again.instructions == "keep me"


def test_json_update_missing_workspace_returns_none(tmp_path):
    """update_workspace on an unknown id returns None."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    assert store.update_workspace("nope", {"name": "x"}) is None


def test_json_workspace_delete(tmp_path):
    """delete_workspace returns True once, then False."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    ws = store.create_workspace(WorkspaceInput(name="QA"))
    assert store.delete_workspace(ws.id) is True
    assert store.get_workspace(ws.id) is None
    assert store.delete_workspace(ws.id) is False


def test_json_save_workspace_verbatim(tmp_path):
    """save_workspace persists the exact object (including its id + timestamps)."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    ws = Workspace(id="ws-seed", name="Seed", sources=["web"])
    store.save_workspace(ws)
    fetched = store.get_workspace("ws-seed")
    assert fetched == ws


# ---------------------------------------------------------------------------
# JSON store — runs
# ---------------------------------------------------------------------------


def test_json_run_create_get_update_list(tmp_path):
    """Run CRUD: create/get, partial update, and list-by-workspace filtering."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_run(_record("run-1", "ws-1", status="running"))
    store.create_run(_record("run-2", "ws-2", status="running"))

    got = store.get_run("run-1")
    assert got is not None
    assert got.workspace_id == "ws-1"

    updated = store.update_run("run-1", status="completed", total_ms=42)
    assert updated.status == "completed"
    assert updated.total_ms == 42
    assert store.get_run("run-1").status == "completed"

    all_runs = store.list_runs()
    assert {r.run_id for r in all_runs} == {"run-1", "run-2"}
    ws1_runs = store.list_runs("ws-1")
    assert [r.run_id for r in ws1_runs] == ["run-1"]


def test_json_missing_run_get_returns_none_update_raises(tmp_path):
    """get_run on an unknown id returns None; update raises KeyError."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    assert store.get_run("ghost") is None
    with pytest.raises(KeyError):
        store.update_run("ghost", status="completed")


def test_json_event_log_idx_monotonic_and_after_idx(tmp_path):
    """append_run_event yields a monotonic idx; load filters by after_idx."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_run(_record("run-1"))
    idxs = [
        store.append_run_event("run-1", {"type": "run_started"}),
        store.append_run_event("run-1", {"type": "result"}),
        store.append_run_event("run-1", {"type": "run_done"}),
    ]
    assert idxs == [0, 1, 2]

    all_events = store.load_run_events("run-1")
    assert [e["idx"] for e in all_events] == [0, 1, 2]
    assert [e["type"] for e in all_events] == ["run_started", "result", "run_done"]

    # after_idx filtering: only events strictly greater than the cursor.
    tail = store.load_run_events("run-1", after_idx=0)
    assert [e["idx"] for e in tail] == [1, 2]
    assert store.load_run_events("run-1", after_idx=2) == []
    # Unknown run -> empty event log.
    assert store.load_run_events("ghost") == []


def test_json_cancel_run(tmp_path):
    """cancel_run is True first, False once terminal, and logs a cancelled event."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_run(_record("run-1", status="running"))
    assert store.cancel_run("run-1") is True
    assert store.get_run("run-1").status == "cancelled"
    # Second cancel is a no-op (already terminal).
    assert store.cancel_run("run-1") is False
    # A cancelled event was appended exactly once.
    events = store.load_run_events("run-1")
    cancelled = [e for e in events if e.get("type") == "cancelled"]
    assert len(cancelled) == 1
    # Unknown run cannot be cancelled.
    assert store.cancel_run("ghost") is False


# ---------------------------------------------------------------------------
# JSON store — past query history
# ---------------------------------------------------------------------------


def test_json_append_past_query_caps_history(tmp_path):
    """append_past_query prepends and caps the history at PAST_QUERY_CAP."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    ws = store.create_workspace(WorkspaceInput(name="QA"))
    for i in range(PAST_QUERY_CAP + 5):
        store.append_past_query(ws.id, PastQuery(q=f"query-{i}", run_id=f"run-{i}"))
    history = store.get_workspace(ws.id).past_queries
    assert len(history) == PAST_QUERY_CAP
    # Newest entry is at the front (prepend semantics).
    assert history[0].q == f"query-{PAST_QUERY_CAP + 4}"
    # Append on an unknown workspace is a silent no-op.
    store.append_past_query("ghost", PastQuery(q="x"))


def test_json_update_past_query_patches_by_run_id(tmp_path):
    """update_past_query patches the matching run_id entry in place."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    ws = store.create_workspace(WorkspaceInput(name="QA"))
    store.append_past_query(ws.id, PastQuery(q="a", run_id="run-a", status="running"))
    store.append_past_query(ws.id, PastQuery(q="b", run_id="run-b", status="running"))

    store.update_past_query(ws.id, "run-a", status="completed", results=7)
    history = {pq.run_id: pq for pq in store.get_workspace(ws.id).past_queries}
    assert history["run-a"].status == "completed"
    assert history["run-a"].results == 7
    # The other entry is untouched.
    assert history["run-b"].status == "running"
    assert history["run-b"].results == 0


# ---------------------------------------------------------------------------
# JSON store — persistence across instances
# ---------------------------------------------------------------------------


def test_json_persistence_survives_fresh_instance(tmp_path):
    """A fresh store on the same root sees previously written data."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    ws = store.create_workspace(WorkspaceInput(name="QA", sources=["web"]))
    store.create_run(_record("run-1", ws.id, status="running"))
    store.append_run_event("run-1", {"type": "run_started"})

    reopened = JsonAgenticSearchStore(root_dir=tmp_path)
    assert reopened.get_workspace(ws.id).name == "QA"
    assert reopened.get_run("run-1") is not None
    assert [e["type"] for e in reopened.load_run_events("run-1")] == ["run_started"]


# ---------------------------------------------------------------------------
# Mongo store — same CRUD contract, skipped unless a live URI is configured
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEWBO_TEST_MONGODB_URI"),
    reason="set MEWBO_TEST_MONGODB_URI to run the Mongo-backed store tests",
)
class TestMongoAgenticSearchStore:
    """Mirror the JSON CRUD contract against a real MongoDB."""

    @pytest.fixture()
    def store(self):
        """A Mongo store on the test database, cleaned before + after each test."""
        uri = os.environ["MEWBO_TEST_MONGODB_URI"]
        s = MongoAgenticSearchStore(uri=uri, database="mewbo_test_agentic")
        # Clean the collections so each test starts empty.
        s._col(s.WS).delete_many({})
        s._col(s.RUNS).delete_many({})
        s._col(s.EVENTS).delete_many({})
        yield s
        s._col(s.WS).delete_many({})
        s._col(s.RUNS).delete_many({})
        s._col(s.EVENTS).delete_many({})

    def test_mongo_workspace_crud(self, store):
        """Workspace create/get/update(partial preserves)/delete on Mongo."""
        ws = store.create_workspace(
            WorkspaceInput(name="QA", sources=["notion"], instructions="keep me")
        )
        assert store.get_workspace(ws.id).name == "QA"
        updated = store.update_workspace(ws.id, {"name": "QA2"})
        assert updated.name == "QA2"
        assert updated.instructions == "keep me"
        assert store.delete_workspace(ws.id) is True
        assert store.get_workspace(ws.id) is None

    def test_mongo_run_and_event_log(self, store):
        """Run CRUD + monotonic event-log idx/after_idx + cancel on Mongo."""
        store.create_run(_record("run-1", status="running"))
        assert store.get_run("run-1").workspace_id == "ws-1"
        i0 = store.append_run_event("run-1", {"type": "run_started"})
        i1 = store.append_run_event("run-1", {"type": "run_done"})
        assert [i0, i1] == [0, 1]
        assert [e["idx"] for e in store.load_run_events("run-1")] == [0, 1]
        assert [e["idx"] for e in store.load_run_events("run-1", after_idx=0)] == [1]
        assert store.cancel_run("run-1") is True
        assert store.cancel_run("run-1") is False

    def test_mongo_past_query_cap_and_patch(self, store):
        """append_past_query caps at PAST_QUERY_CAP; update patches by run_id."""
        ws = store.create_workspace(WorkspaceInput(name="QA"))
        for i in range(PAST_QUERY_CAP + 3):
            store.append_past_query(ws.id, PastQuery(q=f"q{i}", run_id=f"run-{i}"))
        history = store.get_workspace(ws.id).past_queries
        assert len(history) == PAST_QUERY_CAP
        # append_past_query prepends, so the newest run_id always survives the
        # cap — patch that one so the assertion is unconditional (run-0 is the
        # oldest and is deterministically evicted).
        survivor = f"run-{PAST_QUERY_CAP + 2}"
        store.update_past_query(ws.id, survivor, status="failed", results=3)
        match = next(
            pq for pq in store.get_workspace(ws.id).past_queries if pq.run_id == survivor
        )
        assert match.status == "failed"
        assert match.results == 3
