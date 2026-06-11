"""Tests for POST /v1/structured + GET /v1/structured/<run_id>.

The POST kicks the run off via the core async handle
(``StructuredResponder.start_async`` → ``run_id``) and does a SHORT bounded
await for fast completion; the GET resolves a run_id back to its session and
reads the latest ``structured_output`` transcript event. We mock the
responder/runtime boundary — never a real model.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mewbo_api import backend

_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


@pytest.fixture()
def client():
    return backend.app.test_client()


@pytest.fixture()
def auth_headers():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


def _event(payload):
    return {"type": "structured_output", "payload": payload, "ts": "2026-01-01T00:00:00Z"}


# ---------------------------------------------------------------------------
# POST /v1/structured
# ---------------------------------------------------------------------------


def test_structured_requires_api_key(client):
    assert client.post("/v1/structured", json={"query": "x", "schema": _SCHEMA}).status_code == 401


def test_structured_rejects_missing_fields(client, auth_headers):
    r = client.post("/v1/structured", json={"query": "x"}, headers=auth_headers)
    assert r.status_code == 400
    assert "schema" in r.get_json()["message"]


def test_structured_post_fast_completion(client, auth_headers):
    """When the backing session emits structured_output quickly → status completed."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        responder = mock_cls.return_value
        responder.start_async.return_value = "sess-abc:r1"
        with patch(
            "mewbo_api.structured.routes._load_structured_output",
            return_value={"name": "Ada"},
        ):
            r = client.post(
                "/v1/structured",
                json={
                    "query": "Who?",
                    "schema": _SCHEMA,
                    "workspace": "wiki",
                    "tools": ["wiki_search_pages"],
                },
                headers={**auth_headers, "X-Mewbo-Surface": "mcp"},
            )
    assert r.status_code == 200
    body = r.get_json()
    assert body["run_id"] == "sess-abc:r1"
    assert body["status"] == "completed"
    assert body["output"] == {"name": "Ada"}
    assert body["workspace"] == "wiki"
    # Responder built with the request fields; run started via start_async.
    _, kwargs = mock_cls.call_args
    assert kwargs["schema"] == _SCHEMA
    assert kwargs["workspace"] == "wiki"
    assert kwargs["allowed_tools"] == ["wiki_search_pages"]
    # Surface from X-Mewbo-Surface is forwarded so the run is tagged + traced
    # as ``surface:mcp`` (covers the MCP ``structured_query`` tool path, #78).
    assert kwargs["source_platform"] == "mcp"
    responder.start_async.assert_called_once_with("Who?")


def test_structured_post_model_override_threaded_to_responder(client, auth_headers):
    """An optional 'model' field is threaded into StructuredResponder.model_name."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        mock_cls.return_value.start_async.return_value = "sess-m:r1"
        with patch(
            "mewbo_api.structured.routes._load_structured_output",
            return_value={"name": "Ada"},
        ):
            r = client.post(
                "/v1/structured",
                json={
                    "query": "Who?",
                    "schema": _SCHEMA,
                    "model": "openai/gpt-5.4-nano",
                },
                headers=auth_headers,
            )
    assert r.status_code == 200
    _, kwargs = mock_cls.call_args
    assert kwargs["model_name"] == "openai/gpt-5.4-nano"


def test_structured_post_model_omitted_defaults_to_none(client, auth_headers):
    """Omitting 'model' leaves model_name None → the configured default is used."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        mock_cls.return_value.start_async.return_value = "sess-d:r1"
        with patch(
            "mewbo_api.structured.routes._load_structured_output",
            return_value={"name": "Ada"},
        ):
            r = client.post(
                "/v1/structured",
                json={"query": "Who?", "schema": _SCHEMA},
                headers=auth_headers,
            )
    assert r.status_code == 200
    _, kwargs = mock_cls.call_args
    assert kwargs["model_name"] is None


