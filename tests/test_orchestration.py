"""Tests for orchestration workflows."""

import json
import types

from langchain_core.runnables import RunnableLambda  # noqa: E402
from meeseeks_core import planning, task_master  # noqa: E402
from meeseeks_core.classes import (  # noqa: E402
    ActionStep,
    Plan,
    PlanStep,
    TaskQueue,
    set_available_tools,
)
from meeseeks_core.common import get_mock_speaker  # noqa: E402
from meeseeks_core.config import set_config_override  # noqa: E402
from meeseeks_core.context import ContextBuilder  # noqa: E402
from meeseeks_core.hooks import HookManager  # noqa: E402
from meeseeks_core.orchestrator import Orchestrator  # noqa: E402
from meeseeks_core.permissions import (  # noqa: E402
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
)
from meeseeks_core.planning import (  # noqa: E402
    Planner,
    PlanUpdater,
    ResponseSynthesizer,
    StepExecutor,
)
from meeseeks_core.reflection import StepReflector  # noqa: E402
from meeseeks_core.session_store import SessionStore  # noqa: E402
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec, load_registry  # noqa: E402
from meeseeks_tools.integration.aider_edit_blocks import AiderEditBlockTool  # noqa: E402


class Counter:
    """Simple counter helper for call tracking."""

    def __init__(self):
        """Initialize the counter."""
        self.count = 0

    def bump(self):
        """Increment the counter by one."""
        self.count += 1


def _edit_block(path: str, search: str, replace: str) -> str:
    return f"{path}\n```text\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n```\n"


def _types_in_order(events, expected):
    indices = []
    for value in expected:
        indices.append(next(i for i, event in enumerate(events) if event["type"] == value))
    assert indices == sorted(indices)


def make_task_queue(message: str) -> TaskQueue:
    """Build a minimal task queue with a single action step."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="get",
        tool_input=message,
    )
    return TaskQueue(action_steps=[step])


def make_plan(title: str, description: str | None = None) -> Plan:
    """Build a minimal plan with a single step."""
    return Plan(
        steps=[
            PlanStep(
                title=title,
                description=description or title,
            )
        ]
    )


def test_orchestrate_session_completes(monkeypatch, tmp_path):
    """Return a completed task queue when execution succeeds."""
    generate_calls = Counter()
    run_calls = Counter()
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        generate_calls.bump()
        return make_plan("Say hi")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="dummy_tool",
            args="say hi",
            response=None,
        )

    def fake_run(_self, _session_id, task_queue, *_args, **_kwargs):
        run_calls.bump()
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="done")
        task_queue.task_result = "done"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "done")

    task_queue = task_master.orchestrate_session(
        "hello",
        max_iters=3,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert task_queue.task_result == "done"
    assert generate_calls.count == 1
    assert run_calls.count == 1


def test_orchestrator_creates_session_when_missing(monkeypatch, tmp_path):
    """Create a new session when no session_id is provided."""
    session_store = SessionStore(root_dir=str(tmp_path))
    registry = ToolRegistry()

    monkeypatch.setattr(Planner, "generate", lambda *_a, **_k: Plan(steps=[]))
    monkeypatch.setattr(
        Orchestrator, "_run_action_plan", lambda *_a, **_k: TaskQueue(action_steps=[])
    )
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    orchestrator = Orchestrator(
        session_store=session_store,
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=lambda *_a, **_k: True,
        hook_manager=HookManager(),
    )
    orchestrator.run("hello", max_iters=1, session_id=None)
    assert session_store.list_sessions()


def test_generate_action_plan_omits_disabled_tools(monkeypatch):
    """Ensure prompt does not advertise disabled tools."""
    set_config_override({"home_assistant": {"enabled": False}})
    registry = load_registry()

    def _fake_model(messages):
        if hasattr(messages, "to_messages"):
            messages = messages.to_messages()
        combined = "\n".join(
            message.content for message in messages if getattr(message, "content", None)
        )
        assert "Available tools:\n- home_assistant_tool" not in combined
        payload = {"steps": []}
        return json.dumps(payload)

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    plan = task_master.generate_action_plan(
        "hi",
        tool_registry=registry,
    )
    assert plan.steps == []


def test_generate_action_plan_plan_mode_includes_all_tools(monkeypatch):
    """Expose all tools and avoid extra guidance in plan mode."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="plan_tool",
            name="Plan Tool",
            description="Safe tool for planning.",
            factory=lambda: None,
            metadata={"plan_safe": True},
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mut_tool",
            name="Mutating Tool",
            description="Mutating tool.",
            factory=lambda: None,
        )
    )

    def _fake_model(messages):
        if hasattr(messages, "to_messages"):
            messages = messages.to_messages()
        combined = "\n".join(
            message.content for message in messages if getattr(message, "content", None)
        )
        assert "plan_tool" in combined
        assert "mut_tool" in combined
        assert "Tool guidance:" not in combined
        return json.dumps({"steps": []})

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    plan = task_master.generate_action_plan(
        "make a plan",
        tool_registry=registry,
        mode="plan",
    )
    assert plan.steps == []


