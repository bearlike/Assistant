#!/usr/bin/env python3
"""A session's chosen model must survive an unrelated context write.

The same defect class as ``test_device_tool_binding_durability`` — a fact read
off the NEWEST context event verbatim, so every writer of an unrelated context
event silently erases it — but with a worse failure, because the fallback is not
"no model" but ``llm.default_model``. Absent reads as "nothing was chosen", so
the run does not refuse or degrade: it succeeds, on a different model, with no
fallback event, nothing logged, and a transcript whose stored ``session_spec``
still names the model the user picked.

Measured on the deployed stack, session ``bb7f59d5…``: created on
``zai/glm-4.7-flash-reap-fast`` and answered two hours later on the deployment's
default, because the newest context event at that point was the bare
``{"client_capabilities": [...]}`` this seam's own callers write.

``SessionSpecStore.load`` is narrowed to the newest event CARRYING the typed
mirror — the read that cannot miss this way, and already the source for
``project``, ``capabilities`` and ``fallback_models`` at both of these seams.
The model was simply left behind when they were narrowed.

The paired negative matters as much as the positives: an explicit per-request
override must still win, or "durable" would just mean "unchangeable".
"""

from __future__ import annotations

import pytest

API_KEY = "test-master-token-556"

PICKED_MODEL = "zai/glm-4.7-flash-reap-fast"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The real Flask app over a temp-dir store, with the run seam stubbed."""
    from mewbo_core.loop.session_runtime import SessionRuntime
    from mewbo_core.session.session_event_bus import reset_session_event_bus_for_tests
    from mewbo_core.session.session_store import SessionStore

    reset_session_event_bus_for_tests()

    import mewbo_api.backend as backend

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


def _session_on_picked_model(c) -> str:
    """A session created with an explicit model choice, as the picker sends it.

    The choice rides in ``context``, which is where ``POST /api/sessions`` reads
    it — a top-level ``model`` on THIS route is ignored, unlike ``/query`` and
    ``/recover`` where it is an override. Sending it the wrong way would make
    every assertion below compare the default against itself.
    """
    resp = c.post(
        "/api/sessions",
        json={
            "context": {
                "model": PICKED_MODEL,
                "client_capabilities": ["generative_ui"],
            }
        },
        headers=_headers(),
    )
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    session_id = resp.get_json()["session_id"]
    # Guard the fixture itself: if creation did not bind the model, every test
    # below would pass or fail for a reason that has nothing to do with the seam.
    import mewbo_api.backend as backend

    assert backend._session_specs.load(session_id).model == PICKED_MODEL
    return session_id


class TestAnUnrelatedContextWriteDoesNotUnbindTheModel:
    def test_a_capability_only_context_event_keeps_the_model(self, client, captured):
        """The measured shape, reproduced through the seam that lost it.

        ``{"client_capabilities": [...]}`` is what a re-engaging client writes,
        so this is not an exotic sequence — it is the ordinary one.
        """
        c, rt, _backend = client
        sid = _session_on_picked_model(c)

        rt.append_context_event(sid, {"client_capabilities": ["generative_ui"]})

        resp = c.post(f"/api/sessions/{sid}/message", json={"text": "go"}, headers=_headers())
        assert resp.status_code == 200
        assert captured.get("model_name") == PICKED_MODEL, (
            "Re-engagement fell back to the configured default, so the session "
            "silently changed model between turns."
        )

    def test_a_mode_only_context_event_keeps_the_model(self, client, captured):
        """What ``approve_plan`` appends — no carry-forward of anything else."""
        c, rt, _backend = client
        sid = _session_on_picked_model(c)

        rt.append_context_event(sid, {"mode": "act"})

        resp = c.post(f"/api/sessions/{sid}/message", json={"text": "go"}, headers=_headers())
        assert resp.status_code == 200
        assert captured.get("model_name") == PICKED_MODEL

    def test_recovery_re_drives_on_the_session_s_own_model(self, client, captured):
        """``/recover`` already read the LADDER off the spec, but not the model.

        A recovered run coming back on a different model than the fallback chain
        it was recovered with is the specific incoherence this closes.
        """
        c, rt, _backend = client
        sid = _session_on_picked_model(c)

        c.post(f"/api/sessions/{sid}/query", json={"query": "hi"}, headers=_headers())
        # ``start_async`` is stubbed, so stand in for the failed run it would
        # have driven: a user turn plus a not-done completion is recoverable.
        rt.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        rt.session_store.append_event(
            sid,
            {"type": "completion", "payload": {"done": False, "done_reason": "error"}},
        )
        # ``/query`` writes a FULL context event, so without this the newest one
        # still carries ``model`` and the assertion below passes against the
        # defect. Reproducing the vulnerable shape is the whole test: an
        # unrelated writer landing after the last full context event.
        rt.append_context_event(sid, {"client_capabilities": ["generative_ui"]})

        resp = c.post(
            f"/api/sessions/{sid}/recover", json={"action": "continue"}, headers=_headers()
        )
        assert resp.status_code in (200, 202), resp.get_data(as_text=True)
        assert captured.get("model_name") == PICKED_MODEL


class TestAnExplicitOverrideStillWins:
    """Durable must not mean unchangeable — the request tier still outranks."""

    def test_recover_honours_an_explicit_model_override(self, client, captured):
        c, rt, _backend = client
        sid = _session_on_picked_model(c)

        c.post(f"/api/sessions/{sid}/query", json={"query": "hi"}, headers=_headers())
        rt.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        rt.session_store.append_event(
            sid,
            {"type": "completion", "payload": {"done": False, "done_reason": "error"}},
        )

        resp = c.post(
            f"/api/sessions/{sid}/recover",
            json={"action": "continue", "model": "openai/some-other-model"},
            headers=_headers(),
        )
        assert resp.status_code in (200, 202), resp.get_data(as_text=True)
        assert captured.get("model_name") == "openai/some-other-model"