def test_structured_post_non_string_model_ignored(client, auth_headers):
    """A non-string 'model' is ignored (treated as omitted), matching the draft idiom."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        mock_cls.return_value.start_async.return_value = "sess-n:r1"
        with patch(
            "mewbo_api.structured.routes._load_structured_output",
            return_value={"name": "Ada"},
        ):
            r = client.post(
                "/v1/structured",
                json={"query": "Who?", "schema": _SCHEMA, "model": 123},
                headers=auth_headers,
            )
    assert r.status_code == 200
    _, kwargs = mock_cls.call_args
    assert kwargs["model_name"] is None


def test_structured_post_model_override_applied_to_graph_first_responder(client, auth_headers):
    """The override is applied at the ONE route seam, covering the graph-first path.

    ``_graph_first_responder`` returns a built (frozen) ``StructuredResponder``;
    the route applies ``model`` via ``dataclasses.replace`` AFTER it returns
    (yielding a NEW responder), so the agentic_search-owned builder is never
    edited yet the responder the route actually drives honours the override.
    """
    from mewbo_core.structured_response import StructuredResponder

    built = StructuredResponder(
        runtime=MagicMock(),
        schema=_SCHEMA,
        workspace="search-ws",
        model_name="builder-default",
    )
    # Capture the responder instance whose run is actually started — it is the
    # post-replace copy, not ``built``. Patching the class method records ``self``.
    driven: list[StructuredResponder] = []

    def _capture_start(self, query):
        driven.append(self)
        return "sess-gf:r1"

    with (
        patch(
            "mewbo_api.structured.routes.StructuredResource._graph_first_responder",
            return_value=built,
        ),
        patch.object(StructuredResponder, "start_async", _capture_start),
        patch(
            "mewbo_api.structured.routes._load_structured_output",
            return_value={"name": "Ada"},
        ),
    ):
        r = client.post(
            "/v1/structured",
            json={
                "query": "Who?",
                "schema": _SCHEMA,
                "workspace": "search-ws",
                "model": "openai/gemini-3.1-flash-lite",
            },
            headers=auth_headers,
        )
    assert r.status_code == 200
    # The responder the route drove carries the overridden model; the original
    # builder output is left untouched (frozen-dataclass ``replace`` semantics).
    assert driven and driven[0].model_name == "openai/gemini-3.1-flash-lite"
    assert built.model_name == "builder-default"


def test_structured_post_running_when_no_output_yet(client, auth_headers):
    """No structured_output within the bounded await → status running, no output."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        mock_cls.return_value.start_async.return_value = "sess-xyz:r1"
        with (
            patch("mewbo_api.structured.routes._load_structured_output", return_value=None),
            patch("mewbo_api.structured.routes._FAST_AWAIT_SECONDS", 0.0),
        ):
            r = client.post(
                "/v1/structured",
                json={"query": "Who?", "schema": _SCHEMA, "workspace": "wiki"},
                headers=auth_headers,
            )
    assert r.status_code == 200
    body = r.get_json()
    assert body["run_id"] == "sess-xyz:r1"
    assert body["status"] == "running"
    assert body["workspace"] == "wiki"
    assert "output" not in body or body["output"] is None


def test_structured_post_start_failure_returns_error_envelope(client, auth_headers):
    """A start_async failure surfaces a structured error envelope, never a raw 500 string."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        mock_cls.return_value.start_async.side_effect = RuntimeError("boom")
        r = client.post(
            "/v1/structured",
            json={"query": "Who?", "schema": _SCHEMA},
            headers=auth_headers,
        )
    body = r.get_json()
    assert r.status_code >= 400
    assert "error" in body
    assert body["error"]["code"]
    assert body["error"]["reason"]
    # Never leak the internal tool name.
    assert "emit_result" not in str(body).lower()


def test_structured_post_refused_start_returns_error(client, auth_headers):
    """An empty run_id (registry refused) is a structured error, not a crash."""
    with patch("mewbo_api.structured.routes.StructuredResponder") as mock_cls:
        mock_cls.return_value.start_async.return_value = ""
        r = client.post(
            "/v1/structured",
            json={"query": "Who?", "schema": _SCHEMA},
            headers=auth_headers,
        )
    assert r.status_code >= 400
    assert "error" in r.get_json()


# ---------------------------------------------------------------------------
# GET /v1/structured/<run_id>
# ---------------------------------------------------------------------------


def test_structured_get_requires_api_key(client):
    assert client.get("/v1/structured/sess-abc:r1").status_code == 401


def test_structured_get_completed_returns_output(client, auth_headers):
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-abc"]
    rt.session_store.load_transcript.return_value = [_event({"name": "Ada"})]
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-abc:r1", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["run_id"] == "sess-abc:r1"
    assert body["status"] == "completed"
    assert body["output"] == {"name": "Ada"}
    # session_id recovered by splitting on the FIRST colon.
    rt.session_store.load_transcript.assert_called_once_with("sess-abc")


def test_structured_get_completed_carries_graph_provenance(client, auth_headers):
    """A graph-first run's GET surfaces additive pathway/probe provenance (#77)."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-gf"]
    rt.session_store.load_transcript.return_value = [
        {"type": "tool_result", "payload": {"tool_id": "scg_route"}},
        {"type": "tool_result", "payload": {"tool_id": "scg_route"}},
        {"type": "sub_agent", "payload": {"agent_id": "p1", "action": "start"}},
        {"type": "sub_agent", "payload": {"agent_id": "p1", "action": "stop",
                                          "status": "completed"}},
        {"type": "sub_agent", "payload": {"agent_id": "p2", "action": "stop",
                                          "status": "no_data"}},
        _event({"owner": "team-payments"}),
    ]
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-gf:r1", headers=auth_headers)
    body = r.get_json()
    assert body["output"] == {"owner": "team-payments"}
    prov = body["provenance"]
    assert prov["recipes_routed"] == 2
    assert prov["probes_run"] == 2
    assert prov["probe_status"] == {"p1": "completed", "p2": "no_data"}
    # ONE transcript read per GET (output + provenance share it).
    rt.session_store.load_transcript.assert_called_once_with("sess-gf")


