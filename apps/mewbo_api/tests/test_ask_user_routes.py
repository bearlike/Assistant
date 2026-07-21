"""Route + dispatcher contract tests for ask-user questions.

Drives the real Flask backend test client against a temp-store runtime (same
shape as ``test_device_tools_routes.py``). Covers:

1. The ``ask_user`` capability in ``context.client_capabilities`` binds an
   ``AskUserQuestionTool`` into ``extra_session_tools`` at ``/query`` AND at
   the ``/message`` re-engage re-derivation; absence binds nothing.
2. ``QuestionPendingCalls`` semantics: answered-once, token check, semantic
   answer validation against the stored questions, withdraw-vs-resolve race.
3. A full dispatch round trip through ``ApiQuestionDispatcher`` and the
   ``POST .../questions/<call_id>/answer`` route, without any LLM — plus the
   steer-supersede / interrupt / cancel wake paths.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

import pytest
from mewbo_core.ask_user import AskUserQuestionArgs, QuestionAnswerItem
from mewbo_core.session_runtime import RunHandle

API_KEY = "test-master-token-ask-user"

_QUESTIONS_CONTEXT = {"client_capabilities": ["ask_user"]}

_ARGS = AskUserQuestionArgs.model_validate(
    {
        "questions": [
            {
                "header": "Scope",
                "question": "Which scope?",
                "options": [{"label": "Root only"}, {"label": "All agents"}],
            },
            {"header": "Notes", "question": "Anything else?"},
        ]
    }
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from mewbo_core.session_event_bus import reset_session_event_bus_for_tests
    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.session_store import SessionStore

    reset_session_event_bus_for_tests()

    import mewbo_api.backend as backend
    from mewbo_api.ask_user import reset_pending_questions_for_tests

    reset_pending_questions_for_tests()
    monkeypatch.setattr(backend, "MASTER_API_TOKEN", API_KEY, raising=False)
    store = SessionStore(root_dir=str(tmp_path / "sessions"))
    rt = SessionRuntime(session_store=store)
    monkeypatch.setattr(backend, "runtime", rt, raising=False)
    backend.app.config["TESTING"] = True
    return backend.app.test_client(), rt, backend


def _headers(**extra) -> dict:
    return {"X-API-KEY": API_KEY, **extra}


def _wait_for_event(rt, session_id: str, kind: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in rt.load_events(session_id):
            if evt.get("type") == kind:
                return evt["payload"]
        time.sleep(0.02)
    raise AssertionError(f"{kind} event never appeared")


def _idle_handle(**overrides) -> RunHandle:
    fields = {
        "cancel_event": threading.Event(),
        "started_at": "2026-07-18T00:00:00+00:00",
        "message_queue": queue.Queue(),
        "interrupt_step": threading.Event(),
    }
    fields.update(overrides)
    return RunHandle(**fields)


# ---------------------------------------------------------------------------
# Startup registration + capability-gated binding
# ---------------------------------------------------------------------------


def test_question_dispatcher_registered_at_api_startup():
    from mewbo_core.ask_user import QuestionDispatcher

    assert QuestionDispatcher.available() is True


def test_query_with_ask_user_capability_binds_tool(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.ask_user import AskUserQuestionTool

    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or "sid:r1"
    )

    sid = c.post("/api/sessions", json={}, headers=_headers()).get_json()["session_id"]
    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": dict(_QUESTIONS_CONTEXT)},
        headers=_headers(),
    )
    assert resp.status_code == 202
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], AskUserQuestionTool)


def test_query_without_capability_binds_nothing(client, monkeypatch):
    c, rt, backend = client
    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or "sid:r1"
    )

    sid = c.post("/api/sessions", json={}, headers=_headers()).get_json()["session_id"]
    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"client_capabilities": ["wiki"]}},
        headers=_headers(),
    )
    assert resp.status_code == 202
    assert captured.get("extra_session_tools") == []


def test_message_reengage_rebuilds_tool_from_persisted_context(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.ask_user import AskUserQuestionTool

    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or f"{kw['session_id']}:r1"
    )

    sid = c.post(
        "/api/sessions",
        json={"context": dict(_QUESTIONS_CONTEXT)},
        headers=_headers(),
    ).get_json()["session_id"]
    resp = c.post(
        f"/api/sessions/{sid}/message", json={"text": "again"}, headers=_headers()
    )
    assert resp.status_code == 200
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], AskUserQuestionTool)


# ---------------------------------------------------------------------------
# QuestionPendingCalls — registry semantics
# ---------------------------------------------------------------------------


class TestQuestionPendingCalls:
    def _registry(self):
        from mewbo_api.ask_user import QuestionPendingCalls

        return QuestionPendingCalls()

    def test_resolve_happy_path_sets_event(self):
        reg = self._registry()
        event = reg.create("s", "c", "tok", _ARGS)
        outcome, _ = reg.resolve(
            "s",
            "c",
            "tok",
            [QuestionAnswerItem(selected_indexes=[0]), QuestionAnswerItem(text="no")],
            answered_via="console",
        )
        assert outcome == "ok"
        assert event.is_set()
        entry = reg.take("s", "c")
        assert entry is not None
        assert entry.answered_via == "console"
        assert entry.answers[0].selected_indexes == [0]

    def test_unknown_and_bad_token(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        assert reg.resolve("s", "nope", "tok", [], answered_via=None)[0] == "not_found"
        assert (
            reg.resolve("s", "c", "wrong", [QuestionAnswerItem(text="x")], answered_via=None)[0]
            == "bad_token"
        )

    def test_double_resolve_conflicts(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        answers = [QuestionAnswerItem(text="a"), QuestionAnswerItem(text="b")]
        assert reg.resolve("s", "c", "tok", answers, answered_via=None)[0] == "ok"
        assert reg.resolve("s", "c", "tok", answers, answered_via=None)[0] == "conflict"

    @pytest.mark.parametrize(
        "bad_answers",
        [
            [QuestionAnswerItem(text="only one")],  # count mismatch
            [QuestionAnswerItem(selected_indexes=[5]), QuestionAnswerItem(text="x")],  # OOB
            [QuestionAnswerItem(selected_indexes=[0, 1]), QuestionAnswerItem(text="x")],  # arity
            [QuestionAnswerItem(text="x"), QuestionAnswerItem(selected_indexes=[0])],  # free-text q
        ],
    )
    def test_semantically_invalid_answers_do_not_consume(self, bad_answers):
        reg = self._registry()
        event = reg.create("s", "c", "tok", _ARGS)
        outcome, detail = reg.resolve("s", "c", "tok", bad_answers, answered_via=None)
        assert outcome == "invalid" and detail
        assert not event.is_set()
        # Still answerable with a correct body afterwards.
        good = [QuestionAnswerItem(selected_indexes=[1]), QuestionAnswerItem(text="ok")]
        assert reg.resolve("s", "c", "tok", good, answered_via=None)[0] == "ok"

    def test_withdraw_unanswered_removes_entry(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        assert reg.withdraw("s", "c") is True
        assert (
            reg.resolve("s", "c", "tok", [QuestionAnswerItem(text="x")], answered_via=None)[0]
            == "not_found"
        )

    def test_withdraw_after_resolve_loses_the_race(self):
        """The race guard: an answer that landed first must WIN — withdraw
        returns False and the entry (with the user's answers) survives for
        the dispatcher's read."""
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        answers = [QuestionAnswerItem(text="a"), QuestionAnswerItem(text="b")]
        assert reg.resolve("s", "c", "tok", answers, answered_via="aura")[0] == "ok"
        assert reg.withdraw("s", "c") is False
        entry = reg.take("s", "c")
        assert entry is not None and entry.answered_via == "aura"

    def test_take_unconsumed_returns_none(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        assert reg.take("s", "c") is None


# ---------------------------------------------------------------------------
# Full dispatch round trip — no LLM
# ---------------------------------------------------------------------------


def test_full_answer_round_trip_via_route(client):
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    dispatcher = ApiQuestionDispatcher(runtime=rt)

    result_box: dict = {}

    def _run_dispatch():
        result_box["result"] = asyncio.run(dispatcher.dispatch(session_id, _ARGS))

    thread = threading.Thread(target=_run_dispatch)
    thread.start()
    try:
        payload = _wait_for_event(rt, session_id, "user_question")
        assert payload["questions"][0]["header"] == "Scope"
        assert payload["questions"][0]["options"][0]["label"] == "Root only"
        call_id, token = payload["call_id"], payload["call_token"]
        url = f"/api/sessions/{session_id}/questions/{call_id}/answer"

        # Wrong token → 403; unknown call → 404; malformed body → 400.
        resp = c.post(
            url,
            json={"call_token": "wrong", "answers": [{"text": "x"}, {"text": "y"}]},
            headers=_headers(),
        )
        assert resp.status_code == 403
        resp = c.post(
            f"/api/sessions/{session_id}/questions/unknown/answer",
            json={"call_token": token, "answers": [{"text": "x"}, {"text": "y"}]},
            headers=_headers(),
        )
        assert resp.status_code == 404
        resp = c.post(url, json={"call_token": token}, headers=_headers())
        assert resp.status_code == 400
        resp = c.post(
            url,
            json={"call_token": token, "answers": [{"text": "x", "selected_indexes": [0]}]},
            headers=_headers(),
        )
        assert resp.status_code == 400  # XOR violated → item-level 400

        # Semantically wrong (count mismatch) → 422 with an actionable message.
        resp = c.post(
            url,
            json={"call_token": token, "answers": [{"text": "only one"}]},
            headers=_headers(),
        )
        assert resp.status_code == 422
        assert "expected 2 answer" in resp.get_json()["message"]

        # Correct → 200; the blocked dispatch resolves with the answers.
        resp = c.post(
            url,
            json={
                "call_token": token,
                "answers": [{"selected_indexes": [1]}, {"text": "ship it"}],
            },
            headers=_headers(**{"X-Mewbo-Surface": "console"}),
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"resolved": True}

        thread.join(timeout=5)
        assert not thread.is_alive()
        result = result_box["result"]
        assert result.outcome == "answered"
        assert result.answered_via == "console"
        assert result.answers[0].selected_indexes == [1]
        assert result.answers[1].text == "ship it"

        answered = _wait_for_event(rt, session_id, "user_question_answered")
        assert answered["call_id"] == call_id
        assert answered["outcome"] == "answered"
        assert answered["answered_via"] == "console"
        assert answered["answers"] == [
            {"selected_indexes": [1], "text": None},
            {"selected_indexes": None, "text": "ship it"},
        ]

        # The entry is gone once the dispatcher read it → a late POST 404s.
        resp = c.post(
            url,
            json={"call_token": token, "answers": [{"text": "x"}, {"text": "y"}]},
            headers=_headers(),
        )
        assert resp.status_code == 404
    finally:
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("handle_setup", "expected_outcome"),
    [
        (lambda h: h.message_queue.put("typed instead"), "declined"),
        (lambda h: h.interrupt_step.set(), "interrupted"),
        (lambda h: h.cancel_event.set(), "cancelled"),
    ],
)
def test_steering_signals_supersede_a_pending_question(
    client, monkeypatch, handle_setup, expected_outcome
):
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    handle = _idle_handle()
    handle_setup(handle)
    monkeypatch.setattr(rt, "active_run_handle", lambda sid: handle)

    dispatcher = ApiQuestionDispatcher(runtime=rt)
    result = asyncio.run(dispatcher.dispatch(session_id, _ARGS))
    assert result.outcome == expected_outcome
    assert result.answers == ()

    answered = _wait_for_event(rt, session_id, "user_question_answered")
    assert answered["outcome"] == expected_outcome
    assert answered["answers"] is None

    # Superseded ⇒ withdrawn: a late answer POST finds nothing pending.
    question = _wait_for_event(rt, session_id, "user_question")
    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"text": "late"}, {"text": "late"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 404


def test_steer_message_stays_queued_for_the_loop(client, monkeypatch):
    """Supersede must not EAT the steer — the message stays on the queue for
    the loop's own turn-top drain; the question result only tells the model
    the user answered by message."""
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    handle = _idle_handle()
    handle.message_queue.put("typed instead")
    monkeypatch.setattr(rt, "active_run_handle", lambda sid: handle)

    result = asyncio.run(ApiQuestionDispatcher(runtime=rt).dispatch(session_id, _ARGS))
    assert result.outcome == "declined"
    assert handle.message_queue.get_nowait() == "typed instead"
