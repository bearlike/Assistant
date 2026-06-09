"""Tests for ``mewbo_api.realtime.routes``.

Covers ``POST /v1/structured/fast``:
- Auth gate: 401 when X-API-KEY is missing.
- Input validation: 400 on missing / wrong-type query or schema.
- Happy path: 200 with ``output`` validating against the schema + non-empty
  ``citations`` when a fake grounding provider is injected.
- Grounding optional: 200 with empty ``citations`` when no workspace given.
- Synthesis failure: 422 when the synthesizer raises StructuredResponseError.
- Status field is always ``"completed"`` on 200 responses.

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
- ``StructuredSynthesizer.synthesize`` and ``DraftStreamer.astream`` are
  patched at the Flask route level so we exercise the route logic (parsing,
  auth, serialisation, SSE framing) without hitting a real LLM.
- The namespace is registered on the app in a session-scoped fixture so the
  tests run independently of whether the controller has wired it yet.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from mewbo_api import backend
from mewbo_core.structured_response import StructuredResponseError
from mewbo_core.structured_synthesis import Citation

# ---------------------------------------------------------------------------
# Fixtures
#
# The realtime namespace is registered by ``backend.py`` at import time (the
# production ``init_realtime`` wiring), so importing ``backend`` below makes
# ``/v1/structured/fast`` + ``/v1/draft/stream`` available. We deliberately do
# NOT re-register from the test: ``add_url_rule`` after the shared app has
# handled its first request raises (Flask setup is frozen) — which made these
# tests error only under full-suite ordering.
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

_VALID_PAYLOAD = {"answer": "42"}

_CITATIONS = [
    Citation(id="p1", kind="page", snippet="Page snippet", score=0.9, source="page.md"),
    Citation(id="n1", kind="node", snippet="def foo():", score=0.7, source="mod.py#foo"),
]


@pytest.fixture()
def auth_headers():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


@pytest.fixture()
def client():
    return backend.app.test_client()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_fast_requires_auth(client):
    """POST /v1/structured/fast without API key returns 401."""
    resp = client.post(
        "/v1/structured/fast",
        json={"query": "q", "schema": _SCHEMA},
    )
    assert resp.status_code == 401, (
        f"Expected 401, got {resp.status_code}: {resp.get_data(as_text=True)}"
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_fast_missing_query_returns_400(client, auth_headers):
    """Missing 'query' → 400."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        return _VALID_PAYLOAD, []

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"schema": _SCHEMA},
            headers=auth_headers,
        )
    assert resp.status_code == 400


def test_fast_missing_schema_returns_400(client, auth_headers):
    """Missing 'schema' → 400."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        return _VALID_PAYLOAD, []

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "q"},
            headers=auth_headers,
        )
    assert resp.status_code == 400


def test_fast_schema_not_dict_returns_400(client, auth_headers):
    """schema must be a JSON object, not a string."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        return _VALID_PAYLOAD, []

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "q", "schema": "not-a-dict"},
            headers=auth_headers,
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Happy path: with grounding
# ---------------------------------------------------------------------------


def test_fast_returns_output_and_citations(client, auth_headers):
    """200 response contains output that validates the schema + non-empty citations."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        return _VALID_PAYLOAD, list(_CITATIONS)

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "What is the answer?", "schema": _SCHEMA, "workspace": "org/repo"},
            headers=auth_headers,
        )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()

    # output validates the schema
    assert "output" in data, f"'output' missing from response: {data}"
    assert data["output"] == _VALID_PAYLOAD, f"Unexpected output: {data['output']!r}"

    # citations are present and non-empty
    assert "citations" in data, f"'citations' missing from response: {data}"
    assert len(data["citations"]) == 2, f"Expected 2 citations, got {len(data['citations'])}"
    first = data["citations"][0]
    assert first["id"] == "p1"
    assert first["kind"] == "page"
    assert "snippet" in first
    assert "score" in first
    assert "source" in first

    # status
    assert data.get("status") == "completed"


# ---------------------------------------------------------------------------
# Happy path: no workspace → empty citations
# ---------------------------------------------------------------------------


def test_fast_without_workspace_returns_empty_citations(client, auth_headers):
    """When no workspace is given, citations should be empty."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        assert workspace is None, "workspace should be None when not provided"
        return _VALID_PAYLOAD, []

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "q", "schema": _SCHEMA},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["citations"] == []
    assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# Synthesis failure → 422
# ---------------------------------------------------------------------------


def test_fast_synthesis_failure_returns_422(client, auth_headers):
    """StructuredResponseError from synthesize → 422 error envelope."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        raise StructuredResponseError("validation exhausted")

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "q", "schema": _SCHEMA},
            headers=auth_headers,
        )

    assert resp.status_code == 422
    data = resp.get_json()
    assert "error" in data
    assert data["error"]["code"] == 422


# ---------------------------------------------------------------------------
# Unexpected exception → 500
# ---------------------------------------------------------------------------


def test_fast_unexpected_exception_returns_500(client, auth_headers):
    """An unexpected exception from synthesize → 500 error envelope (not unhandled)."""
    async def _synth(self, query, schema, *, workspace=None, k=8):
        raise RuntimeError("unexpected boom")

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "q", "schema": _SCHEMA},
            headers=auth_headers,
        )

    assert resp.status_code == 500
    data = resp.get_json()
    assert "error" in data
    assert data["error"]["code"] == 500


# ---------------------------------------------------------------------------
# Workspace forwarded correctly
# ---------------------------------------------------------------------------


def test_fast_workspace_forwarded_to_synthesize(client, auth_headers):
    """The workspace from the request body is forwarded to synthesize."""
    received = {}

    async def _synth(self, query, schema, *, workspace=None, k=8):
        received["workspace"] = workspace
        return _VALID_PAYLOAD, []

    with patch("mewbo_api.realtime.routes.StructuredSynthesizer.synthesize", new=_synth):
        resp = client.post(
            "/v1/structured/fast",
            json={"query": "q", "schema": _SCHEMA, "workspace": "my/workspace"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert received.get("workspace") == "my/workspace"


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