def test_structured_get_plain_run_has_no_provenance(client, auth_headers):
    """A plain (non-graph) run that fanned no probes carries no provenance key."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-abc"]
    rt.session_store.load_transcript.return_value = [_event({"name": "Ada"})]
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-abc:r1", headers=auth_headers)
    assert "provenance" not in r.get_json()


def test_structured_get_latest_output_wins(client, auth_headers):
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-abc"]
    rt.session_store.load_transcript.return_value = [
        _event({"name": "old"}),
        _event({"name": "new"}),
    ]
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-abc:r1", headers=auth_headers)
    assert r.get_json()["output"] == {"name": "new"}


def test_structured_get_running_no_output(client, auth_headers):
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-abc"]
    rt.session_store.load_transcript.return_value = []
    rt.summarize_session.return_value = {"status": "running"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-abc:r1", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "running"
    assert body.get("output") is None
    assert "error" not in body


def test_structured_get_terminal_without_output_yields_422_envelope(client, auth_headers):
    """Terminal session that never emitted a structured result → 422 error envelope."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-abc"]
    rt.session_store.load_transcript.return_value = []
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-abc:r1", headers=auth_headers)
    body = r.get_json()
    assert "error" in body
    assert body["error"]["code"] == 422
    assert body["error"]["reason"]
    # Never leak the internal tool name nor an empty 'failed:' string.
    assert "emit_result" not in str(body).lower()


def test_structured_get_validation_failure_payload_is_error(client, auth_headers):
    """A structured_output carrying the emit tool's _error marker → error envelope."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-abc"]
    rt.session_store.load_transcript.return_value = [
        _event({"_error": "schema_validation_failed", "detail": "Field 'name': required"})
    ]
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-abc:r1", headers=auth_headers)
    body = r.get_json()
    assert "error" in body
    assert body["error"]["code"] == 422
    assert "output" not in body or body["output"] is None


def test_structured_get_unknown_session_is_error_envelope(client, auth_headers):
    """#40/#64: an unknown run id 404s on the existence check, BEFORE the route
    ever reads the transcript or summarizes — no phantom idle/422 fall-through."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = []
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-missing:r1", headers=auth_headers)
    body = r.get_json()
    assert r.status_code == 404
    assert body["error"]["code"] == 404
    assert body["run_id"] == "sess-missing:r1"
    # Existence is checked first: neither transcript read nor summarize runs.
    rt.session_store.load_transcript.assert_not_called()
    rt.summarize_session.assert_not_called()


def test_structured_get_output_always_reports_completed_status(client, auth_headers):
    """Output present ⇒ status is always 'completed', regardless of summarize_session."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-race"]
    rt.session_store.load_transcript.return_value = [_event({"name": "Ada"})]
    # Simulate a brief race where summarize_session still says 'running'.
    rt.summarize_session.return_value = {"status": "running"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-race:r1", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "completed"
    assert body["output"] == {"name": "Ada"}
    assert body["run_id"] == "sess-race:r1"


def test_structured_get_running_body_has_run_id_no_output(client, auth_headers):
    """Running state: run_id present, no output key, no error key."""
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-run"]
    rt.session_store.load_transcript.return_value = []
    rt.summarize_session.return_value = {"status": "running"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-run:r2", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["run_id"] == "sess-run:r2"
    assert body["status"] == "running"
    assert "output" not in body
    assert "error" not in body


def test_structured_get_terminal_no_output_error_is_caller_meaningful(client, auth_headers):
    """Natural completion without structured output returns a caller-meaningful 422.

    The error reason must not leak internal tool names (e.g. 'emit_result') and
    must provide a actionable suggestion ('retry with a simpler schema').
    """
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = ["sess-noresult"]
    rt.session_store.load_transcript.return_value = []
    rt.summarize_session.return_value = {"status": "completed"}
    with patch("mewbo_api.structured.routes._runtime", rt):
        r = client.get("/v1/structured/sess-noresult:r1", headers=auth_headers)
    assert r.status_code == 422
    body = r.get_json()
    assert "error" in body
    assert body["error"]["code"] == 422
    reason = body["error"]["reason"]
    assert "emit_result" not in reason.lower()
    assert "retry" in reason.lower() or "schema" in reason.lower()
    assert body["run_id"] == "sess-noresult:r1"


def test_structured_grounding_tool_id_exists_in_plugin_manifest():
    """'wiki_search_pages' is the canonical grounding tool; ensure no typo silently passes.

    Reads the wiki plugin manifest (the authoritative list of registered tool ids)
    and asserts the id referenced in the structured-routes tests is actually present.
    A missing id here means the tool was renamed and the test/docs need updating.
    """
    import json
    from pathlib import Path

    manifest_path = (
        Path(__file__).parents[3]
        / "packages"
        / "mewbo_graph"
        / "src"
        / "mewbo_graph"
        / "plugins"
        / "wiki"
        / ".claude-plugin"
        / "plugin.json"
    )
    manifest = json.loads(manifest_path.read_text())
    registered_ids = {entry["tool_id"] for entry in manifest.get("session_tools", [])}
    assert "wiki_search_pages" in registered_ids, (
        "wiki_search_pages is not registered in the wiki plugin manifest; "
        "update the tool id reference in structured-routes tests."
    )
