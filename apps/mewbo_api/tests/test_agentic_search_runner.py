"""Tests for the EchoSearchRunner over a real JSON store."""

# mypy: ignore-errors

import pytest
from mewbo_api.agentic_search import fixtures
from mewbo_api.agentic_search.runner import EchoSearchRunner
from mewbo_api.agentic_search.schemas import RunRecord, Workspace
from mewbo_api.agentic_search.store import JsonAgenticSearchStore


@pytest.fixture()
def store(tmp_path):
    """A real JSON store under a throwaway temp dir."""
    return JsonAgenticSearchStore(root_dir=tmp_path)


def _seed_run(store, *, sources, run_id="run-1", workspace_id="ws-1", query="q"):
    """Create a workspace + a running RunRecord; return (run, workspace)."""
    workspace = Workspace(id=workspace_id, name="WS", sources=sources)
    store.save_workspace(workspace)
    run = RunRecord(
        run_id=run_id,
        session_id=f"agentic_search:run:{run_id}",
        workspace_id=workspace_id,
        query=query,
        status="running",
        source_ids=list(sources),
    )
    store.create_run(run)
    return run, workspace


def test_results_and_trace_restricted_to_enabled_sources(store):
    """Results + trace only reference the workspace's enabled sources."""
    enabled = ["web", "drive", "notion"]
    run, ws = _seed_run(store, sources=enabled)
    payload = EchoSearchRunner().start(run, ws, store=store)

    enabled_set = set(enabled)
    assert {r.source for r in payload.results} <= enabled_set
    assert {a.source_id for a in payload.trace} <= enabled_set
    # And the filter actually dropped something: the demo fixtures span more
    # sources than the workspace enables, so the visible set is a strict subset.
    assert payload.results
    assert payload.trace
    assert {r.source for r in payload.results} < {r["source"] for r in fixtures.DEMO_RESULTS}


def test_answer_bullet_cites_are_subset_of_visible_results(store):
    """Every cited id on an answer bullet refers to a visible result."""
    run, ws = _seed_run(store, sources=["web", "drive", "notion"])
    payload = EchoSearchRunner().start(run, ws, store=store)

    visible_ids = {r.id for r in payload.results}
    for bullet in payload.answer.bullets:
        for cite in bullet.cites:
            assert cite in visible_ids


def test_answer_sources_count_matches_results(store):
    """answer.sources_count equals the number of visible results."""
    run, ws = _seed_run(store, sources=["web", "drive", "notion"])
    payload = EchoSearchRunner().start(run, ws, store=store)
    assert payload.answer.sources_count == len(payload.results)


def test_event_log_brackets_run_started_and_run_done(store):
    """The appended event log begins with run_started and ends with run_done."""
    run, ws = _seed_run(store, sources=["web", "drive", "notion"])
    EchoSearchRunner().start(run, ws, store=store)

    events = store.load_run_events(run.run_id)
    assert events, "echo runner must append events"
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_done"
    assert events[-1]["status"] == "completed"
    # idx is monotonic across the whole sequence.
    assert [e["idx"] for e in events] == list(range(len(events)))


def test_answer_delta_chunks_reconstruct_tldr(store):
    """Concatenating answer_delta text chunks rebuilds the answer's tldr."""
    run, ws = _seed_run(store, sources=["web", "drive", "notion"])
    payload = EchoSearchRunner().start(run, ws, store=store)

    deltas = [
        e["text"]
        for e in store.load_run_events(run.run_id)
        if e.get("type") == "answer_delta"
    ]
    assert deltas, "echo runner streams the synthesis as answer_delta chunks"
    assert "".join(deltas) == payload.answer.tldr


def test_record_persisted_completed_with_payload(store):
    """The runner persists status=completed and a non-null payload on the record."""
    run, ws = _seed_run(store, sources=["web", "drive", "notion"])
    EchoSearchRunner().start(run, ws, store=store)

    persisted = store.get_run(run.run_id)
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.completed_at is not None
    assert persisted.payload is not None
    assert persisted.payload.run_id == run.run_id
    assert persisted.payload.status == "completed"
    assert persisted.total_ms > 0
