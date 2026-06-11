"""Extra coverage for agentic_search/store.py — map-job CRUD, seeding gates,
malformed-file resilience, list edge cases, and the singleton lifecycle.

The existing test_agentic_search_store.py covers basic workspace + run CRUD.
This file covers the gaps:

- ``JsonAgenticSearchStore`` map-job family (create/get/update/list/events).
- Malformed JSON resilience in ``_load_ws`` and ``get_run``.
- ``list_runs`` + ``list_map_jobs`` with a file inside the directory tree.
- ``seed_workspaces_if_empty`` gate (seeding disabled / already populated).
- ``seeding_enabled`` env-var gate.
- Singleton lifecycle: ``set_store`` / ``get_store`` re-use path.
- ``_append_jsonl_event`` idx when path doesn't exist yet (first write).
- ``_load_jsonl_events`` malformed-line resilience.
"""

# mypy: ignore-errors

import json
import os
from pathlib import Path

import pytest
from mewbo_api.agentic_search.schemas import (
    MapJobRecord,
    RunRecord,
    Workspace,
    WorkspaceInput,
)
from mewbo_api.agentic_search.store import (
    JsonAgenticSearchStore,
    get_store,
    reset_for_tests,
    seed_workspaces,
    seed_workspaces_if_empty,
    seeding_enabled,
    set_store,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp: Path) -> JsonAgenticSearchStore:
    return JsonAgenticSearchStore(root_dir=tmp)


def _run_record(run_id: str = "run-1", ws_id: str = "ws-1", **kw) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        session_id=f"sess-{run_id}",
        workspace_id=ws_id,
        query="hello",
        **kw,
    )


def _job_record(
    job_id: str = "job-1", source_id: str = "notion", source_type: str = "notion"
) -> MapJobRecord:
    return MapJobRecord(job_id=job_id, source_id=source_id, source_type=source_type)


# ---------------------------------------------------------------------------
# Map-job CRUD — create / get / update / list / events
# ---------------------------------------------------------------------------


def test_json_map_job_create_and_get(tmp_path):
    """create_map_job persists; get_map_job retrieves it."""
    store = _store(tmp_path)
    job = _job_record()
    store.create_map_job(job)
    fetched = store.get_map_job("job-1")
    assert fetched is not None
    assert fetched.job_id == "job-1"
    assert fetched.source_id == "notion"
    assert fetched.status == "queued"


def test_json_map_job_get_missing_returns_none(tmp_path):
    """get_map_job on an absent id returns None."""
    store = _store(tmp_path)
    assert store.get_map_job("ghost") is None


def test_json_map_job_update(tmp_path):
    """update_map_job patches fields and returns the updated record."""
    store = _store(tmp_path)
    store.create_map_job(_job_record())
    updated = store.update_map_job("job-1", status="running", node_count=5)
    assert updated.status == "running"
    assert updated.node_count == 5
    # Verify on disk.
    again = store.get_map_job("job-1")
    assert again is not None
    assert again.node_count == 5


def test_json_map_job_update_missing_raises(tmp_path):
    """update_map_job on an absent job raises KeyError."""
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        store.update_map_job("ghost", status="failed")


def test_json_map_job_list_all(tmp_path):
    """list_map_jobs returns all jobs newest-first when source_id is None."""
    store = _store(tmp_path)
    store.create_map_job(_job_record("job-a", source_id="notion"))
    store.create_map_job(_job_record("job-b", source_id="github"))
    jobs = store.list_map_jobs()
    assert {j.job_id for j in jobs} == {"job-a", "job-b"}


def test_json_map_job_list_filtered_by_source(tmp_path):
    """list_map_jobs(source_id=...) filters to that source only."""
    store = _store(tmp_path)
    store.create_map_job(_job_record("job-notion", source_id="notion"))
    store.create_map_job(_job_record("job-github", source_id="github"))
    notion_jobs = store.list_map_jobs(source_id="notion")
    assert [j.job_id for j in notion_jobs] == ["job-notion"]