def test_orchestrate_session_marks_incomplete_on_failure(monkeypatch, tmp_path):
    """Mark completion as incomplete when a tool step fails."""
    generate_calls = Counter()
    run_calls = Counter()
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        generate_calls.bump()
        return Plan(
            steps=[
                PlanStep(title="Run dummy tool", description="Execute the tool."),
                PlanStep(title="Respond", description="Summarize the outcome."),
            ]
        )

    def fake_decide(*_args, **_kwargs):
        step = _args[2] if len(_args) > 2 else None
        if isinstance(step, PlanStep) and step.title == "Respond":
            return types.SimpleNamespace(
                decision="respond",
                tool_id=None,
                args=None,
                response="Failed to complete the tool step.",
            )
        return types.SimpleNamespace(
            decision="tool",
            tool_id="dummy_tool",
            args="payload",
            response=None,
        )

    def fake_run(_self, _session_id, task_queue, *_args, **_kwargs):
        run_calls.bump()
        task_queue.action_steps[0].result = None
        task_queue.task_result = "ERROR: boom"
        task_queue.last_error = "dummy_tool (get) failed: boom"
        return task_queue

    captured = {}

    def fake_update(*_args, **kwargs):
        captured["last_result"] = kwargs.get("last_result")
        return kwargs.get("remaining_steps", [])

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)
    monkeypatch.setattr(PlanUpdater, "update", fake_update)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        return_state=True,
    )

    assert task_queue.task_result == "Failed to complete the tool step."
    assert generate_calls.count == 1
    assert run_calls.count == 1
    assert state.done_reason == "completed"
    assert "ERROR: boom" in (captured.get("last_result") or "")


def test_orchestrate_session_edit_block_success_records_events(monkeypatch, tmp_path):
    """Run an edit-block tool and assert event ordering + payloads."""
    monkeypatch.setattr("meeseeks_core.session_store._utc_now", lambda: "2024-01-01T00:00:00+00:00")
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = "sess-edit-success"
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="aider_edit_block_tool",
            name="Aider Edit Blocks",
            description="Apply Aider-style SEARCH/REPLACE blocks to files.",
            factory=lambda: AiderEditBlockTool(),
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Apply edit block")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="aider_edit_block_tool",
            args={
                "content": _edit_block(
                    "hello.txt",
                    "alpha\n...\ngamma\n",
                    "alpha\n...\ndelta\n",
                ),
                "root": str(tmp_path),
            },
            response=None,
        )

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "done")

    task_queue = task_master.orchestrate_session(
        "apply edit",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        approval_callback=lambda *_a, **_k: True,
    )

    assert task_queue.task_result == "done"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ndelta\n"

    events = session_store.load_transcript(session_id)
    _types_in_order(events, ["user", "action_plan", "tool_result", "completion"])
    tool_event = next(event for event in events if event["type"] == "tool_result")
    assert tool_event["payload"]["tool_id"] == "aider_edit_block_tool"
    assert tool_event["payload"]["success"] is True
    assert "kind': 'diff'" in tool_event["payload"]["result"]


def test_orchestrate_session_edit_block_input_error_marks_incomplete(monkeypatch, tmp_path):
    """Mark completion incomplete on edit-block input errors without disabling the tool."""
    monkeypatch.setattr("meeseeks_core.session_store._utc_now", lambda: "2024-01-01T00:00:00+00:00")
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = "sess-edit-error"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="aider_edit_block_tool",
            name="Aider Edit Blocks",
            description="Apply Aider-style SEARCH/REPLACE blocks to files.",
            factory=lambda: AiderEditBlockTool(),
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Apply invalid edit block")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="aider_edit_block_tool",
            args={"content": "no edits here", "root": str(tmp_path)},
            response=None,
        )

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "apply edit",
        max_iters=2,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        approval_callback=lambda *_a, **_k: True,
        return_state=True,
    )

    assert state.done_reason == "incomplete"
    assert task_queue.last_error is not None
    spec = registry.get_spec("aider_edit_block_tool")
    assert spec is not None
    assert spec.enabled is True
    events = session_store.load_transcript(session_id)
    _types_in_order(events, ["user", "action_plan", "tool_result", "completion"])
    tool_event = next(event for event in events if event["type"] == "tool_result")
    assert tool_event["payload"]["success"] is False


