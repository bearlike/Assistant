"""Tests for ``mewbo_api.realtime.routes``.

Covers ``POST /v1/draft/stream``:
- Auth gate: 401 when X-API-KEY is missing.
- Input validation: 400 on missing query.
- Happy path: ``text/event-stream`` response, frames parse to token deltas,
  terminal ``{done: true}`` frame is present.
- With workspace: grounding provider is consulted and its output is forwarded
  as context to the streamer.
- Without workspace: no grounding provider call.
- model override: forwarded to DraftStreamer constructor.

Stub boundary:
- ``DraftStreamer.astream`` is patched at the Flask route level so we exercise
  the route logic (parsing, auth, serialisation, SSE framing) without hitting
  a real LLM.
- The namespace is registered on the app in a session-scoped fixture so the
  tests run independently of whether the controller has wired it yet.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from mewbo_api import backend
from mewbo_api.realtime.recorder import (
    DRAFT_STREAM_TAG,
    FAST_STRUCTURED_TAG,
    RealtimeSessionRecorder,
)
from mewbo_core.session_provenance import SessionOrigin
from mewbo_core.structured_synthesis import Citation

# ---------------------------------------------------------------------------
# Fixtures
#
# The realtime namespace is registered by ``backend.py`` at import time (the
# production ``init_realtime`` wiring), so importing ``backend`` below makes
# ``/v1/draft/stream`` available. We deliberately do NOT re-register from the
# test: ``add_url_rule`` after the shared app has handled its first request
# raises (Flask setup is frozen) — which made these tests error only under
# full-suite ordering.
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {"answer": "42"}


@pytest.fixture()
def auth_headers():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


@pytest.fixture()
def client():
    return backend.app.test_client()


# ===========================================================================
# POST /v1/draft/stream — token-streaming draft endpoint
# ===========================================================================


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fake_astream_tokens(*tokens: str) -> AsyncIterator[str]:
    """Async generator that yields the given token strings."""
    for t in tokens:
        yield t


def _parse_sse_frames(body: str) -> list[dict]:
    """Parse an SSE response body into a list of decoded JSON payloads."""
    frames = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            frames.append(payload)
    return frames


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_draft_stream_requires_auth(client):
    """POST /v1/draft/stream without API key returns 401."""
    resp = client.post(
        "/v1/draft/stream",
        json={"query": "tell me about this"},
    )
    assert resp.status_code == 401, (
        f"Expected 401, got {resp.status_code}: {resp.get_data(as_text=True)}"
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_draft_stream_missing_query_returns_400(client, auth_headers):
    """Missing 'query' → 400."""

    async def _fake_stream(self, query, *, context=""):
        yield "ok"

    with patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream):
        resp = client.post(
            "/v1/draft/stream",
            json={},
            headers=auth_headers,
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Happy path: SSE frames + terminal done
# ---------------------------------------------------------------------------


def test_draft_stream_happy_path(client, auth_headers):
    """Response is text/event-stream; frames contain token deltas + terminal done."""
    tokens = ["Hello", ", ", "world", "!"]

    async def _fake_stream(self, query, *, context=""):
        for t in tokens:
            yield t

    with patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "say hello"},
            headers=auth_headers,
        )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert "text/event-stream" in resp.content_type

    body = resp.get_data(as_text=True)
    frames = _parse_sse_frames(body)

    # All token frames appear before the done frame
    token_frames = [f for f in frames if "token" in f]
    done_frames = [f for f in frames if f.get("done") is True]

    assert token_frames, f"No token frames found in: {body!r}"
    assert [f["token"] for f in token_frames] == tokens

    assert len(done_frames) == 1, f"Expected exactly one done frame, got: {done_frames}"
    # done frame is the LAST frame
    assert frames[-1].get("done") is True


def test_draft_stream_concatenated_tokens_equal_text(client, auth_headers):
    """Joining all token deltas reconstructs the full intended text."""
    words = ["The", " quick", " brown", " fox"]

    async def _fake_stream(self, query, *, context=""):
        for w in words:
            yield w

    with patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "finish this"},
            headers=auth_headers,
        )

    body = resp.get_data(as_text=True)
    frames = _parse_sse_frames(body)
    tokens = [f["token"] for f in frames if "token" in f]
    assert "".join(tokens) == "".join(words)


# ---------------------------------------------------------------------------
# Grounding: with workspace → provider consulted
# ---------------------------------------------------------------------------


def test_draft_stream_with_workspace_consults_grounding(client, auth_headers):
    """When workspace is provided, WikiGroundingProvider.search is called."""
    grounding_calls: list[tuple[str, str]] = []

    class _FakeProvider:
        def search(self, slug: str, query: str, *, k: int = 8) -> list[Citation]:
            grounding_calls.append((slug, query))
            return [
                Citation(
                    id="p1",
                    kind="page",
                    snippet="Some info",
                    score=0.9,
                    source="page.md",
                )
            ]

    context_seen: list[str] = []

    async def _fake_stream(self, query, *, context=""):
        context_seen.append(context)
        yield "grounded answer"

    with (
        patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream),
        patch(
            "mewbo_api.realtime.grounding.WikiGroundingProvider",
            new=_FakeProvider,
        ),
    ):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "what is X?", "workspace": "org/repo"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    # Grounding was consulted
    assert grounding_calls == [("org/repo", "what is X?")], (
        f"Unexpected grounding calls: {grounding_calls}"
    )
    # Context was forwarded to the streamer (non-empty)
    assert context_seen and context_seen[0], (
        "Expected non-empty context to be forwarded to DraftStreamer"
    )


def test_draft_stream_without_workspace_no_grounding(client, auth_headers):
    """Without a workspace, WikiGroundingProvider.search must NOT be called."""
    grounding_calls: list = []

    class _FakeProvider:
        def search(self, slug: str, query: str, **kwargs: object) -> list[Citation]:
            grounding_calls.append((slug, query))
            return []

    context_seen: list[str] = []

    async def _fake_stream(self, query, *, context=""):
        context_seen.append(context)
        yield "plain answer"

    with (
        patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream),
    ):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "what is X?"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert grounding_calls == [], "Grounding must NOT be called when workspace is absent"
    # Context must be empty string (no grounding)
    assert context_seen == [""], f"Expected empty context, got {context_seen}"


# ---------------------------------------------------------------------------
# Model override
# ---------------------------------------------------------------------------


def test_draft_stream_model_override_forwarded(client, auth_headers):
    """The 'model' field in the request body is forwarded to DraftStreamer."""
    captured_model: list[str | None] = []

    class _CapturingStreamer:
        def __init__(self, *, model_name: str | None = None) -> None:
            captured_model.append(model_name)

        async def astream(self, query: str, *, context: str = ""):
            yield "ok"

    with patch("mewbo_api.realtime.routes.DraftStreamer", new=_CapturingStreamer):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "q", "model": "openai/gpt-4o-mini"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert captured_model == ["openai/gpt-4o-mini"], (
        f"Expected model to be 'openai/gpt-4o-mini', got {captured_model}"
    )


# ===========================================================================
# Session-backing + provenance (#78) — the synthesis mode and draft stream
# mint a real session, tagged with the right origin, with a single-turn
# transcript persisted WRITE-BEHIND (after the response). We drive the real
# route + store path and stub only the LLM seam; persistence is forced
# synchronous so the test can read the store deterministically (the production
# path fires it on a daemon thread).
# ===========================================================================


@pytest.fixture()
def sync_persist():
    """Make ``persist_async`` run synchronously so the store write is observable.

    The route fires write-behind persistence on a daemon thread; under test we
    want the real persistence code path to run before we assert, so we redirect
    it to the (identical) synchronous ``persist``. This stubs scheduling only —
    the store write itself is the real one.
    """
    def _sync(self, **kwargs):
        self.persist(**kwargs)

    with patch.object(RealtimeSessionRecorder, "persist_async", new=_sync):
        yield


def _route_runtime():
    """The runtime the realtime routes actually write to.

    Read this rather than ``backend.runtime`` so the assertions are robust to
    full-suite ordering: ``test_backend._reset_backend`` rebinds
    ``backend.runtime`` to a temp store by plain assignment, but the route holds
    the import-time ``realtime.routes._runtime`` — so the persisted session lives
    in the latter, not whatever ``backend.runtime`` currently points at.
    """
    from mewbo_api.realtime import routes as realtime_routes
    return realtime_routes._runtime


def _transcript(session_id: str) -> list[dict]:
    return _route_runtime().session_store.load_transcript(session_id)


def test_draft_mints_tagged_session_with_streamed_text(client, auth_headers, sync_persist):
    """A draft stream mints a ``draft:stream`` session; streamed text is persisted."""
    tokens = ["Hello", ", ", "world"]

    async def _fake_stream(self, query, *, context=""):
        for t in tokens:
            yield t

    with patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "say hello"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # Additive: the session id rides a response header AND the done frame.
        session_id = resp.headers.get("X-Mewbo-Session")
        assert session_id, "expected X-Mewbo-Session header"
        body = resp.get_data(as_text=True)  # drains the generator → persist runs

    frames = _parse_sse_frames(body)
    done = [f for f in frames if f.get("done") is True]
    assert done and done[0].get("session_id") == session_id

    tags = _route_runtime().session_store.tags_for_session(session_id)
    assert f"{DRAFT_STREAM_TAG}:{session_id}" in tags
    assert DRAFT_STREAM_TAG not in tags
    assert SessionOrigin.classify(tags, {}) == SessionOrigin.DRAFT

    events = _transcript(session_id)
    assistant = [e for e in events if e.get("type") == "assistant"]
    assert assistant, f"expected an assistant turn in transcript: {[e.get('type') for e in events]}"
    assert assistant[-1]["payload"]["text"] == "".join(tokens)
    assert _route_runtime().summarize_session(session_id)["origin"] == "draft"


def test_draft_mid_stream_error_is_honest(client, auth_headers, sync_persist):
    """A stream that dies mid-flight emits an SSE error frame + summarizes failed.

    No false ``done``/``completed``: the transcript records an ``error``
    completion (not the success path), and the client gets an ``{"error": ...}``
    frame instead of ``{"done": true}``.
    """
    async def _boom_stream(self, query, *, context=""):
        yield "partial"
        raise RuntimeError("upstream exploded")

    with patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_boom_stream):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "q"},
            headers=auth_headers,
        )
        session_id = resp.headers.get("X-Mewbo-Session")
        body = resp.get_data(as_text=True)

    frames = _parse_sse_frames(body)
    assert any("error" in f for f in frames), f"expected an error frame: {frames}"
    assert not any(f.get("done") for f in frames), "no false done frame on failure"

    summary = _route_runtime().summarize_session(session_id)
    assert summary["status"] == "failed", f"expected failed, got {summary['status']}"


def test_draft_wire_contract_token_frames_unchanged(client, auth_headers, sync_persist):
    """Token frames stay ``{"token": ...}`` only — session_id rides the done frame."""
    async def _fake_stream(self, query, *, context=""):
        for t in ["a", "b"]:
            yield t

    with patch("mewbo_api.realtime.routes.DraftStreamer.astream", new=_fake_stream):
        resp = client.post(
            "/v1/draft/stream",
            json={"query": "q"},
            headers=auth_headers,
        )
        body = resp.get_data(as_text=True)

    frames = _parse_sse_frames(body)
    token_frames = [f for f in frames if "token" in f]
    # Token frames carry ONLY the token key — additive change is isolated to the
    # terminal done frame, so existing SideStage consumers are unaffected.
    assert all(set(f.keys()) == {"token"} for f in token_frames)
    assert [f["token"] for f in token_frames] == ["a", "b"]


# ===========================================================================
# Record existence + tag uniqueness through a REAL store (#87)
#
# The #78 gap: tests stubbed the route's ``_runtime`` and asserted event
# PAYLOADS, never that a session RECORD exists. The recorder appended events
# onto a pre-minted id but never created the record, so on Mongo (which lists
# the ``sessions`` collection, not ``events``) the transcript was an orphan:
# invisible to ``list_sessions`` and every read surface built on it.
#
# These drive the recorder directly against a fresh JSON-backed SessionRuntime
# (isolated from the shared backend app — no ``_reset_backend`` leak) and assert
# the RECORD is materialised + listed with the right origin, that two runs mint
# two DISTINCT tags onto two sessions, and that the transcript is readable.
# ===========================================================================


@pytest.fixture()
def real_runtime(tmp_path):
    """A real JSON-backed SessionRuntime rooted at an isolated temp dir."""
    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.session_store import SessionStore

    return SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path)))


def test_persist_materialises_listed_session_record(real_runtime):
    """``persist`` creates a RECORD visible to ``list_sessions`` (not an orphan).

    The defect: events existed but no session record, so ``list_sessions`` (and
    every console surface built on it) never saw the id. We assert the id is
    listed AND classified ``structured`` — both reads resolve the record.
    """
    recorder = RealtimeSessionRecorder.for_fast(real_runtime, "What is the answer?")
    sid = recorder.session_id

    # Before persist: no record, not listed.
    assert sid not in real_runtime.session_store.list_sessions()

    recorder.persist(output=_VALID_PAYLOAD)

    # After persist: a real RECORD, enumerated by the store's session list.
    assert sid in real_runtime.session_store.list_sessions(), (
        "persist() must materialise a session record, not just append orphan events"
    )
    # Listed by the runtime's summary surface (the /api/sessions path) with origin.
    listed = real_runtime.list_sessions()
    summary = next((s for s in listed if s["session_id"] == sid), None)
    assert summary is not None, f"session {sid} not in runtime.list_sessions()"
    assert summary["origin"] == "structured"


def test_persist_idempotent_on_replay(real_runtime):
    """Calling ``persist`` twice does not duplicate the record (idempotent)."""
    recorder = RealtimeSessionRecorder.for_draft(real_runtime, "say hi")
    recorder.persist(text="hi")
    recorder.persist(text="hi")
    sid = recorder.session_id
    assert real_runtime.session_store.list_sessions().count(sid) == 1


def test_two_runs_two_distinct_tags(real_runtime):
    """Two fast runs mint two DISTINCT per-session tags (no constant-tag collision).

    A constant tag (``structured:fast``) keyed the tags collection would make the
    second run OVERWRITE the first run's tag — the first session would silently
    lose its tag and reclassify to the ``user`` fallback. The per-session tag
    (``structured:fast:<id>``) keeps both runs independently tagged + classified.
    """
    r1 = RealtimeSessionRecorder.for_fast(real_runtime, "q1")
    r2 = RealtimeSessionRecorder.for_fast(real_runtime, "q2")
    r1.persist(output=_VALID_PAYLOAD)
    r2.persist(output=_VALID_PAYLOAD)

    store = real_runtime.session_store
    tags1 = store.tags_for_session(r1.session_id)
    tags2 = store.tags_for_session(r2.session_id)

    # Distinct tags, one per session — neither stole the other's.
    assert tags1 == [f"{FAST_STRUCTURED_TAG}:{r1.session_id}"]
    assert tags2 == [f"{FAST_STRUCTURED_TAG}:{r2.session_id}"]
    assert tags1 != tags2

    # The bare prefix is NOT a live tag → the first run is NOT resolvable by it,
    # proving no shared doc was overwritten.
    assert store.resolve_tag(FAST_STRUCTURED_TAG) is None

    # BOTH sessions still classify as ``structured`` (no reclassification to user).
    assert SessionOrigin.classify(tags1, {}) == SessionOrigin.STRUCTURED
    assert SessionOrigin.classify(tags2, {}) == SessionOrigin.STRUCTURED


def test_persisted_transcript_readable_via_events_path(real_runtime):
    """The single-turn transcript is readable via the load_transcript surface."""
    recorder = RealtimeSessionRecorder.for_fast(real_runtime, "the question")
    recorder.persist(output=_VALID_PAYLOAD)

    events = real_runtime.session_store.load_transcript(recorder.session_id)
    assert events, "transcript must be non-empty (the /events surface reads this)"
    types = [e.get("type") for e in events]
    assert "user" in types and "structured_output" in types and "completion" in types
    out = [e for e in events if e.get("type") == "structured_output"][-1]
    assert out["payload"] == _VALID_PAYLOAD