def test_json_map_job_list_empty_when_dir_absent(tmp_path):
    """list_map_jobs returns [] when the map_jobs directory doesn't exist yet."""
    store = _store(tmp_path)
    # map_jobs/ is not created until the first create_map_job call.
    assert store.list_map_jobs() == []


def test_json_map_job_event_log_monotonic_idx(tmp_path):
    """append_map_job_event yields a monotonic idx; load filters by after_idx."""
    store = _store(tmp_path)
    store.create_map_job(_job_record())
    i0 = store.append_map_job_event("job-1", {"type": "phase_started", "phase": "connect"})
    i1 = store.append_map_job_event("job-1", {"type": "phase_started", "phase": "introspect"})
    i2 = store.append_map_job_event("job-1", {"type": "run_done"})
    assert [i0, i1, i2] == [0, 1, 2]

    all_events = store.load_map_job_events("job-1")
    assert [e["idx"] for e in all_events] == [0, 1, 2]

    tail = store.load_map_job_events("job-1", after_idx=0)
    assert [e["idx"] for e in tail] == [1, 2]

    assert store.load_map_job_events("job-1", after_idx=2) == []


def test_json_map_job_events_unknown_job_returns_empty(tmp_path):
    """load_map_job_events on an absent job returns []."""
    store = _store(tmp_path)
    assert store.load_map_job_events("ghost") == []


# ---------------------------------------------------------------------------
# Malformed-file resilience
# ---------------------------------------------------------------------------


def test_json_malformed_workspace_skipped_in_list(tmp_path):
    """A corrupted workspace JSON is silently skipped by list_workspaces."""
    store = _store(tmp_path)
    # Write a valid workspace.
    ws = store.create_workspace(WorkspaceInput(name="Good"))
    # Corrupt a second workspace file.
    bad_path = store.root_dir / "workspaces" / "ws-bad.json"
    bad_path.write_text("{not-valid-json", encoding="utf-8")
    listed = store.list_workspaces()
    # Only the good one survives.
    assert len(listed) == 1
    assert listed[0].id == ws.id


def test_json_malformed_workspace_get_returns_none(tmp_path):
    """get_workspace returns None for a malformed JSON file."""
    store = _store(tmp_path)
    bad_path = store.root_dir / "workspaces" / "ws-broken.json"
    bad_path.write_text("{not-valid-json", encoding="utf-8")
    assert store.get_workspace("ws-broken") is None


def test_json_malformed_run_skipped_in_list(tmp_path):
    """A corrupted run JSON is silently skipped by list_runs."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-good"))
    # Write a bad run manually.
    bad_dir = store.root_dir / "runs" / "run-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "run.json").write_text("{broken", encoding="utf-8")
    runs = store.list_runs()
    ids = {r.run_id for r in runs}
    assert "run-good" in ids
    assert "run-bad" not in ids


def test_json_malformed_run_get_returns_none(tmp_path):
    """get_run returns None for a malformed JSON file."""
    store = _store(tmp_path)
    bad_dir = store.root_dir / "runs" / "run-broken"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "run.json").write_text("{broken", encoding="utf-8")
    assert store.get_run("run-broken") is None


def test_json_malformed_map_job_get_returns_none(tmp_path):
    """get_map_job returns None for a malformed JSON file."""
    store = _store(tmp_path)
    bad_dir = store.root_dir / "map_jobs" / "job-broken"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "job.json").write_text("{broken", encoding="utf-8")
    assert store.get_map_job("job-broken") is None


def test_json_malformed_map_job_skipped_in_list(tmp_path):
    """A corrupted map-job JSON is silently skipped by list_map_jobs."""
    store = _store(tmp_path)
    store.create_map_job(_job_record("job-good"))
    bad_dir = store.root_dir / "map_jobs" / "job-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "job.json").write_text("{broken", encoding="utf-8")
    jobs = store.list_map_jobs()
    ids = {j.job_id for j in jobs}
    assert "job-good" in ids
    assert "job-bad" not in ids


def test_json_malformed_event_line_skipped(tmp_path):
    """A malformed JSONL line in an event log is silently skipped."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-1"))
    # Write a valid event then corrupt the next line manually.
    events_path = store._events_path("run-1")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps({"type": "run_started", "idx": 0})
        + "\n"
        + "NOTJSON\n"
        + json.dumps({"type": "run_done", "idx": 1})
        + "\n",
        encoding="utf-8",
    )
    loaded = store.load_run_events("run-1")
    assert [e["idx"] for e in loaded] == [0, 1]
    types = [e["type"] for e in loaded]
    assert "run_started" in types
    assert "run_done" in types