def test_orchestrate_session_plan_mode_no_replan(monkeypatch, tmp_path):
    """Plan mode should not replan after a failure."""
    generate_calls = Counter()
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        generate_calls.bump()
        return make_plan("Plan it")

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "make a plan for this",
        max_iters=3,
        session_id=session_id,
        session_store=session_store,
        return_state=True,
        mode="plan",
    )

    assert generate_calls.count == 1
    assert state.done is True
    assert state.done_reason == "planned"
    events = session_store.load_transcript(session_id)
    completion = next(event for event in events if event["type"] == "completion")
    assert completion["payload"].get("done_reason") == "planned"


def test_orchestrate_session_cancels_before_step(monkeypatch, tmp_path):
    """Cancel before running the first step."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Do work")

    def fake_decide(*_args, **_kwargs):
        raise AssertionError("step executor should not run on cancel")

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "cancel",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        should_cancel=lambda: True,
        return_state=True,
    )

    assert task_queue is not None
    assert state.done_reason == "canceled"
    events = session_store.load_transcript(session_id)
    completion = next(event for event in events if event["type"] == "completion")
    assert completion["payload"]["done_reason"] == "canceled"


def test_orchestrate_session_empty_response_marks_incomplete(monkeypatch, tmp_path):
    """Handle respond decisions that omit a response."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        return make_plan("Respond")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(decision="respond", tool_id=None, args=None, response=None)

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        return_state=True,
    )

    assert state.done_reason == "incomplete"
    assert "empty response" in (task_queue.task_result or "")


def test_orchestrate_session_invalid_tool_id(monkeypatch, tmp_path):
    """Mark incomplete when step executor returns a disallowed tool."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="allowed_tool",
            name="Allowed Tool",
            description="Allowed tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Run tool")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="missing_tool",
            args="payload",
            response=None,
        )

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        return_state=True,
    )

    assert state.done_reason == "incomplete"
    assert "not allowed" in (task_queue.task_result or "")


def test_orchestrate_session_coerces_tool_args(monkeypatch, tmp_path):
    """Normalize missing and non-string args before tool execution."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        return Plan(
            steps=[
                PlanStep(title="Step 1", description="First"),
                PlanStep(title="Step 2", description="Second"),
            ]
        )

    decisions = iter(
        [
            types.SimpleNamespace(
                decision="tool",
                tool_id="dummy_tool",
                args=None,
                response=None,
            ),
            types.SimpleNamespace(
                decision="tool",
                tool_id="dummy_tool",
                args=["a", "b"],
                response=None,
            ),
        ]
    )

    def fake_decide(*_args, **_kwargs):
        return next(decisions)

    captured = []

    def fake_run(_self, _session_id, task_queue, *_args, **_kwargs):
        captured.append(task_queue.action_steps[0].tool_input)
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)

    def fake_update(*_args, **kwargs):
        return kwargs.get("remaining_steps", [])

    monkeypatch.setattr(PlanUpdater, "update", fake_update)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "hello",
        max_iters=2,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert captured == ["", "['a', 'b']"]


def test_orchestrate_session_invalid_decision(monkeypatch, tmp_path):
    """Surface invalid step decisions as errors."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        return make_plan("Do it")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(decision="unknown", tool_id=None, args=None, response=None)

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        return_state=True,
    )

    assert state.done_reason == "incomplete"
    assert "Invalid step decision" in (task_queue.task_result or "")


def test_orchestrate_session_cancels_after_step(monkeypatch, tmp_path):
    """Cancel after completing a step."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Run tool")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="dummy_tool",
            args="payload",
            response=None,
        )

    def fake_run(_self, _session_id, task_queue, *_args, **_kwargs):
        task_queue.task_result = "ok"
        return task_queue

    cancel_calls = {"count": 0}

    def should_cancel():
        cancel_calls["count"] += 1
        return cancel_calls["count"] > 1

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        should_cancel=should_cancel,
        return_state=True,
    )

    assert task_queue.task_result == "ok"
    assert state.done_reason == "canceled"


