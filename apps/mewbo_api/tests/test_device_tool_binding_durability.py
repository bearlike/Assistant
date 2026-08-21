#!/usr/bin/env python3
"""A device-tool declaration must survive an unrelated context write.

``device_tools`` is not a spec field: it enters through the request-context
merge and every later run re-derives the toolset from the session's persisted
context. Read with the NEWEST context event verbatim, that makes every writer of
a context event a de-registration — the model simply stops having the tools, and
because "absent" and "never declared" are the same value, no surface can tell
which happened.

Three writers in the tree do exactly that, and none of them knows device tools
exist: ``approve_plan`` writes ``{"mode": "act"}``, ``reinject_recovery_context``
writes only its gating keys, and the fork route appends
``{forked_from, forked_at, model}`` as the new newest event. Teaching each of
them to carry the key forward is the smaller diff and the wrong shape — it puts
the obligation on every FUTURE writer. The read is narrowed instead, with the
same ``payload_key=`` protection ``project`` already has.

The paired negative matters as much as the positives: an EXPLICIT empty
declaration still de-registers, or "narrowed" would just mean "sticky forever".
"""

from __future__ import annotations

import pytest

API_KEY = "test-master-token-555"

_DEVICE_TOOL = {
    "tool_id": "device_send_sms",
    "description": "Send an SMS message.",
    "parameters": {
        "type": "object",
        "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "body"],
    },
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The real Flask app over a temp-dir store, with the run seam stubbed."""
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


@pytest.fixture()
def captured(client, monkeypatch):
    """Capture the kwargs the route hands ``start_async``."""
    _c, rt, _backend = client
    box: dict = {}

    def fake_start_async(**kwargs):
        box.clear()
        box.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)
    return box


def _device_tool_ids(captured: dict) -> list[str]:
    """The ids of the client-declared tools bound into the captured run."""
    from mewbo_core.tooling.client_tools import ClientDeclaredTool

    return [
        tool.tool_id
        for tool in captured.get("extra_session_tools") or []
        if isinstance(tool, ClientDeclaredTool)
    ]


def _declaring_session(c, declaration: list[dict] | None = None) -> str:
    """A session whose creation context declares the device tool."""
    resp = c.post(
        "/api/sessions",
        json={
            "context": {
                "device_tools": [_DEVICE_TOOL] if declaration is None else declaration,
                # Present so ``reinject_recovery_context`` has a gating key to
                # re-emit — without one it is a no-op and the recovery ordering
                # this suite reproduces never arises.
                "client_capabilities": ["device_control"],
            }
        },
        headers=_headers(),
    )
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    return resp.get_json()["session_id"]


class TestAnUnrelatedContextWriteDoesNotDeRegister:
    """The three writers, each reproduced through the route that drives it."""

    def test_a_mode_only_context_event_keeps_the_device_tools(self, client, captured):
        # What ``approve_plan`` appends: ``{"mode": "act"}``, no carry-forward.
        c, rt, _backend = client
        sid = _declaring_session(c)

        rt.append_context_event(sid, {"mode": "act"})

        resp = c.post(f"/api/sessions/{sid}/message", json={"text": "go"}, headers=_headers())
        assert resp.status_code == 200
        assert _device_tool_ids(captured) == ["device_send_sms"]

    def test_a_fork_still_resolves_its_device_tools(self, client, captured):
        # The fork route appends ``{forked_from, ...}`` as the NEWEST context
        # event of the copied transcript, so the declaration is present in the
        # fork's history and invisible to an un-narrowed read.
        c, _rt, _backend = client
        sid = _declaring_session(c)

        resp = c.post(f"/api/sessions/{sid}/fork", json={}, headers=_headers())
        assert resp.status_code == 201
        fork_id = resp.get_json()["session_id"]

        resp = c.post(
            f"/api/sessions/{fork_id}/query", json={"query": "hi"}, headers=_headers()
        )
        assert resp.status_code == 202
        assert _device_tool_ids(captured) == ["device_send_sms"]

    def test_a_recover_retry_then_a_message_keeps_the_device_tools(self, client, captured):
        # The displaced failure: ``/recover`` derives its grants and THEN
        # appends a gating-only context event, so the retry run binds fine and
        # the NEXT turn binds nothing. One turn between cause and symptom is
        # why this read as random.
        c, rt, _backend = client
        sid = _declaring_session(c)

        c.post(
            f"/api/sessions/{sid}/query",
            json={"query": "hi", "context": {"device_tools": [_DEVICE_TOOL]}},
            headers=_headers(),
        )
        # start_async is stubbed, so stand in for the failed run it would have
        # driven: a user turn plus a not-done completion makes it recoverable.
        rt.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        rt.append_event(
            sid, {"type": "completion", "payload": {"done": False, "done_reason": "error"}}
        )

        resp = c.post(f"/api/sessions/{sid}/recover", json={"action": "retry"}, headers=_headers())
        assert resp.status_code == 202
        assert _device_tool_ids(captured) == ["device_send_sms"], "the retry run itself"

        resp = c.post(f"/api/sessions/{sid}/message", json={"text": "again"}, headers=_headers())
        assert resp.status_code == 200
        assert _device_tool_ids(captured) == ["device_send_sms"], "the turn AFTER the retry"

    def test_a_query_that_omits_them_keeps_the_device_tools(self, client, captured):
        # The client re-advertises on every ``/query`` today, which is what
        # masked all of this. A turn that does not is not a de-registration.
        c, _rt, _backend = client
        sid = _declaring_session(c)

        resp = c.post(f"/api/sessions/{sid}/query", json={"query": "hi"}, headers=_headers())
        assert resp.status_code == 202
        assert _device_tool_ids(captured) == ["device_send_sms"]


class TestAnExplicitDeclarationStillDecides:
    """Narrowing must not become "sticky forever" — the paired negatives."""

    def test_an_explicit_empty_declaration_de_registers(self, client, captured):
        c, _rt, _backend = client
        sid = _declaring_session(c)

        # A client that stops advertising says so: ``[]`` is a DECLARATION of
        # none, distinct from a context event that is silent on the subject.
        resp = c.post(
            f"/api/sessions/{sid}/query",
            json={"query": "stop", "context": {"device_tools": []}},
            headers=_headers(),
        )
        assert resp.status_code == 202
        assert _device_tool_ids(captured) == []

        resp = c.post(f"/api/sessions/{sid}/message", json={"text": "after"}, headers=_headers())
        assert resp.status_code == 200
        assert _device_tool_ids(captured) == []

    def test_a_session_that_never_declared_binds_nothing(self, client, captured):
        c, _rt, _backend = client
        resp = c.post("/api/sessions", json={}, headers=_headers())
        sid = resp.get_json()["session_id"]

        resp = c.post(f"/api/sessions/{sid}/query", json={"query": "hi"}, headers=_headers())
        assert resp.status_code == 202
        assert captured.get("extra_session_tools") == []

    def test_a_newer_declaration_replaces_the_older_one(self, client, captured):
        c, _rt, _backend = client
        sid = _declaring_session(c)

        replacement = {**_DEVICE_TOOL, "tool_id": "device_open_app"}
        resp = c.post(
            f"/api/sessions/{sid}/query",
            json={"query": "hi", "context": {"device_tools": [replacement]}},
            headers=_headers(),
        )
        assert resp.status_code == 202
        assert _device_tool_ids(captured) == ["device_open_app"]

        resp = c.post(f"/api/sessions/{sid}/message", json={"text": "again"}, headers=_headers())
        assert resp.status_code == 200
        assert _device_tool_ids(captured) == ["device_open_app"]


class TestTheReadIsNarrowedInTheStore:
    """A narrowing argument must narrow the WORK, not just the answer.

    The route-level tests above pass just as well against a fold over the whole
    transcript, which is the expensive way to be right — and the reason
    ``latest_event_of_type`` grew a ``payload_key`` at all. This pins the CALL:
    the binding asks the store for the newest context event that CARRIES the
    key, never for the newest context event.
    """

    def test_the_store_read_carries_the_payload_key(self):
        from mewbo_api.device_tools import DeviceToolBinding

        calls: list[tuple[str, str, str | None]] = []

        def latest_event_of_type(session_id, event_type, payload_key):
            calls.append((session_id, event_type, payload_key))
            return {"type": "context", "payload": {"device_tools": [_DEVICE_TOOL]}}

        binding = DeviceToolBinding(latest_event_of_type=latest_event_of_type)
        specs = binding.specs_for("s1", {})

        assert [s.tool_id for s in specs] == ["device_send_sms"]
        assert calls == [("s1", "context", "device_tools")]

    def test_a_declaration_in_hand_costs_no_store_read(self):
        # The request that CARRIES the declaration is the common path; it must
        # not pay a store read to re-answer a question it already holds.
        from mewbo_api.device_tools import DeviceToolBinding

        calls: list[tuple] = []

        def latest_event_of_type(session_id, event_type, payload_key):
            calls.append((session_id, event_type, payload_key))
            return None

        binding = DeviceToolBinding(latest_event_of_type=latest_event_of_type)
        specs = binding.specs_for("s1", {"device_tools": [_DEVICE_TOOL]})

        assert [s.tool_id for s in specs] == ["device_send_sms"]
        assert calls == []

    def test_a_malformed_persisted_declaration_still_raises(self):
        # The tolerant re-drive wrapper depends on this staying a ValueError —
        # resolving from the store must not swallow what validation refuses.
        from mewbo_api.device_tools import DeviceToolBinding

        binding = DeviceToolBinding(
            latest_event_of_type=lambda *_args: {
                "type": "context",
                "payload": {"device_tools": [{"tool_id": "send_sms", "description": "x"}]},
            }
        )
        with pytest.raises(ValueError):
            binding.specs_for("s1", {})