def test_json_blank_line_in_event_log_skipped(tmp_path):
    """Blank lines in a JSONL event file are silently skipped."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-blank"))
    events_path = store._events_path("run-blank")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps({"type": "run_started", "idx": 0})
        + "\n"
        + "\n"
        + "   \n"
        + json.dumps({"type": "run_done", "idx": 1})
        + "\n",
        encoding="utf-8",
    )
    loaded = store.load_run_events("run-blank")
    assert [e["idx"] for e in loaded] == [0, 1]


# ---------------------------------------------------------------------------
# list_runs edge cases
# ---------------------------------------------------------------------------


def test_json_list_runs_ignores_non_dir_entries(tmp_path):
    """list_runs skips stray non-directory entries under runs/."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-1"))
    # Place a plain file inside runs/ that is not a directory.
    (store.root_dir / "runs" / "stray.txt").write_text("noise", encoding="utf-8")
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == "run-1"


def test_json_list_map_jobs_ignores_non_dir_entries(tmp_path):
    """list_map_jobs skips stray non-directory entries under map_jobs/."""
    store = _store(tmp_path)
    store.create_map_job(_job_record("job-1"))
    (store.root_dir / "map_jobs" / "stray.txt").write_text("noise", encoding="utf-8")
    jobs = store.list_map_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_id == "job-1"


# ---------------------------------------------------------------------------
# Seeding gates
# ---------------------------------------------------------------------------


def test_seeding_enabled_default_on():
    """seeding_enabled() returns True by default (env var not set)."""
    env = os.environ.copy()
    env.pop("MEWBO_AGENTIC_SEARCH_SEED", None)
    # Temporarily clear the env var without mutating global state permanently.
    old = os.environ.pop("MEWBO_AGENTIC_SEARCH_SEED", None)
    try:
        assert seeding_enabled() is True
    finally:
        if old is not None:
            os.environ["MEWBO_AGENTIC_SEARCH_SEED"] = old


def test_seeding_enabled_off_when_env_set_to_zero(monkeypatch):
    """seeding_enabled() returns False when MEWBO_AGENTIC_SEARCH_SEED=0."""
    monkeypatch.setenv("MEWBO_AGENTIC_SEARCH_SEED", "0")
    assert seeding_enabled() is False


def test_seed_workspaces_if_empty_skips_when_already_populated(tmp_path):
    """seed_workspaces_if_empty does nothing if the store has existing workspaces."""
    store = _store(tmp_path)
    # Pre-populate with a workspace.
    store.create_workspace(WorkspaceInput(name="Existing"))
    before = len(store.list_workspaces())
    seed_workspaces_if_empty(store)
    # Should NOT have added the demo workspaces on top.
    assert len(store.list_workspaces()) == before


def test_seed_workspaces_if_empty_skips_when_seeding_disabled(tmp_path, monkeypatch):
    """seed_workspaces_if_empty does nothing when MEWBO_AGENTIC_SEARCH_SEED=0."""
    monkeypatch.setenv("MEWBO_AGENTIC_SEARCH_SEED", "0")
    store = _store(tmp_path)
    seed_workspaces_if_empty(store)
    assert store.list_workspaces() == []


def test_seed_workspaces_if_empty_seeds_empty_store(tmp_path):
    """seed_workspaces_if_empty populates demo workspaces into an empty store."""
    store = _store(tmp_path)
    assert store.list_workspaces() == []
    seed_workspaces_if_empty(store)
    ws_ids = {w.id for w in store.list_workspaces()}
    assert {"eng-docs", "product", "research"} <= ws_ids


