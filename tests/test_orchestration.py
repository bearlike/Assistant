"""Tests for orchestration workflows."""

import json

from langchain_core.runnables import RunnableLambda  # noqa: E402
from meeseeks_core import planning, task_master  # noqa: E402
from meeseeks_core.action_runner import ActionPlanRunner  # noqa: E402
from meeseeks_core.classes import ActionStep, TaskQueue, set_available_tools  # noqa: E402
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
from meeseeks_core.planning import Planner, ResponseSynthesizer  # noqa: E402
from meeseeks_core.reflection import StepReflector  # noqa: E402
from meeseeks_core.session_store import SessionStore  # noqa: E402
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec, load_registry  # noqa: E402


class Counter:
    """Simple counter helper for call tracking."""

    def __init__(self):
        """Initialize the counter."""
        self.count = 0

    def bump(self):
        """Increment the counter by one."""
        self.count += 1


def make_task_queue(message: str) -> TaskQueue:
    """Build a minimal task queue with a single action step."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument=message,
    )
    return TaskQueue(action_steps=[step])


def test_orchestrate_session_completes(monkeypatch, tmp_path):
    """Return a completed task queue when execution succeeds."""
    generate_calls = Counter()
    run_calls = Counter()
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        generate_calls.bump()
        return make_task_queue("say hi")

    def fake_run(_self, task_queue):
        run_calls.bump()
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="done")
        task_queue.task_result = "done"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "done")

    task_queue = task_master.orchestrate_session(
        "hello",
        max_iters=3,
        session_id=session_id,
        session_store=session_store,
    )

    assert task_queue.task_result == "done"
    assert generate_calls.count == 1
    assert run_calls.count == 1


def test_orchestrator_creates_session_when_missing(monkeypatch, tmp_path):
    """Create a new session when no session_id is provided."""
    session_store = SessionStore(root_dir=str(tmp_path))
    registry = ToolRegistry()

    monkeypatch.setattr(Planner, "generate", lambda *_a, **_k: TaskQueue(action_steps=[]))
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
        payload = {"action_steps": []}
        return json.dumps(payload)

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    task_queue = task_master.generate_action_plan(
        "hi",
        tool_registry=registry,
    )
    assert task_queue.action_steps == []


def test_generate_action_plan_plan_mode_filters_tools(monkeypatch):
    """Expose only plan-safe tools and avoid extra guidance in plan mode."""
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
        assert "mut_tool" not in combined
        assert "Tool guidance:" not in combined
        return json.dumps({"action_steps": []})

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    task_queue = task_master.generate_action_plan(
        "make a plan",
        tool_registry=registry,
        mode="plan",
    )
    assert task_queue.action_steps == []


def test_orchestrate_session_replans_on_failure(monkeypatch, tmp_path):
    """Replan when an action plan fails once."""
    generate_calls = Counter()
    run_calls = Counter()
    captured = {}
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(_self, user_query, *_args, **_kwargs):
        generate_calls.bump()
        if generate_calls.count == 2:
            captured["query"] = user_query
        return make_task_queue("say hi")

    def fake_run(_self, task_queue):
        run_calls.bump()
        if run_calls.count == 1:
            task_queue.action_steps[0].result = None
            task_queue.task_result = "failed"
            task_queue.last_error = "home_assistant_tool (get) failed: boom"
        else:
            MockSpeaker = get_mock_speaker()
            task_queue.action_steps[0].result = MockSpeaker(content="ok")
            task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "ok")

    task_queue = task_master.orchestrate_session(
        "hello",
        max_iters=2,
        session_id=session_id,
        session_store=session_store,
    )

    assert task_queue.task_result == "ok"
    assert generate_calls.count == 2
    assert run_calls.count == 2
    assert "Last tool failure:" in captured["query"]


def test_orchestrate_session_plan_mode_no_replan(monkeypatch, tmp_path):
    """Plan mode should not replan after a failure."""
    generate_calls = Counter()
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(_self, *_args, **_kwargs):
        generate_calls.bump()
        return make_task_queue("plan it")

    def fake_run(_self, _session_id, task_queue, *_args, **_kwargs):
        task_queue.last_error = "tool not allowed in plan mode"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(Orchestrator, "_run_action_plan", fake_run)
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
    assert state.done_reason in {"blocked", "incomplete"}


def test_orchestrate_session_schema_replan_and_context(monkeypatch, tmp_path):
    """Exercise schema rendering, event formatting, and replan failures together."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    captured: dict[str, str] = {}

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
                    "properties": "bad",
                }
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_prop_bad",
            name="Prop bad tool",
            description="Tool with mixed schema properties",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "properties": {
                        "other": "bad",
                        "query": {"type": "string", "description": "Search query"},
                    }
                }
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_field_non_str",
            name="Non-str field tool",
            description="Schema with non-string field name",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={"schema": {"properties": {1: {"type": "string"}}}},
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_schema_bad",
            name="Schema bad tool",
            description="Schema is not a dict",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={"schema": "oops"},
        )
    )
    registry.register(
        ToolSpec(
            tool_id="mcp_no_preferred",
            name="No preferred fields",
            description="Schema without preferred fields",
            factory=lambda: DummyTool(),
            kind="mcp",
            metadata={
                "schema": {
                    "properties": {"alpha": {"type": "string"}, "beta": {"type": "string"}},
                }
            },
        )
    )

    session_store.append_event(
        session_id,
        {"type": "tool_result", "payload": {"action_argument": {"foo": "bar"}}},
    )
    session_store.append_event(
        session_id,
        {"type": "tool_result", "payload": {"action_argument": "plain"}},
    )

    call_count = Counter()

    def fake_model(messages):
        if hasattr(messages, "to_messages"):
            messages = messages.to_messages()
        call_count.bump()
        system = "\n".join(msg.content for msg in messages if getattr(msg, "content", None))
        if call_count.count == 1:
            captured["system"] = system
        else:
            captured["query"] = messages[-1].content
        if call_count.count == 1:
            payload = {
                "action_steps": [
                    {
                        "action_consumer": "mcp_bad_schema",
                        "action_type": "get",
                        "action_argument": {"foo": "bar", "baz": "qux"},
                    },
                    {
                        "action_consumer": "mcp_prop_bad",
                        "action_type": "get",
                        "action_argument": '{"query": "ok"}',
                    },
                    {
                        "action_consumer": "mcp_prop_bad",
                        "action_type": "get",
                        "action_argument": "{bad}",
                    },
                    {
                        "action_consumer": "mcp_schema_bad",
                        "action_type": "get",
                        "action_argument": "ok",
                    },
                    {
                        "action_consumer": "mcp_bad_schema",
                        "action_type": "get",
                        "action_argument": {"foo": "bar"},
                    },
                    {
                        "action_consumer": "mcp_no_preferred",
                        "action_type": "get",
                        "action_argument": "plain",
                    },
                ]
            }
        else:
            payload = {"action_steps": []}
        return json.dumps(payload)

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(fake_model),
    )
    monkeypatch.setattr(Orchestrator, "_should_synthesize_response", lambda *_a, **_k: False)
    set_config_override({"context": {"selection_enabled": False}})

    policy = PermissionPolicy(
        rules=[],
        default_by_action={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ALLOW},
        default_decision=PermissionDecision.ALLOW,
    )

    task_master.orchestrate_session(
        "hello",
        max_iters=2,
        session_id=session_id,
        session_store=session_store,
        tool_registry=registry,
        permission_policy=policy,
        approval_callback=lambda *_args: True,
    )

    assert '"foo": "bar"' in captured["system"]
    assert "query: string - Search query" in captured["system"]
    assert "Last tool failure:" in captured["query"]
    assert "Expected JSON object with fields" in captured["query"]


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
        action_consumer="boom_tool",
        action_type="get",
        action_argument="go",
    )
    queue = TaskQueue(action_steps=[step])
    task_master.run_action_plan(queue, tool_registry=registry)
    assert queue.last_error is not None
    assert "boom_tool" in queue.last_error