def test_orchestrate_session_exception_before_task_queue(monkeypatch, tmp_path):
    """Create a fallback task queue when orchestration fails early."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        raise RuntimeError("planner boom")

    monkeypatch.setattr(Planner, "generate", fake_generate)

    task_queue = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
    )

    assert task_queue.last_error == "planner boom"
    events = session_store.load_transcript(session_id)
    completion = next(event for event in events if event["type"] == "completion")
    assert completion["payload"]["done_reason"] == "error"


def test_expand_tool_ids_returns_empty_set():
    """Keep empty tool selections unchanged."""
    assert Orchestrator._expand_tool_ids(set(), []) == set()


def test_orchestrate_session_emits_completion_on_exception(monkeypatch, tmp_path):
    """Always emit completion when orchestration raises."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Run dummy tool")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="dummy_tool",
            args="payload",
            response=None,
        )

    def fake_run(_self, *_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)

    task_queue = task_master.orchestrate_session(
        "trigger error",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert task_queue.last_error == "boom"
    events = session_store.load_transcript(session_id)
    completion = next(event for event in events if event["type"] == "completion")
    assert completion["payload"]["done_reason"] == "error"
    assert completion["payload"]["error"] == "boom"


def test_orchestrate_session_mcp_missing_required_marks_incomplete(monkeypatch, tmp_path):
    """Mark completion incomplete when MCP schema requirements are violated."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    registry = ToolRegistry()

    class DummyTool:
        def run(self, _step):
            return get_mock_speaker()(content="ok")

    registry.register(
        ToolSpec(
            tool_id="mcp_bad_schema",
            name="Bad schema tool",
            description="MCP tool with strict schema",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Call MCP tool with bad args")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="mcp_bad_schema",
            args={"foo": "bar", "baz": "qux"},
            response=None,
        )

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)
    set_config_override({"context": {"selection_enabled": False}})

    policy = PermissionPolicy(
        rules=[],
        default_by_operation={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ALLOW},
        default_decision=PermissionDecision.ALLOW,
    )

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        permission_policy=policy,
        approval_callback=lambda *_args: True,
        return_state=True,
    )

    assert state.done_reason == "incomplete"
    assert task_queue.last_error is not None


def test_run_action_plan_records_last_error():
    """Record the most recent tool failure for replanning."""
    set_available_tools(["boom_tool"])

    class BoomTool:
        def run(self, step):
            raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="boom_tool",
            name="Boom",
            description="Boom",
            factory=lambda: BoomTool(),
        )
    )
    step = ActionStep(
        tool_id="boom_tool",
        operation="get",
        tool_input="go",
    )
    queue = TaskQueue(action_steps=[step])
    task_master.run_action_plan(queue, tool_registry=registry)
    assert queue.last_error is not None
    assert "boom_tool" in queue.last_error


def test_run_action_plan_missing_tool_records_last_error():
    """Capture failures when a tool is missing from the registry."""
    registry = ToolRegistry()
    step = ActionStep(
        tool_id="missing_tool",
        operation="get",
        tool_input="payload",
    )
    queue = TaskQueue(action_steps=[step])
    task_master.run_action_plan(queue, tool_registry=registry)
    assert queue.last_error is not None
    assert "tool not available" in queue.last_error


def test_run_action_plan_coerces_mcp_string_payload():
    """Coerce MCP payloads while handling array and scalar conversions."""
    captured = []

    class DummyTool:
        def run(self, step):
            captured.append(step.tool_input)
            return get_mock_speaker()(content="ok")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="mcp_array_tool",
            name="Array MCP Tool",
            description="Array schema tool",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "array", "items": {"type": "string"}}},
                }
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_string_tool",
            name="String MCP Tool",
            description="String schema tool",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_bad_tool",
            name="Bad MCP Tool",
            description="Unsupported payload type",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
    )
    steps = [
        ActionStep(
            tool_id="mcp_array_tool",
            operation="get",
            tool_input={"foo": "value"},
        ),
        ActionStep(
            tool_id="mcp_string_tool",
            operation="get",
            tool_input={"foo": ["value"]},
        ),
        ActionStep.construct(
            tool_id="mcp_bad_tool",
            operation="get",
            tool_input=["bad"],
        ),
    ]
    queue = TaskQueue(action_steps=steps)
    task_master.run_action_plan(queue, tool_registry=registry)
    assert captured == [{"query": ["value"]}, {"query": "value"}]
    assert queue.last_error is not None
    assert "Unsupported tool_input type" in queue.last_error


def test_orchestrate_session_passes_summary(monkeypatch, tmp_path):
    """Pass stored summaries into action plan generation."""
    captured = {}
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    session_store.save_summary(session_id, "previous summary")

    def fake_generate(_self, _query, *_args, **kwargs):
        context = kwargs.get("context")
        captured["summary"] = context.summary if context else None
        return Plan(steps=[])

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
    )

    assert captured["summary"] == "previous summary"


def test_response_synthesis_helpers(monkeypatch):
    """Exercise tool output collection and synthesis defaults."""
    queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="tool",
                operation="get",
                tool_input="x",
                result=None,
            )
        ]
    )
    assert Orchestrator._collect_tool_outputs(queue) == []
    queue.last_error = "tool failed"
    assert Orchestrator._collect_tool_outputs(queue) == ["ERROR: tool failed"]
    assert Orchestrator._should_synthesize_response(TaskQueue(action_steps=[])) is True

    def _fake_model(_inputs):
        return get_mock_speaker()(content="synthesized")

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )
    result = ResponseSynthesizer(None).synthesize(
        user_query="hi",
        tool_outputs=["output"],
        model_name=None,
        context=None,
    )
    assert result == "synthesized"


def test_serialize_plan_step_includes_title_description():
    """Serialize plan steps into action plan payloads."""
    step = PlanStep(title="Turn on lights", description="Use HA to turn on the lights.")
    payload = Orchestrator._serialize_plan_step(step)
    assert payload["title"] == "Turn on lights"
    assert payload["description"] == "Use HA to turn on the lights."


def test_orchestrate_session_updates_summary_on_memory_keyword(monkeypatch, tmp_path):
    """Update summary immediately when user asks to remember something."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        return Plan(steps=[])

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "Remember these numbers 12345",
        session_id=session_id,
        session_store=session_store,
    )

    summary = session_store.load_summary(session_id)
    assert summary is not None
    assert "Remember these numbers 12345" in summary


