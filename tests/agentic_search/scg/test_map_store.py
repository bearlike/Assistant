"""Tests for the map-source (SCG indexing) job record in the agentic_search store.

Per spec #19 §16.2 the map job lives in the *agentic_search* store (NOT the SCG
structure store), reusing the run-event-log + ``RunSseGenerator`` plumbing. These
tests exercise the JSON backend under a tmp dir (no Mongo, no LLM, no runtime):

* create / get / update map job;
* ``MapJobProgress.emit_phase`` dual-writes (event log + snapshot phase);
* event-log idx stays monotonic + filters by ``after_idx``;
* ``store.reset_for_tests`` isolation (fresh store, no leaked map jobs).
"""

import pytest
from mewbo_api.agentic_search import store as store_mod
from mewbo_api.agentic_search.scg.map_progress import MapJobProgress
from mewbo_api.agentic_search.schemas import MapJobRecord
from mewbo_api.agentic_search.store import JsonAgenticSearchStore


def _job(job_id="map-1", source_id="github", source_type="mcp", **kw):
    return MapJobRecord(
        job_id=job_id,
        source_id=source_id,
        source_type=source_type,
        **kw,
    )


# ---------------------------------------------------------------------------
# Map-job CRUD
# ---------------------------------------------------------------------------


def test_create_get_map_job(tmp_path):
    """create_map_job persists; get returns it; missing id returns None."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    assert store.get_map_job("map-1") is None
    store.create_map_job(_job("map-1", "github", "mcp"))
    got = store.get_map_job("map-1")
    assert got is not None
    assert got.source_id == "github"
    assert got.source_type == "mcp"
    # Defaults from the wire model.
    assert got.status == "queued"
    assert got.phase is None
    assert got.node_count == 0
    assert got.edge_count == 0
    assert got.error is None


def test_update_map_job_partial_and_missing(tmp_path):
    """update_map_job patches given fields; unknown id raises KeyError."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1", status="mapping"))
    updated = store.update_map_job(
        "map-1", status="complete", node_count=12, edge_count=7
    )
    assert updated.status == "complete"
    assert updated.node_count == 12
    assert updated.edge_count == 7
    # Untouched field preserved.
    assert updated.source_id == "github"
    # Persisted, not just returned.
    assert store.get_map_job("map-1").node_count == 12
    with pytest.raises(KeyError):
        store.update_map_job("ghost", status="failed")


def test_update_map_job_error_is_redacted_dict(tmp_path):
    """error round-trips as a small {code, message} dict (never a secret)."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1", status="mapping"))
    err = {"code": "introspect_failed", "message": "OpenAPI doc unreachable"}
    updated = store.update_map_job("map-1", status="failed", error=err)
    assert updated.error == err
    assert store.get_map_job("map-1").error == err


def test_list_map_jobs_newest_first_and_filtered(tmp_path):
    """list_map_jobs returns newest-first; filters to source_id when given."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1", source_id="github", created_at="2026-01-01T00:00:00Z"))
    store.create_map_job(_job("map-2", source_id="notion", created_at="2026-02-01T00:00:00Z"))
    store.create_map_job(_job("map-3", source_id="github", created_at="2026-03-01T00:00:00Z"))

    all_ids = [j.job_id for j in store.list_map_jobs()]
    assert all_ids == ["map-3", "map-2", "map-1"]  # newest-first by created_at

    gh_ids = [j.job_id for j in store.list_map_jobs(source_id="github")]
    assert gh_ids == ["map-3", "map-1"]
    assert store.list_map_jobs(source_id="ghost") == []


# ---------------------------------------------------------------------------
# Map-job event log — monotonic idx + after_idx
# ---------------------------------------------------------------------------