def test_run_action_plan_missing_tool_records_last_error():
    """Capture failures when a tool is missing from the registry."""
    registry = ToolRegistry()
    step = ActionStep(
        action_consumer="missing_tool",
        action_type="get",
        action_argument="payload",
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
            captured.append(step.action_argument)
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
            action_consumer="mcp_array_tool",
            action_type="get",
            action_argument={"foo": "value"},
        ),
        ActionStep(
            action_consumer="mcp_string_tool",
            action_type="get",
            action_argument={"foo": ["value"]},
        ),
        ActionStep.construct(
            action_consumer="mcp_bad_tool",
            action_type="get",
            action_argument=["bad"],
        ),
    ]
    queue = TaskQueue(action_steps=steps)
    task_master.run_action_plan(queue, tool_registry=registry)
    assert captured == [{"query": ["value"]}, {"query": "value"}]
    assert queue.last_error is not None
    assert "Unsupported action_argument type" in queue.last_error


def test_orchestrate_session_passes_summary(monkeypatch, tmp_path):
    """Pass stored summaries into action plan generation."""
    captured = {}
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    session_store.save_summary(session_id, "previous summary")

    def fake_generate(_self, _query, *_args, **kwargs):
        context = kwargs.get("context")
        captured["summary"] = context.summary if context else None
        return make_task_queue("say hi")

    def fake_run(_self, task_queue):
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="ok")
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "ok")

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
                action_consumer="tool",
                action_type="get",
                action_argument="x",
                result=None,
            )
        ]
    )
    assert Orchestrator._collect_tool_outputs(queue) == []
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


def test_serialize_action_step_includes_optional_fields():
    """Include optional metadata fields in serialized action payloads."""
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument="turn on",
        title="Turn on lights",
        objective="Illuminate the room",
        execution_checklist=["Use HA", "Target lights"],
        expected_output="Lights on",
    )
    payload = Orchestrator._serialize_action_step(step)
    assert payload["title"] == "Turn on lights"
    assert payload["objective"] == "Illuminate the room"
    assert payload["execution_checklist"] == ["Use HA", "Target lights"]
    assert payload["expected_output"] == "Lights on"