def test_orchestrate_session_passes_recent_events(monkeypatch, tmp_path):
    """Pass recent events into action plan generation."""
    captured = {}
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    session_store.append_event(
        session_id,
        {"type": "user", "payload": {"text": "Earlier message"}},
    )

    def fake_generate(_self, _query, *_args, **kwargs):
        context = kwargs.get("context")
        captured["recent_events"] = context.recent_events if context else None
        return Plan(steps=[])

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "hello",
        session_id=session_id,
        session_store=session_store,
    )

    recent = captured.get("recent_events") or []
    assert any(event.get("type") == "user" for event in recent)


def test_orchestrate_session_records_mcp_tool_result(monkeypatch, tmp_path):
    """Record MCP tool results into the session transcript."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    class FakeMCPTool:
        def run(self, step):
            return get_mock_speaker()(content=f"fake:{step.tool_input}")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="mcp_fake_search",
            name="Fake MCP Search",
            description="Fake MCP tool for tests",
            factory=lambda: FakeMCPTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Search for Krishnakanth")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="mcp_fake_search",
            args="Who is Krishnakanth?",
            response=None,
        )

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "search",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    events = session_store.load_transcript(session_id)
    tool_events = [event for event in events if event.get("type") == "tool_result"]
    assert tool_events
    payload = tool_events[-1]["payload"]
    assert payload["tool_id"] == "mcp_fake_search"
    assert payload["tool_input"] == {"query": "Who is Krishnakanth?"}
    assert payload["result"] == "fake:{'query': 'Who is Krishnakanth?'}"


def test_orchestrate_session_synthesizes_response(monkeypatch, tmp_path):
    """Synthesize a response after tool execution when no talk-to-user step."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    class DummyTool:
        def run(self, _step):
            return get_mock_speaker()(content="tool output")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="mcp_dummy",
            name="Dummy MCP",
            description="Dummy tool",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Call dummy tool")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="mcp_dummy",
            args="hello",
            response=None,
        )

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "final reply")

    task_queue = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert task_queue.task_result == "final reply"
    events = session_store.load_transcript(session_id)
    assert any(
        event.get("type") == "assistant" and event.get("payload", {}).get("text") == "final reply"
        for event in events
    )


def test_orchestrate_session_context_selection(monkeypatch, tmp_path):
    """Trigger context selection when token budget crosses threshold."""
    captured = {}
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    session_store.append_event(
        session_id,
        {"type": "user", "payload": {"text": "Old context"}},
    )
    session_store.append_event(
        session_id,
        {"type": "tool_result", "payload": {"result": "Old tool result"}},
    )

    def fake_select(_self, events, user_query, model_name):
        captured["selected"] = events[:1]
        return events[:1]

    def fake_generate(_self, _query, *_args, **kwargs):
        context = kwargs.get("context")
        captured["selected_events"] = context.selected_events if context else None
        return Plan(steps=[])

    set_config_override({"context": {"selection_threshold": 0.0, "recent_event_limit": 1}})
    monkeypatch.setattr(ContextBuilder, "_select_context_events", fake_select)
    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "hello",
        session_id=session_id,
        session_store=session_store,
    )

    assert captured.get("selected_events") is not None
    assert captured["selected_events"] == captured["selected"]