def test_map_job_event_idx_monotonic_and_after_idx(tmp_path):
    """append_map_job_event yields a monotonic idx; load filters by after_idx."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1"))
    idxs = [
        store.append_map_job_event("map-1", {"type": "phase", "name": "connect"}),
        store.append_map_job_event("map-1", {"type": "phase", "name": "introspect"}),
        store.append_map_job_event("map-1", {"type": "phase", "name": "parse"}),
    ]
    assert idxs == [0, 1, 2]

    all_events = store.load_map_job_events("map-1")
    assert [e["idx"] for e in all_events] == [0, 1, 2]
    assert [e["name"] for e in all_events] == ["connect", "introspect", "parse"]

    # after_idx: only events strictly greater than the cursor.
    tail = store.load_map_job_events("map-1", after_idx=0)
    assert [e["idx"] for e in tail] == [1, 2]
    assert store.load_map_job_events("map-1", after_idx=2) == []
    # Unknown job -> empty event log.
    assert store.load_map_job_events("ghost") == []


def test_map_job_events_separate_from_run_events(tmp_path):
    """Map-job + run event logs are independent (idx counters don't collide)."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1"))
    # A run with the same trailing id must not share the map-job event log.
    from mewbo_api.agentic_search.schemas import RunRecord

    store.create_run(
        RunRecord(run_id="run-1", session_id="sess-1", workspace_id="ws-1", query="q")
    )
    store.append_run_event("run-1", {"type": "run_started"})
    store.append_map_job_event("map-1", {"type": "phase", "name": "connect"})

    assert [e["type"] for e in store.load_run_events("run-1")] == ["run_started"]
    assert [e["name"] for e in store.load_map_job_events("map-1")] == ["connect"]
    # Both start at idx 0 — separate monotonic counters.
    assert store.load_run_events("run-1")[0]["idx"] == 0
    assert store.load_map_job_events("map-1")[0]["idx"] == 0


# ---------------------------------------------------------------------------
# MapJobProgress.emit_phase — the dual write
# ---------------------------------------------------------------------------


def test_emit_phase_dual_writes_event_and_snapshot(tmp_path):
    """emit_phase appends a phase event AND patches phase + phase_started_at."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1", status="mapping"))

    idx = MapJobProgress.emit_phase(store, "map-1", "introspect")
    assert idx == 0

    # 1) Snapshot side: phase + phase_started_at patched on the record.
    job = store.get_map_job("map-1")
    assert job.phase == "introspect"
    assert job.phase_started_at  # ISO ts set
    # Status (the coarse bucket) is untouched by a phase emit.
    assert job.status == "mapping"

    # 2) Event-log side: a single phase event appended.
    events = store.load_map_job_events("map-1")
    assert len(events) == 1
    assert events[0]["type"] == "phase"
    assert events[0]["name"] == "introspect"
    assert events[0]["idx"] == 0


def test_emit_phase_sequence_keeps_idx_monotonic(tmp_path):
    """Repeated emit_phase keeps event idx monotonic + snapshot tracks latest."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    store.create_map_job(_job("map-1", status="mapping"))

    phases = ["connect", "introspect", "parse", "link", "finalize"]
    idxs = [MapJobProgress.emit_phase(store, "map-1", p) for p in phases]
    assert idxs == [0, 1, 2, 3, 4]

    events = store.load_map_job_events("map-1")
    assert [e["name"] for e in events] == phases
    assert [e["idx"] for e in events] == [0, 1, 2, 3, 4]
    # Snapshot reflects the final phase only.
    assert store.get_map_job("map-1").phase == "finalize"


# ---------------------------------------------------------------------------
# reset_for_tests isolation
# ---------------------------------------------------------------------------


def test_reset_for_tests_isolates_map_jobs():
    """reset_for_tests swaps a fresh store — map jobs do not leak across resets."""
    store_mod.reset_for_tests()
    store_a = store_mod.get_store()
    store_a.create_map_job(_job("map-1"))
    assert store_a.get_map_job("map-1") is not None

    # A fresh reset must not see the previous store's map job.
    store_mod.reset_for_tests()
    store_b = store_mod.get_store()
    assert store_b is not store_a
    assert store_b.get_map_job("map-1") is None