def test_orchestrate_session_updates_summary_on_memory_keyword(monkeypatch, tmp_path):
    """Update summary immediately when user asks to remember something."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()

    def fake_generate(*_args, **_kwargs):
        return make_task_queue("ok")

    def fake_run(_self, task_queue):
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="ok")
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "ok")

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
        return make_task_queue("ok")

    def fake_run(_self, task_queue):
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="ok")
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "ok")

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
            return get_mock_speaker()(content=f"fake:{step.action_argument}")

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
        step = ActionStep(
            action_consumer="mcp_fake_search",
            action_type="get",
            action_argument="Who is Krishnakanth?",
        )
        return TaskQueue(action_steps=[step])

    monkeypatch.setattr(Planner, "generate", fake_generate)
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
    assert payload["action_consumer"] == "mcp_fake_search"
    assert payload["action_argument"] == {"query": "Who is Krishnakanth?"}
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
        step = ActionStep(
            action_consumer="mcp_dummy",
            action_type="get",
            action_argument="hello",
        )
        return TaskQueue(action_steps=[step])

    monkeypatch.setattr(Planner, "generate", fake_generate)
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
        return make_task_queue("ok")

    def fake_run(_self, task_queue):
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="ok")
        task_queue.task_result = "ok"
        return task_queue

    set_config_override({"context": {"selection_threshold": 0.0, "recent_event_limit": 1}})
    monkeypatch.setattr(ContextBuilder, "_select_context_events", fake_select)
    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "ok")

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

    def fake_generate(*_args, **_kwargs):
        generate_calls.bump()
        return make_task_queue("say hi")

    def fake_run(_self, task_queue):
        run_calls.bump()
        task_queue.action_steps[0].result = None
        task_queue.task_result = "failed"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)

    task_queue, state = task_master.orchestrate_session(
        "hello",
        max_iters=1,
        return_state=True,
        session_id=session_id,
        session_store=session_store,
    )

    assert task_queue.task_result == "failed"
    assert state.done is True
    assert state.done_reason == "incomplete"
    assert generate_calls.count == 1
    assert run_calls.count == 1


def test_orchestrator_max_iters_zero_marks_limit(tmp_path):
    """Mark completion reason when max_iters is zero."""
    session_store = SessionStore(root_dir=str(tmp_path))
    registry = ToolRegistry()
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
        initial_task_queue=TaskQueue(action_steps=[]),
        return_state=True,
        session_id=session_store.create_session(),
    )
    assert task_queue.action_steps == []
    assert state.done is False
    assert state.done_reason == "max_iterations_reached"


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
        self.called_with = action_step.action_argument
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=f"ok:{action_step.action_argument}")


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
                action_type="set",
                decision=PermissionDecision.DENY,
            )
        ],
        default_by_action={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ASK},
        default_decision=PermissionDecision.ASK,
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                action_consumer="dummy_tool",
                action_type="set",
                action_argument="payload",
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
    """Allow hooks to modify action arguments."""
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
        default_by_action={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ALLOW},
        default_decision=PermissionDecision.ALLOW,
    )

    def pre_hook(step):
        step.action_argument = "updated"
        return step

    def post_hook(step, result):
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=f"post:{step.action_argument}")

    hooks = HookManager(pre_tool_use=[pre_hook], post_tool_use=[post_hook])
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                action_consumer="dummy_tool",
                action_type="get",
                action_argument="original",
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
                action_consumer="boom_tool",
                action_type="set",
                action_argument="go",
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
                action_consumer="dummy_tool",
                action_type="set",
                action_argument="original",
            )
        ]
    )
    task_queue = task_master.run_action_plan(
        task_queue,
        tool_registry=registry,
        approval_callback=lambda _: True,
        model_name="gpt-3.5-turbo",
    )
    assert task_queue.action_steps[0].result is None


def test_orchestrate_session_auto_compact(monkeypatch, tmp_path):
    """Auto-compact sessions based on token budget."""
    session_store = SessionStore(root_dir=str(tmp_path))
    session_id = session_store.create_session()
    set_config_override({"token_budget": {"auto_compact_threshold": 0.0}})

    def fake_generate(*_args, **_kwargs):
        return make_task_queue("say hi")

    def fake_run(_self, task_queue):
        MockSpeaker = get_mock_speaker()
        task_queue.action_steps[0].result = MockSpeaker(content="done")
        task_queue.task_result = "done"
        return task_queue

    monkeypatch.setattr(Planner, "generate", fake_generate)
    monkeypatch.setattr(ActionPlanRunner, "run", fake_run)
    monkeypatch.setattr(ResponseSynthesizer, "synthesize", lambda *_a, **_k: "done")

    task_master.orchestrate_session(
        "hello",
        max_iters=1,
        session_id=session_id,
        session_store=session_store,
    )

    assert session_store.load_summary(session_id) is not None
