"""Tests for WikiQaSession, WikiQaSseGenerator, and the QA routes."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mewbo_api.wiki.events import WikiQaSseGenerator
from mewbo_api.wiki.jobs import WikiQaSession
from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import QaAnswer

API_KEY = "test-key-123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture
def runtime(store: JsonWikiStore) -> MagicMock:
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-qa-abc"
    rt.start_async.return_value = True
    rt.cancel.return_value = True
    return rt


@pytest.fixture(autouse=True)
def _fast_sse(monkeypatch):
    """Drain SSE quickly in route tests — overrides via env vars.

    Without this, ``WikiQaSseGenerator`` (default 600 × 0.5s) blocks ~5 min
    waiting for a terminal event when the runtime is mocked.
    """
    monkeypatch.setenv("MEWBO_WIKI_SSE_MAX_IDLE", "2")
    monkeypatch.setenv("MEWBO_WIKI_SSE_SLEEP", "0")


@pytest.fixture
def wiki_app(tmp_path: Path, monkeypatch, store, runtime):
    """Flask test app with wiki routes mounted and a temp JsonWikiStore."""
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register(flask_app, runtime)

    yield flask_app, store

    routes_mod._runtime = None


@pytest.fixture
def client(wiki_app):
    flask_app, store = wiki_app
    return flask_app.test_client(), store


def _valid_qa_body(**overrides) -> dict:
    body = {
        "question": "How does authentication work?",
        "fromPageId": "auth-overview",
        "model": "anthropic/claude-sonnet-4-6",
        "slug": "org/repo",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Unit: WikiQaSession.start
# ---------------------------------------------------------------------------


def test_qa_start_creates_record_and_emits_meta_event(store, runtime):
    """start() saves QaAnswer and immediately appends the meta event."""
    answer = WikiQaSession.start(
        slug="org/repo",
        question="What is the auth flow?",
        from_page_id="auth-overview",
        model="anthropic/claude-sonnet-4-6",
        runtime=runtime,
    )
    assert answer.answer_id
    # Record saved
    persisted = store.get_qa(answer.answer_id)
    assert persisted is not None
    assert persisted.model == "anthropic/claude-sonnet-4-6"
    # meta event is the first (and at this point only) event
    events = store.load_qa_events(answer.answer_id)
    assert len(events) >= 1
    first = events[0]
    assert first["type"] == "meta"
    assert first["answerId"] == answer.answer_id
    assert first["model"] == "anthropic/claude-sonnet-4-6"
    assert first["fromPageId"] == "auth-overview"
    # session wiring
    runtime.resolve_session.assert_called_once()
    tag = runtime.resolve_session.call_args.kwargs["session_tag"]
    assert tag == f"wiki:qa:{answer.answer_id}"
    runtime.start_async.assert_called_once()
    kw = runtime.start_async.call_args.kwargs
    assert "wiki_search_pages" in kw["allowed_tools"]
    assert "wiki_emit_block" in kw["allowed_tools"]
    assert kw["model_name"] == "anthropic/claude-sonnet-4-6"
    assert kw["user_query"] == "What is the auth flow?"


def test_qa_start_playbook_contains_agent_instructions(store, runtime):
    """skill_instructions comes from the wiki-qa.md body."""
    WikiQaSession.start(
        slug="org/repo",
        question="What?",
        from_page_id="",
        model="anthropic/claude-sonnet-4-6",
        runtime=runtime,
    )
    kw = runtime.start_async.call_args.kwargs
    # wiki-qa.md body must reference the retrieval tools
    assert "wiki_search_pages" in kw["skill_instructions"]
    assert "wiki_emit_block" in kw["skill_instructions"]


def test_qa_meta_appears_before_first_tool_call(store, runtime):
    """meta is the FIRST event in the log — emitted synchronously before start_async."""
    answer = WikiQaSession.start(
        slug="org/repo",
        question="Q",
        from_page_id="",
        model="anthropic/claude-sonnet-4-6",
        runtime=runtime,
    )
    events = store.load_qa_events(answer.answer_id)
    # The meta event must be at index 0 regardless of what start_async does
    assert events[0]["type"] == "meta"


# ---------------------------------------------------------------------------
# Unit: WikiQaSession.cancel
# ---------------------------------------------------------------------------


def test_qa_cancel_appends_cancelled_event(store, runtime):
    """cancel() appends a cancelled event and calls runtime.cancel."""
    answer = WikiQaSession.start(
        slug="org/repo",
        question="Q",
        from_page_id="",
        model="anthropic/claude-sonnet-4-6",
        runtime=runtime,
    )
    result = WikiQaSession.cancel(answer.answer_id, runtime=runtime)
    assert result is True
    events = store.load_qa_events(answer.answer_id)
    assert any(e["type"] == "cancelled" for e in events)
    runtime.cancel.assert_called_once_with("sess-qa-abc")


def test_qa_cancel_idempotent(store, runtime):
    """Calling cancel twice returns False on the second call."""
    answer = WikiQaSession.start(
        slug="org/repo",
        question="Q",
        from_page_id="",
        model="anthropic/claude-sonnet-4-6",
        runtime=runtime,
    )
    WikiQaSession.cancel(answer.answer_id, runtime=runtime)
    second = WikiQaSession.cancel(answer.answer_id, runtime=runtime)
    assert second is False
    # Only one cancelled event in the log
    cancelled = [e for e in store.load_qa_events(answer.answer_id) if e["type"] == "cancelled"]
    assert len(cancelled) == 1


def test_qa_cancel_unknown_answer_is_noop(store, runtime):
    """cancel() on a non-existent answer_id — no crash, returns False."""
    # No QaAnswer in store — cancel must not raise
    store.save_qa(QaAnswer(
        answerId="ghost-id",
        fromPageId="",
        summarySources=[],
        model="m",
        blocks=[],
    ))
    # Append a cancelled event first so idempotency kicks in
    store.append_qa_event("ghost-id", {"type": "cancelled"})
    result = WikiQaSession.cancel("ghost-id", runtime=runtime)
    assert result is False


# ---------------------------------------------------------------------------
# Unit: WikiQaSseGenerator
# ---------------------------------------------------------------------------


def _seed_qa_with_events(store, answer_id: str = "ans-sse-001") -> None:
    """Helper: persist a QaAnswer + a handful of events."""
    store.save_qa(QaAnswer(
        answerId=answer_id,
        fromPageId="overview",
        summarySources=[],
        model="m",
        blocks=[],
    ))
    store.append_qa_event(answer_id, {
        "type": "meta", "answerId": answer_id, "model": "m", "fromPageId": "",
    })
    store.append_qa_event(answer_id, {"type": "summary_ready", "sources": ["p1"]})
    store.append_qa_event(answer_id, {
        "type": "block_open", "index": 0, "block": {"kind": "p", "text": "Hello"},
    })
    store.append_qa_event(answer_id, {"type": "block_close", "index": 0})
    store.append_qa_event(answer_id, {"type": "complete", "totalBlocks": 1})


def test_sse_generator_yields_all_events(store):
    """Generator replays all seeded events including terminal and then stops."""
    _seed_qa_with_events(store)
    gen = WikiQaSseGenerator(store=store, answer_id="ans-sse-001", max_idle_cycles=2, sleep_s=0)
    frames = list(gen.generate())
    types_seen = []
    for frame in frames:
        # Each frame: "event: <type>\ndata: {...}\n\n"
        for line in frame.splitlines():
            if line.startswith("event: "):
                types_seen.append(line[len("event: "):])
    assert "meta" in types_seen
    assert "summary_ready" in types_seen
    assert "block_open" in types_seen
    assert "block_close" in types_seen
    assert "complete" in types_seen


def test_sse_generator_after_idx_filters(store):
    """after_idx=-1 returns all events; after_idx=2 skips the first 3."""
    _seed_qa_with_events(store)
    gen_all = WikiQaSseGenerator(
        store=store, answer_id="ans-sse-001", after_idx=-1, max_idle_cycles=2, sleep_s=0
    )
    frames_all = list(gen_all.generate())

    gen_partial = WikiQaSseGenerator(
        store=store, answer_id="ans-sse-001", after_idx=2, max_idle_cycles=2, sleep_s=0
    )
    frames_partial = list(gen_partial.generate())

    assert len(frames_partial) < len(frames_all)


def test_sse_generator_terminates_on_cancelled(store):
    """Generator stops after seeing a cancelled event."""
    answer_id = "ans-cancel-sse"
    store.save_qa(QaAnswer(
        answerId=answer_id, fromPageId="", summarySources=[], model="m", blocks=[],
    ))
    store.append_qa_event(answer_id, {
        "type": "meta", "answerId": answer_id, "model": "m", "fromPageId": "",
    })
    store.append_qa_event(answer_id, {"type": "cancelled"})
    gen = WikiQaSseGenerator(store=store, answer_id=answer_id, max_idle_cycles=2, sleep_s=0)
    frames = list(gen.generate())
    types_seen = [
        line[len("event: "):]
        for f in frames
        for line in f.splitlines()
        if line.startswith("event: ")
    ]
    assert "cancelled" in types_seen
    # No heartbeat frames expected for such a short session
    assert "heartbeat" not in types_seen


def test_sse_generator_idles_out(store):
    """Generator breaks after max_idle_cycles when no terminal event arrives."""
    answer_id = "ans-idle"
    store.save_qa(QaAnswer(
        answerId=answer_id, fromPageId="", summarySources=[], model="m", blocks=[],
    ))
    # No events at all — generator will idle out
    gen = WikiQaSseGenerator(store=store, answer_id=answer_id, max_idle_cycles=3, sleep_s=0)
    frames = list(gen.generate())
    # Should yield no SSE data frames (possibly a heartbeat or two)
    data_frames = [f for f in frames if f.startswith("event: ") and "heartbeat" not in f]
    assert data_frames == []


# ---------------------------------------------------------------------------
# Route: POST /v1/wiki/qa
# ---------------------------------------------------------------------------


def test_post_qa_returns_200_text_event_stream(client):
    """POST /v1/wiki/qa with valid body → 200, content-type text/event-stream."""
    c, store = client
    resp = c.post(
        "/v1/wiki/qa",
        json=_valid_qa_body(),
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type


def test_post_qa_response_starts_with_meta_event(client):
    """First SSE frame from POST /v1/wiki/qa is the meta event."""
    c, store = client
    resp = c.post(
        "/v1/wiki/qa",
        json=_valid_qa_body(),
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    # Consume all response data (buffered=True in test client)
    raw = resp.data.decode()
    # Find the first event: line "event: meta"
    assert "event: meta" in raw
    # meta must appear before any block_open
    meta_pos = raw.find("event: meta")
    block_pos = raw.find("event: block_open")
    # Either no block events, or meta comes first
    assert block_pos == -1 or meta_pos < block_pos


def test_post_qa_validates_missing_model(client):
    """Missing model → 400 validation error."""
    c, _ = client
    resp = c.post(
        "/v1/wiki/qa",
        json={"question": "What?", "slug": "org/repo"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["code"] == "validation"
    assert "model" in data.get("fields", {})


def test_post_qa_validates_missing_question(client):
    """Missing question → 400 validation error."""
    c, _ = client
    resp = c.post(
        "/v1/wiki/qa",
        json={"model": "m", "slug": "org/repo"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["code"] == "validation"
    assert "question" in data.get("fields", {})


def test_post_qa_validates_missing_slug(client):
    """Missing slug → 400 validation error."""
    c, _ = client
    resp = c.post(
        "/v1/wiki/qa",
        json={"question": "Q", "model": "m"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["code"] == "validation"
    assert "slug" in data.get("fields", {})


def test_post_qa_requires_auth(client):
    """POST /v1/wiki/qa without auth → 401."""
    c, _ = client
    resp = c.post("/v1/wiki/qa", json=_valid_qa_body())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Route: DELETE /v1/wiki/qa/<id>
# ---------------------------------------------------------------------------


def _seed_qa(store, answer_id: str = "ans-001") -> QaAnswer:
    ans = QaAnswer(
        answerId=answer_id,
        fromPageId="overview",
        summarySources=["src/main.py"],
        model="m",
        blocks=[],
    )
    store.save_qa(ans)
    return ans


def test_delete_qa_appends_cancelled(client):
    """DELETE /v1/wiki/qa/<id> → 200 with QaAnswer body."""
    c, store = client
    _seed_qa(store, "ans-del-001")
    resp = c.delete("/v1/wiki/qa/ans-del-001", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["answerId"] == "ans-del-001"
    events = store.load_qa_events("ans-del-001")
    assert any(e["type"] == "cancelled" for e in events)


def test_delete_qa_idempotent(client):
    """DELETE twice → both 200; no duplicate cancelled events."""
    c, store = client
    _seed_qa(store, "ans-del-002")
    r1 = c.delete("/v1/wiki/qa/ans-del-002", headers={"X-Api-Key": API_KEY})
    r2 = c.delete("/v1/wiki/qa/ans-del-002", headers={"X-Api-Key": API_KEY})
    assert r1.status_code == 200
    assert r2.status_code == 200
    cancelled = [e for e in store.load_qa_events("ans-del-002") if e["type"] == "cancelled"]
    assert len(cancelled) == 1


def test_delete_qa_not_found(client):
    """DELETE unknown answer_id → 404."""
    c, _ = client
    resp = c.delete("/v1/wiki/qa/no-such-ans", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Route: POST /v1/wiki/qa/<id>/stream
# ---------------------------------------------------------------------------


def test_stream_qa_replays_from_start(client):
    """POST /v1/wiki/qa/<id>/stream replays all events from idx=0."""
    c, store = client
    _seed_qa_with_events(store, "ans-stream-001")
    resp = c.post("/v1/wiki/qa/ans-stream-001/stream", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    raw = resp.data.decode()
    assert "event: meta" in raw
    assert "event: complete" in raw


def test_stream_qa_not_found(client):
    """POST /v1/wiki/qa/missing/stream → 404."""
    c, _ = client
    resp = c.post("/v1/wiki/qa/no-such/stream", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


def test_stream_qa_requires_auth(client):
    c, store = client
    _seed_qa_with_events(store, "ans-auth-stream")
    resp = c.post("/v1/wiki/qa/ans-auth-stream/stream")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Route: GET /v1/wiki/qa/<id> (snapshot — Task 1.5 verif)
# ---------------------------------------------------------------------------


def test_get_qa_snapshot_returns_answer(client):
    """Existing GET snapshot route returns the QaAnswer with camelCase keys."""
    c, store = client
    _seed_qa(store, "ans-snap-001")
    resp = c.get("/v1/wiki/qa/ans-snap-001", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["answerId"] == "ans-snap-001"
    assert "summarySources" in data
    assert "blocks" in data
