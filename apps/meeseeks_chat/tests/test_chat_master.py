"""Tests for the Streamlit chat UI helpers."""

# ruff: noqa: I001
# mypy: ignore-errors
import os
import types

from meeseeks_chat import chat_master
from meeseeks_core.classes import ActionStep, Plan, PlanStep, TaskQueue
from meeseeks_core.session_store import SessionStore


def _make_task_queue(action_steps):
    task_queue = TaskQueue(action_steps=action_steps)
    task_queue.human_message = "hello"
    return task_queue


def test_generate_action_plan_helper(monkeypatch):
    """Return a formatted action plan with the generated queue."""
    plan = Plan(steps=[PlanStep(title="Say hello", description="Respond to the user.")])

    def fake_generate(*args, **kwargs):
        return plan

    monkeypatch.setattr(chat_master, "generate_action_plan", fake_generate)
    plan_list, returned = chat_master.generate_action_plan_helper("hello")
    assert returned == plan
    assert plan_list == ["Say hello: Respond to the user."]


def test_run_action_plan_helper(monkeypatch, tmp_path):
    """Combine responses from a task queue execution."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    fake_state = types.SimpleNamespace(
        session_store=session_store,
        session_id=session_id,
    )
    monkeypatch.setattr(chat_master, "st", types.SimpleNamespace(session_state=fake_state))

    plan = Plan(steps=[PlanStep(title="Say hello", description="Respond to the user.")])

    captured = {}

    def fake_orchestrate(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        captured["session_store"] = kwargs.get("session_store")
        queue = _make_task_queue(
            [
                ActionStep(
                    tool_id="home_assistant_tool",
                    operation="get",
                    tool_input="hello",
                    result=type("Result", (), {"content": "ok"})(),
                )
            ]
        )
        queue.task_result = "ok"
        return queue

    monkeypatch.setattr(chat_master, "orchestrate_session", fake_orchestrate)
    response = chat_master.run_action_plan_helper(plan)
    assert response == "ok"
    assert captured["session_id"] == session_id
    assert captured["session_store"] is session_store


def test_main_flow(monkeypatch, tmp_path):
    """Exercise the main chat flow with stubbed Streamlit runtime."""

    class DummyContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSessionState(dict):
        def __getattr__(self, item):
            return self[item]

        def __setattr__(self, key, value):
            self[key] = value

    class FakeStreamlit:
        def __init__(self):
            self.session_state = FakeSessionState()

        def set_page_config(self, *args, **kwargs):
            return None

        def image(self, *args, **kwargs):
            return None

        def markdown(self, *args, **kwargs):
            return None

        def chat_message(self, *args, **kwargs):
            return DummyContext()

        def chat_input(self, *args, **kwargs):
            return "hello"

        def spinner(self, *args, **kwargs):
            return DummyContext()

        def expander(self, *args, **kwargs):
            return DummyContext()

        def caption(self, *args, **kwargs):
            return None

    class DummyMemory:
        def __init__(self, k=5):
            self.saved = []

        def save_context(self, inputs, outputs):
            self.saved.append((inputs, outputs))

    fake_st = FakeStreamlit()
    monkeypatch.setattr(chat_master, "st", fake_st)
    monkeypatch.setattr(chat_master, "ConversationBufferWindowMemory", DummyMemory)
    monkeypatch.setattr(chat_master.time, "sleep", lambda *_: None)
    chat_root = os.path.join(os.path.dirname(__file__), "..")
    monkeypatch.chdir(chat_root)

    def fake_generate(user_input):
        plan = Plan(steps=[PlanStep(title="Say hello", description="Respond to the user.")])
        return ["Say hello: Respond to the user."], plan

    monkeypatch.setattr(chat_master, "generate_action_plan_helper", fake_generate)
    monkeypatch.setattr(chat_master, "run_action_plan_helper", lambda *_: "ok")
    monkeypatch.setattr(chat_master, "SessionStore", lambda: SessionStore(root_dir=str(tmp_path)))

    chat_master.main()

    assert "messages" in fake_st.session_state
    assert any(msg["role"] == "assistant" for msg in fake_st.session_state["messages"])
