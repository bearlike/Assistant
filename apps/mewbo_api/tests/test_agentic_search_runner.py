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


def test_no_duplicate_result_events_in_log(store):
    """Each result id appears exactly once in the run event log (issue #82)."""
    run, ws = _seed_run(store, sources=["notion", "github", "drive", "linear", "filesystem"])
    EchoSearchRunner().start(run, ws, store=store)

    result_ids = [
        (e.get("result") or {}).get("id")
        for e in store.load_run_events(run.run_id)
        if e.get("type") == "result"
    ]
    assert result_ids, "echo runner must emit result events"
    assert len(result_ids) == len(set(result_ids)), (
        f"duplicate result ids reached the log: {result_ids}"
    )


def test_result_append_is_idempotent_by_id(store):
    """A re-appended result (same id) is a no-op — the dedup guard (issue #82).

    Simulates the real-world double-projection paths the issue names — an SSE
    replay+tail boundary, a re-drive, or a settle-time reconciliation re-emitting
    a result that already landed. The store guard must collapse it so the event
    log (which the SSE transport replays and the console reducer merges) stays
    duplicate-free by construction. A *different* id still appends.
    """
    from mewbo_api.agentic_search import events
    from mewbo_api.agentic_search.schemas import SearchResult

    run, _ = _seed_run(store, sources=["notion"])
    r = SearchResult(id="r1", source="notion", kind="docs", title="One")
    first_idx = store.append_run_event(run.run_id, events.result(item=r))
    # Re-append the SAME result id — must be a no-op returning the original idx.
    again_idx = store.append_run_event(run.run_id, events.result(item=r))
    assert again_idx == first_idx

    logged = [
        e for e in store.load_run_events(run.run_id) if e.get("type") == "result"
    ]
    assert len(logged) == 1, "the duplicate result must not be written a second time"

    # A genuinely different result still appends past the guard.
    r2 = SearchResult(id="r2", source="notion", kind="docs", title="Two")
    store.append_run_event(run.run_id, events.result(item=r2))
    ids = [
        (e.get("result") or {}).get("id")
        for e in store.load_run_events(run.run_id)
        if e.get("type") == "result"
    ]
    assert ids == ["r1", "r2"]


def test_non_result_events_are_not_deduped(store):
    """Non-result events (agent_line, answer_delta) still append every time.

    The guard is scoped to ``result`` events only — repeated trace/answer events
    are legitimately distinct emissions and must not be collapsed.
    """
    from mewbo_api.agentic_search import events
    from mewbo_api.agentic_search.schemas import TraceLine

    run, _ = _seed_run(store, sources=["notion"])
    line = TraceLine(t_ms=0, text="scanning")
    store.append_run_event(run.run_id, events.agent_line(agent_id="a1", line=line))
    store.append_run_event(run.run_id, events.agent_line(agent_id="a1", line=line))
    store.append_run_event(run.run_id, events.answer_delta(text="hello "))
    store.append_run_event(run.run_id, events.answer_delta(text="hello "))

    logged = store.load_run_events(run.run_id)
    assert sum(1 for e in logged if e.get("type") == "agent_line") == 2
    assert sum(1 for e in logged if e.get("type") == "answer_delta") == 2