def test_orchestrate_session_max_iters(monkeypatch, tmp_path):
    """Stop after max iteration count is reached."""
    generate_calls = Counter()
    run_calls = Counter()
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy Tool",
            description="Test tool",
            factory=lambda: None,
        )
    )

    def fake_generate(*_args, **_kwargs):
        generate_calls.bump()
        return make_plan("Run dummy tool")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="dummy_tool",
            args="payload",
            response=None,
        )

    def fake_run(_self, _session_id, task_queue, *_args, **_kwargs):
        run_calls.bump()
        task_queue.action_steps[0].result = None
        task_queue.task_result = "failed"
        task_queue.last_error = "dummy_tool (get) failed: boom"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        return_state=True,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert task_queue.task_result == "failed"
    assert state.done is True
    assert state.done_reason == "incomplete"
    assert generate_calls.count == 1
    assert run_calls.count == 1


def test_orchestrate_session_direct_response_short_circuits(monkeypatch, tmp_path):
    """Stop execution and skip plan updates when a step responds directly."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    plan = Plan(
        steps=[
            PlanStep(title="Step 1", description="Ask for context"),
            PlanStep(title="Step 2", description="Use tools"),
        ]
    )
    called = {"update": False, "synth": False}

    def fake_decide(_self, *_args, **_kwargs):
        return planning.StepDecision(decision="respond", response="Need more context.")

    def fake_update(*_args, **_kwargs):
        called["update"] = True
        return []

    def fake_synthesize(*_args, **_kwargs):
        called["synth"] = True
        return "synthesized"

    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(PlanUpdater, "update", fake_update)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", fake_synthesize)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=3,
        return_state=True,
        session_id=session_id,
        session_store=session_store,
        tool_registry=ToolRegistry(),
        initial_plan=plan,
    )

    assert state.done is True
    assert task_queue.task_result == "Need more context."
    assert called["update"] is False
    assert called["synth"] is False


def test_orchestrate_session_keeps_tools_when_selector_false(monkeypatch, tmp_path):
    """Keep tool visibility when the selector says no tools are required."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: None,
        )
    )
    plan = Plan(steps=[PlanStep(title="Step 1", description="Fetch info")])
    captured = {}

    def fake_generate(*_args, **_kwargs):
        return plan

    def fake_select(*_args, **_kwargs):
        return planning.ToolSelection(tool_required=False, tool_ids=[], rationale="none")

    def fake_decide(_self, *_args, **kwargs):
        captured["allowed"] = kwargs.get("allowed_tools")
        return planning.StepDecision(decision="respond", response="ok")

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(planning.ToolSelector, "select", fake_select)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)

    task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert captured.get("allowed")


def test_orchestrate_session_expands_web_read(monkeypatch, tmp_path):
    """Include web_url_read when web_search is selected."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="mcp_utils_internet_search_searxng_web_search",
            name="Web Search",
            description="Search the web.",
            factory=lambda: None,
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_utils_internet_search_web_url_read",
            name="Web Read",
            description="Read a web URL.",
            factory=lambda: None,
        )
    )
    captured = {}

    def fake_generate(*_args, **_kwargs):
        return Plan(steps=[PlanStep(title="Search", description="Find sources")])

    def fake_select(*_args, **_kwargs):
        return planning.ToolSelection(
            tool_required=True,
            tool_ids=["mcp_utils_internet_search_searxng_web_search"],
            rationale="search first",
        )

    def fake_decide(_self, *_args, **kwargs):
        captured["allowed"] = kwargs.get("allowed_tools")
        return planning.StepDecision(decision="respond", response="ok")

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(planning.ToolSelector, "select", fake_select)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)

    task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert any(
        spec.tool_id == "mcp_utils_internet_search_web_url_read"
        for spec in captured.get("allowed", [])
    )


def test_orchestrate_session_adds_web_tools_for_verify_steps(monkeypatch, tmp_path):
    """Add web search/read tools when plan requires open/verify steps."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: None,
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_utils_internet_search_web_url_read",
            name="Web Read",
            description="Read a web URL.",
            factory=lambda: None,
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_utils_internet_search_searxng_web_search",
            name="Web Search",
            description="Search the web.",
            factory=lambda: None,
        )
    )
    captured = {}

    def fake_generate(*_args, **_kwargs):
        return Plan(steps=[PlanStep(title="Open and verify sources", description="Read sources")])

    def fake_select(*_args, **_kwargs):
        return planning.ToolSelection(
            tool_required=True,
            tool_ids=["dummy_tool"],
            rationale="narrow",
        )

    def fake_decide(_self, *_args, **kwargs):
        captured["allowed"] = kwargs.get("allowed_tools")
        return planning.StepDecision(decision="respond", response="ok")

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(planning.ToolSelector, "select", fake_select)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)

    task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    allowed_ids = {spec.tool_id for spec in captured.get("allowed", [])}
    assert "mcp_utils_internet_search_web_url_read" in allowed_ids
    assert "mcp_utils_internet_search_searxng_web_search" in allowed_ids