def test_seed_workspaces_builds_validated_models():
    """seed_workspaces() returns Workspace instances with valid ids + timestamps."""
    workspaces = seed_workspaces()
    assert len(workspaces) >= 3
    for ws in workspaces:
        assert isinstance(ws, Workspace)
        assert ws.id
        assert ws.name


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


def test_get_store_returns_same_singleton(tmp_path):
    """get_store() returns the same object on successive calls."""
    reset_for_tests()
    s1 = get_store()
    s2 = get_store()
    assert s1 is s2


def test_set_store_replaces_singleton(tmp_path):
    """set_store() replaces the singleton; get_store() sees the new one."""
    fresh = JsonAgenticSearchStore(root_dir=tmp_path)
    set_store(fresh)
    assert get_store() is fresh
    # Restore a clean seeded store so other tests remain isolated.
    reset_for_tests()


def test_reset_for_tests_seeds_demo_workspaces():
    """reset_for_tests() installs a fresh store with the demo workspaces present."""
    reset_for_tests()
    store = get_store()
    ids = {w.id for w in store.list_workspaces()}
    assert {"eng-docs", "product", "research"} <= ids


# ---------------------------------------------------------------------------
# _append_jsonl_event — first write (path does not exist) creates the file
# ---------------------------------------------------------------------------


def test_append_jsonl_event_first_write_starts_at_idx_zero(tmp_path):
    """The first append_run_event for a run yields idx 0 (no prior events)."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-fresh"))
    idx = store.append_run_event("run-fresh", {"type": "run_started"})
    assert idx == 0
    events = store.load_run_events("run-fresh")
    assert len(events) == 1
    assert events[0]["idx"] == 0
    assert events[0]["type"] == "run_started"


def test_json_store_default_root_dir(monkeypatch, tmp_path):
    """JsonAgenticSearchStore with root_dir=None resolves from config."""
    # Patch get_config_value to return our tmp_path so we don't litter ~/.mewbo.
    import mewbo_api.agentic_search.store as store_module

    monkeypatch.setattr(
        store_module,
        "get_config_value",
        lambda *args, **kw: str(tmp_path),
    )
    store = JsonAgenticSearchStore(root_dir=None)
    # Should have created subdirectories under tmp_path/agentic_search.
    assert (store.root_dir / "workspaces").exists()
    assert (store.root_dir / "runs").exists()


def test_json_list_runs_returns_empty_when_runs_dir_deleted(tmp_path):
    """list_runs returns [] gracefully when the runs directory has been removed."""
    store = _store(tmp_path)
    # Manually remove the runs directory to exercise the early-return branch.
    import shutil

    shutil.rmtree(store.root_dir / "runs")
    assert store.list_runs() == []


# ---------------------------------------------------------------------------
# update_past_query — missing workspace is a no-op (does not raise)
# ---------------------------------------------------------------------------


def test_update_past_query_missing_workspace_is_noop(tmp_path):
    """update_past_query on a non-existent workspace does not raise."""
    store = _store(tmp_path)
    store.update_past_query("ghost-ws", "run-1", status="completed", results=0)
    # No exception — the workspace simply didn't exist.


# ---------------------------------------------------------------------------
# cancel_run on a running run — shared base-class logic
# ---------------------------------------------------------------------------


def test_json_cancel_running_run_sets_status_and_appends_event(tmp_path):
    """cancel_run on a running run sets status=cancelled and appends the event."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-1", status="running"))
    result = store.cancel_run("run-1")
    assert result is True
    assert store.get_run("run-1").status == "cancelled"
    events = store.load_run_events("run-1")
    assert any(e.get("type") == "cancelled" for e in events)


def test_json_cancel_already_terminal_returns_false(tmp_path):
    """cancel_run on a completed run returns False (already terminal)."""
    store = _store(tmp_path)
    store.create_run(_run_record("run-1", status="completed"))
    assert store.cancel_run("run-1") is False


def test_json_cancel_missing_run_returns_false(tmp_path):
    """cancel_run on an absent run_id returns False."""
    store = _store(tmp_path)
    assert store.cancel_run("ghost") is False
