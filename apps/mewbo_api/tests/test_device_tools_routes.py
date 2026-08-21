"""Route-level contract tests for client-declared device tools.

Drives the real Flask backend test client against a temp-store runtime (only
auth, storage root, and the run-dispatch seam are swapped — same shape as
``test_api_session_message_reengage.py``). Covers:

1. ``POST /query`` with valid ``context.device_tools`` binds a
   ``ClientDeclaredTool`` into ``extra_session_tools``; an invalid spec 400s.
2. The SAME derivation applies at every dispatch site that was duplicating
   ``_extract_allowed_tools`` (``/query``, ``/message`` re-engage, ``/recover``,
   sync ``POST /api/query``) — the DRY refactor requires.
3. A full dispatch round trip through the concrete ``ApiDeviceToolDispatcher``
   and the ``POST .../device_tools/<call_id>/result`` route, without any LLM.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

API_KEY = "test-master-token-179"

_VALID_DEVICE_TOOL = {
    "tool_id": "device_send_sms",
    "description": "Send an SMS message.",
    "parameters": {
        "type": "object",
        "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "body"],
    },
}

_INVALID_DEVICE_TOOL = {
    "tool_id": "send_sms",  # missing required device_ prefix
    "description": "Send an SMS message.",
    "parameters": {"type": "object"},
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from mewbo_core.loop.session_runtime import SessionRuntime
    from mewbo_core.session.session_event_bus import reset_session_event_bus_for_tests
    from mewbo_core.session.session_store import SessionStore

    reset_session_event_bus_for_tests()

    import mewbo_api.backend as backend
    from mewbo_api.device_tools import reset_pending_calls_for_tests

    reset_pending_calls_for_tests()
    monkeypatch.setattr(backend, "MASTER_API_TOKEN", API_KEY, raising=False)
    store = SessionStore(root_dir=str(tmp_path / "sessions"))
    rt = SessionRuntime(session_store=store)
    monkeypatch.setattr(backend, "runtime", rt, raising=False)
    backend.app.config["TESTING"] = True
    return backend.app.test_client(), rt, backend


def _headers() -> dict:
    return {"X-API-KEY": API_KEY}


def _wait_for_call_event(rt, session_id: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = rt.load_events(session_id)
        for evt in events:
            if evt.get("type") == "device_tool_call":
                return evt["payload"]
        time.sleep(0.02)
    raise AssertionError("device_tool_call event never appeared")


# ---------------------------------------------------------------------------
# Startup registration
# ---------------------------------------------------------------------------


def test_dispatcher_registered_at_api_startup():
    from mewbo_core.tooling.client_tools import DeviceToolDispatcher

    assert DeviceToolDispatcher.available() is True


# ---------------------------------------------------------------------------
# /query — the primary dispatch site
# ---------------------------------------------------------------------------


def test_query_binds_device_tools_into_extra_session_tools(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.tooling.client_tools import ClientDeclaredTool

    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return "sid:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"device_tools": [_VALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    assert resp.status_code == 202
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], ClientDeclaredTool)
    assert tools[0].tool_id == "device_send_sms"
    assert tools[0].schema["function"]["parameters"] == _VALID_DEVICE_TOOL["parameters"]


def test_query_invalid_device_tool_spec_returns_400(client, monkeypatch):
    c, rt, backend = client

    def fake_start_async(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("start_async must not be called for an invalid spec")

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"device_tools": [_INVALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    assert resp.status_code == 400


def test_query_invalid_device_tool_spec_does_not_poison_session_context(client, monkeypatch):
    """F6: validate BEFORE persisting — a 400'd malformed declaration must
    never become the session's latest context event, or every subsequent
    re-engage/recover would inherit it and 400 forever (a bricked session)."""
    c, rt, backend = client

    monkeypatch.setattr(rt, "start_async", lambda **kw: "sid:r1")

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"device_tools": [_INVALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    assert resp.status_code == 400

    context_events = [
        e["payload"] for e in rt.load_events(sid) if e.get("type") == "context"
    ]
    assert all("device_tools" not in c for c in context_events)

    # The session is NOT bricked: a subsequent /message re-engage succeeds.
    resp = c.post(
        f"/api/sessions/{sid}/message", json={"text": "hi"}, headers=_headers()
    )
    assert resp.status_code == 200


def test_query_duplicate_device_tool_ids_returns_400(client, monkeypatch):
    """SessionToolRegistry.build_for resolves session tools by id, first-wins —
    a silent duplicate would leave the second declaration invisible rather
    than rejected, so reject it outright."""
    c, rt, backend = client

    def fake_start_async(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("start_async must not be called for duplicate tool_ids")

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]

    duplicate = dict(_VALID_DEVICE_TOOL)
    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"device_tools": [duplicate, dict(duplicate)]}},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert "duplicate" in resp.get_json()["message"].lower()


def test_query_without_device_tools_passes_empty_extra_session_tools(client, monkeypatch):
    c, rt, backend = client
    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return "sid:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/query", json={"query": "hi"}, headers=_headers()
    )
    assert resp.status_code == 202
    assert captured.get("extra_session_tools") == []


# ---------------------------------------------------------------------------
# /message re-engage — must rebuild from LAST-PERSISTED context (DRY site)
# ---------------------------------------------------------------------------


def test_message_reengage_rebuilds_device_tools_from_persisted_context(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.tooling.client_tools import ClientDeclaredTool

    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post(
        "/api/sessions",
        json={"context": {"device_tools": [_VALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/message",
        json={"text": "hello again"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], ClientDeclaredTool)
    assert tools[0].tool_id == "device_send_sms"


def test_message_reengage_self_heals_a_poisoned_persisted_context(client, monkeypatch):
    """F6 defense-in-depth: `/message` re-engage reads PERSISTED context it
    cannot 400 on behalf of. A malformed `device_tools` declaration that
    somehow made it into the transcript (session creation does not validate
    `device_tools` — a realistic poisoning vector even after the
    validate-before-persist ordering fix) must be dropped with a warning,
    never brick the session with a repeating 400."""
    c, rt, backend = client

    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    # POST /api/sessions never validates device_tools — this persists a
    # malformed declaration directly, simulating an already-poisoned session.
    resp = c.post(
        "/api/sessions",
        json={"context": {"device_tools": [_INVALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/message", json={"text": "hi"}, headers=_headers()
    )
    assert resp.status_code == 200
    assert captured.get("extra_session_tools") == []


# ---------------------------------------------------------------------------
# /recover — same DRY site
# ---------------------------------------------------------------------------


def test_recover_rebuilds_device_tools_from_persisted_context(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.tooling.client_tools import ClientDeclaredTool

    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]
    # `_load_last_context` reads only the single MOST-RECENT context event
    # (pre-existing behaviour, same as `mcp_tools`) — a well-behaved client
    # resends `device_tools` on every /query turn (the Aura context law), so
    # carry it on the turn that fails and gets recovered.
    c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"device_tools": [_VALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    # Simulate a failed run so recovery has something to retry: a prior user
    # turn (start_async is mocked away, so the route never records one) plus
    # a failed completion.
    rt.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    rt.append_event(sid, {"type": "completion", "payload": {"done": False, "done_reason": "error"}})

    resp = c.post(
        f"/api/sessions/{sid}/recover", json={"action": "retry"}, headers=_headers()
    )
    assert resp.status_code == 202
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], ClientDeclaredTool)


def test_recover_self_heals_a_poisoned_persisted_context(client, monkeypatch):
    """F6 defense-in-depth for `/recover`, mirroring the `/message` case."""
    c, rt, backend = client

    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]
    rt.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    rt.append_event(sid, {"type": "completion", "payload": {"done": False, "done_reason": "error"}})
    # Poison the LATEST context event directly (bypasses the validated
    # dispatch sites — the realistic poisoning vector, see the /message test).
    rt.append_context_event(sid, {"device_tools": [_INVALID_DEVICE_TOOL]})

    resp = c.post(
        f"/api/sessions/{sid}/recover", json={"action": "retry"}, headers=_headers()
    )
    assert resp.status_code == 202
    assert captured.get("extra_session_tools") == []


# ---------------------------------------------------------------------------
# Sync POST /api/query — the fourth DRY site
# ---------------------------------------------------------------------------


def test_sync_query_binds_device_tools(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.classes import TaskQueue
    from mewbo_core.tooling.client_tools import ClientDeclaredTool

    captured: dict = {}

    def fake_run_sync(**kwargs):
        captured.update(kwargs)
        return TaskQueue(steps=[])

    monkeypatch.setattr(rt, "run_sync", fake_run_sync)

    resp = c.post(
        "/api/query",
        json={"query": "hi", "context": {"device_tools": [_VALID_DEVICE_TOOL]}},
        headers=_headers(),
    )
    assert resp.status_code == 200
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], ClientDeclaredTool)


def test_sync_query_invalid_device_tool_spec_returns_400_without_persisting(client, monkeypatch):
    """F6 ordering fix applies to the sync endpoint too: validate before
    `append_context_event`."""
    c, rt, backend = client

    def fake_run_sync(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("run_sync must not be called for an invalid spec")

    monkeypatch.setattr(rt, "run_sync", fake_run_sync)

    resp = c.post(
        "/api/query",
        json={
            "query": "hi",
            "session_id": "presized-sid",
            "context": {"device_tools": [_INVALID_DEVICE_TOOL]},
        },
        headers=_headers(),
    )
    assert resp.status_code == 400
    context_events = [
        e["payload"]
        for e in rt.load_events("presized-sid")
        if e.get("type") == "context"
    ]
    assert all("device_tools" not in c for c in context_events)


# ---------------------------------------------------------------------------
# Full dispatch round trip — no LLM
# ---------------------------------------------------------------------------


def test_full_dispatch_round_trip_via_result_route(client):
    c, rt, backend = client
    from mewbo_api.device_tools import ApiDeviceToolDispatcher
    from mewbo_core.session.session_event_bus import get_session_event_bus

    session_id = rt.resolve_session()
    get_session_event_bus().subscribe(session_id, executor=True)  # the DEVICE client
    dispatcher = ApiDeviceToolDispatcher(runtime=rt)

    result_box: dict = {}

    def _run_dispatch():
        result_box["result"] = asyncio.run(
            dispatcher.dispatch(session_id, "device_send_sms", {"to": "1"})
        )

    thread = threading.Thread(target=_run_dispatch)
    thread.start()
    try:
        payload = _wait_for_call_event(rt, session_id)
        assert payload["tool_id"] == "device_send_sms"
        assert "call_id" in payload and "call_token" in payload and "expires_at" in payload

        # Wrong token → 403, call remains pending.
        resp = c.post(
            f"/api/sessions/{session_id}/device_tools/{payload['call_id']}/result",
            json={"call_token": "wrong-token", "status": "ok", "result": {}},
            headers=_headers(),
        )
        assert resp.status_code == 403

        # Unknown call_id → 404.
        resp = c.post(
            f"/api/sessions/{session_id}/device_tools/unknown-call/result",
            json={"call_token": payload["call_token"], "status": "ok", "result": {}},
            headers=_headers(),
        )
        assert resp.status_code == 404

        # Malformed body → 400.
        resp = c.post(
            f"/api/sessions/{session_id}/device_tools/{payload['call_id']}/result",
            json={"call_token": payload["call_token"]},
            headers=_headers(),
        )
        assert resp.status_code == 400

        # F3: an error status with neither a code nor a message → 400, not
        # silently accepted then relying on client_tools defaulting alone.
        resp = c.post(
            f"/api/sessions/{session_id}/device_tools/{payload['call_id']}/result",
            json={"call_token": payload["call_token"], "status": "error", "error": {}},
            headers=_headers(),
        )
        assert resp.status_code == 400

        # Correct token → 200, resolves the waiting dispatch().
        resp = c.post(
            f"/api/sessions/{session_id}/device_tools/{payload['call_id']}/result",
            json={"call_token": payload["call_token"], "status": "ok", "result": {"sent": True}},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"resolved": True}

        thread.join(timeout=5)
        assert not thread.is_alive()
        assert result_box["result"] == {"status": "ok", "result": {"sent": True}}

        # Second POST for the same (now-consumed) call → 409.
        resp = c.post(
            f"/api/sessions/{session_id}/device_tools/{payload['call_id']}/result",
            json={"call_token": payload["call_token"], "status": "ok", "result": {}},
            headers=_headers(),
        )
        assert resp.status_code == 409
    finally:
        thread.join(timeout=5)


def test_dispatch_timeout_surfaces_device_timeout_error(client, monkeypatch):
    c, rt, backend = client
    import mewbo_api.device_tools as device_tools_mod
    from mewbo_api.device_tools import ApiDeviceToolDispatcher
    from mewbo_core.session.session_event_bus import get_session_event_bus

    monkeypatch.setattr(device_tools_mod, "DEVICE_TOOL_TIMEOUT_S", 0.2)
    session_id = rt.resolve_session()
    get_session_event_bus().subscribe(session_id, executor=True)  # the DEVICE client
    dispatcher = ApiDeviceToolDispatcher(runtime=rt)

    result = asyncio.run(dispatcher.dispatch(session_id, "device_send_sms", {}))
    assert result == {
        "status": "error",
        "error": {
            "code": "device_timeout",
            "message": (
                "No result received for device tool 'device_send_sms' within 0.2s."
            ),
        },
    }


class TestExtractAllowedToolsThreeState:
    """``mcp_tools`` is three-state and the empty case is a real ceiling.

    ``_extract_allowed_tools`` reads the PERSISTED context at every re-engage
    site, so a collapse here silently re-widens a session's tool scope on
    ``/message`` and ``/recover`` even when the original ``start_async`` was
    correctly scoped. A client that persisted ``mcp_tools: []`` advertised no
    MCP tools; returning ``None`` re-bound every MCP tool in the registry to a
    session that declared none.
    """

    def test_absent_mcp_tools_is_unrestricted(self):
        from mewbo_api.backend import _extract_allowed_tools

        assert _extract_allowed_tools({}) is None
        assert _extract_allowed_tools({"cwd": "/tmp"}) is None

    def test_an_empty_mcp_tools_list_is_preserved_as_empty(self):
        from mewbo_api.backend import _extract_allowed_tools

        assert _extract_allowed_tools({"mcp_tools": []}) == []

    def test_a_non_empty_mcp_tools_list_passes_through(self):
        from mewbo_api.backend import _extract_allowed_tools

        assert _extract_allowed_tools({"mcp_tools": ["mcp_a", "mcp_b"]}) == [
            "mcp_a",
            "mcp_b",
        ]

    def test_a_non_list_mcp_tools_value_is_unrestricted(self):
        from mewbo_api.backend import _extract_allowed_tools

        assert _extract_allowed_tools({"mcp_tools": "not-a-list"}) is None