def test_orchestrate_session_reflection_failure_synthesizes(monkeypatch, tmp_path):
    """Synthesize a graceful response when reflection blocks progress."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    class DummyTool:
        def run(self, _step):
            return get_mock_speaker()(content="tool output")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: DummyTool(),
        )
    )

    def fake_generate(*_args, **_kwargs):
        return make_plan("Get verified price")

    def fake_decide(*_args, **_kwargs):
        return types.SimpleNamespace(
            decision="tool",
            tool_id="dummy_tool",
            args="query",
            response=None,
        )

    class DummyReflection:
        status = "retry"
        notes = "Missing timestamp"
        revised_argument = None

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(StepExecutor, "decide", fake_decide)
    monkeypatch.setattr(
        StepReflector,
        "reflect",
        lambda *_args, **_kwargs: DummyReflection(),
    )
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "graceful fail")

    task_queue, state = task_master.orchestrate_session(
        "get price",
        max_iters=1,
        return_state=True,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
    )

    assert state.done is True
    assert state.done_reason == "incomplete"
    assert task_queue.last_error is not None
    assert task_queue.task_result == "graceful fail"
    events = session_store.load_transcript(session_id)
    assert any(
        event.get("type") == "assistant" and event.get("payload", {}).get("text") == "graceful fail"
        for event in events
    )


def test_orchestrator_max_iters_zero_marks_limit(monkeypatch, tmp_path):
    """Mark completion reason when max_iters is zero."""
    session_store = SessionStore(root_dir=str(tmp_path))
    registry = ToolRegistry()
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)
    orchestrator = Orchestrator(
        session_store=session_store,
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=lambda *_a, **_k: True,
        hook_manager=HookManager(),
    )
    task_queue, state = orchestrator.run(
        "hello",
        max_iters=0,
        initial_plan=make_plan("Do something"),
        return_state=True,
        session_id=session_store.create_session(),
    )
    assert task_queue.action_steps == []
    assert state.done is True
    assert state.done_reason == "max_steps_reached"


def test_resolve_mode_plan_trigger():
    """Switch to plan mode when plan keywords are present."""
    assert Orchestrator._resolve_mode("Make a plan for launch", None) == "plan"


def test_should_replan_blocks_permission_denied():
    """Avoid replanning when permission was denied."""
    task_queue = TaskQueue(action_steps=[])
    task_queue.last_error = "permission denied for tool"
    assert Orchestrator._should_replan(task_queue, 0, 3, mode="act") is False


def test_should_replan_blocks_tool_not_allowed():
    """Avoid replanning when tool not allowed error occurs."""
    task_queue = TaskQueue(action_steps=[])
    task_queue.last_error = "tool not allowed in plan mode"
    assert Orchestrator._should_replan(task_queue, 0, 3, mode="act") is False


def test_run_action_plan_plan_mode_filters_tools(monkeypatch, tmp_path):
    """Pass plan-safe tool ids to the action runner in plan mode."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="plan_tool",
            name="Plan Tool",
            description="Safe tool for planning.",
            factory=lambda: None,
            metadata={"plan_safe": True},
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mut_tool",
            name="Mutating Tool",
            description="Mutating tool.",
            factory=lambda: None,
        )
    )
    captured = {}

    class DummyRunner:
        def __init__(self, *args, **kwargs):
            captured["allowed"] = kwargs.get("allowed_tool_ids")

        def run(self, task_queue):
            return task_queue

    monkeypatch.setattr("meeseeks_core.orchestrator.ActionPlanRunner", DummyRunner)

    orchestrator = Orchestrator(
        session_store=SessionStore(root_dir=str(tmp_path)),
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=lambda *_a, **_k: True,
        hook_manager=HookManager(),
    )
    orchestrator._run_action_plan(
        session_id="session",
        task_queue=TaskQueue(action_steps=[]),
        mode="plan",
    )

    assert captured["allowed"] == {"plan_tool"}


def test_orchestrate_session_compact(tmp_path):
    """Compact session transcripts when limits are exceeded."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    session_store.append_event(
        session_id,
        {"type": "user", "payload": {"text": "hello"}},
    )

    task_queue, state = task_master.orchestrate_session(
        "/compact",
        return_state=True,
        session_id=session_id,
        session_store=session_store,
    )

    assert state.done is True
    assert state.done_reason == "compacted"
    assert task_queue.task_result is not None


class DummyTool:
    """Stub tool used for permission tests."""

    def __init__(self):
        """Initialize the dummy tool."""
        self.called_with = None

    def run(self, action_step):
        """Return a mocked tool response."""
        self.called_with = action_step.tool_input
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=f"ok:{action_step.tool_input}")


def test_run_action_plan_permission_denied():
    """Return permission denied when policy disallows execution."""
    registry = ToolRegistry()
    dummy_tool = DummyTool()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: dummy_tool,
        )
    )
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                tool_id="dummy_tool",
                operation="set",
                decision=PermissionDecision.DENY,
            )
        ],
        default_by_operation={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ASK},
        default_decision=PermissionDecision.ASK,
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="dummy_tool",
                operation="set",
                tool_input="payload",
            )
        ]
    )
    task_queue = task_master.run_action_plan(
        task_queue,
        tool_registry=registry,
        permission_policy=policy,
        approval_callback=lambda _: False,
    )
    assert dummy_tool.called_with is None
    assert "Permission denied" in (task_queue.task_result or "")


def test_run_action_plan_hooks_modify_input():
    """Allow hooks to modify tool inputs."""
    registry = ToolRegistry()
    dummy_tool = DummyTool()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: dummy_tool,
        )
    )
    policy = PermissionPolicy(
        rules=[],
        default_by_operation={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ALLOW},
        default_decision=PermissionDecision.ALLOW,
    )

    def pre_hook(step):
        step.tool_input = "updated"
        return step

    def post_hook(step, result):
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=f"post:{step.tool_input}")

    hooks = HookManager(pre_tool_use=[pre_hook], post_tool_use=[post_hook])
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="dummy_tool",
                operation="get",
                tool_input="original",
            )
        ]
    )
    task_queue = task_master.run_action_plan(
        task_queue,
        tool_registry=registry,
        permission_policy=policy,
        hook_manager=hooks,
    )
    assert dummy_tool.called_with == "updated"
    assert task_queue.task_result.endswith("post:updated")


def test_run_action_plan_disables_tool_on_error():
    """Disable tool when a runtime error occurs."""

    class BoomTool:
        def run(self, action_step):
            raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="boom_tool",
            name="Boom",
            description="Boom tool",
            factory=lambda: BoomTool(),
        )
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="boom_tool",
                operation="set",
                tool_input="go",
            )
        ]
    )
    task_master.run_action_plan(
        task_queue,
        tool_registry=registry,
        approval_callback=lambda _: True,
    )

    spec = next(
        spec for spec in registry.list_specs(include_disabled=True) if spec.tool_id == "boom_tool"
    )
    assert spec.enabled is False
    assert "Runtime error" in spec.metadata.get("disabled_reason", "")


def test_run_action_plan_reflection_blocks_progress(monkeypatch):
    """Stop execution when reflection requests a revision."""
    registry = ToolRegistry()
    dummy_tool = DummyTool()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: dummy_tool,
        )
    )

    class DummyReflection:
        status = "revise"
        notes = "Need to adjust"
        revised_argument = "updated"

    monkeypatch.setattr(
        StepReflector,
        "reflect",
        lambda *_args, **_kwargs: DummyReflection(),
    )

    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="dummy_tool",
                operation="set",
                tool_input="original",
            )
        ]
    )
    task_queue = task_master.run_action_plan(
        task_queue,
        tool_registry=registry,
        approval_callback=lambda _: True,
        model_name="gpt-3.5-turbo",
    )
    assert task_queue.action_steps[0].result is not None
    assert task_queue.action_steps[0].result.content == "ok:original"
    assert task_queue.last_error is not None
    assert "needs revision" in task_queue.last_error


def test_orchestrate_session_auto_compact(monkeypatch, tmp_path):
    """Auto-compact sessions based on token budget."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    set_config_override({"token_budget": {"auto_compact_threshold": 0.0}})

    def fake_generate(*_args, **_kwargs):
        return Plan(steps=[])

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)

    task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
    )

    assert session_store.load_summary(session_id) is not None
