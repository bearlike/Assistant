"""Tests for planner example message wrappers."""

from contextlib import contextmanager

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from meeseeks_core import planning as planning_module
from meeseeks_core.classes import Plan
from meeseeks_core.planning import Planner
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec


def test_planner_examples_are_wrapped():
    """Ensure planner examples are tagged as illustrative only."""
    messages = Planner._build_example_messages(["home_assistant_tool"], mode="plan")
    assert len(messages) == 4
    for message in messages:
        assert isinstance(message, HumanMessage | AIMessage)
        assert message.content.startswith(
            '<example desc="Illustrative only; not part of the live conversation">'
        )
        assert message.content.endswith("</example>")


def test_planner_examples_wrap_when_tools_missing():
    """Ensure example tags exist even when tool examples are empty."""
    messages = Planner._build_example_messages([], mode="plan")
    assert len(messages) == 4
    for message in messages:
        assert message.content.startswith(
            '<example desc="Illustrative only; not part of the live conversation">'
        )
        assert message.content.endswith("</example>")


def test_planner_requires_tool_registry():
    """Require a tool registry for planning calls."""
    planner = Planner(tool_registry=None)
    try:
        planner.generate("hi", "gpt-4o")
    except ValueError as exc:
        assert "Tool registry" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing tool registry")


def test_planner_includes_langfuse_callbacks(monkeypatch):
    """Attach callbacks config when Langfuse handler is present."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy",
            name="Dummy Tool",
            description="Test",
            factory=lambda: object(),
        )
    )
    captured: dict[str, object] = {}

    class DummyChain:
        def __or__(self, _other):
            return self

        def invoke(self, *_args, **kwargs):
            captured["config"] = kwargs.get("config")
            return Plan(steps=[])

    class DummyPrompt:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def __or__(self, _other):
            return DummyChain()

    class DummyParser:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def get_format_instructions(self):
            return "format"

    monkeypatch.setattr(planning_module, "ChatPromptTemplate", DummyPrompt)
    monkeypatch.setattr(planning_module, "PydanticOutputParser", DummyParser)
    monkeypatch.setattr(planning_module, "build_chat_model", lambda **_k: object())
    monkeypatch.setattr(planning_module, "build_langfuse_handler", lambda **_k: object())

    planner = Planner(registry)
    planner.generate("hello", "gpt-4o")
    config = captured.get("config")
    assert config is not None
    assert "callbacks" in config


def test_planner_generate_uses_tool_specs_and_updates_span(monkeypatch):
    """Use provided tool specs and update langfuse span output."""
    registry = ToolRegistry()
    spec = ToolSpec(
        tool_id="dummy_tool",
        name="Dummy",
        description="Test",
        factory=lambda: object(),
    )
    planner = Planner(registry)
    captured: dict[str, object] = {}

    def fake_build(_prompt, _context, **kwargs):
        captured["tool_specs"] = kwargs.get("tool_specs")
        return "prompt"

    planner._prompt_builder.build = fake_build  # type: ignore[assignment]

    class DummySpan:
        def __init__(self):
            self.updates: list[dict[str, object]] = []

        def update_trace(self, **kwargs):
            self.updates.append(kwargs)

    dummy_span = DummySpan()

    @contextmanager
    def fake_span(_name):
        yield dummy_span

    monkeypatch.setattr(planning_module, "langfuse_trace_span", fake_span)
    monkeypatch.setattr(planning_module, "build_langfuse_handler", lambda **_k: None)

    def _fake_model(_inputs):
        return '{"steps": []}'

    monkeypatch.setattr(
        planning_module,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    plan = planner.generate("hello", "gpt-5.2", tool_specs=[spec])
    assert plan.steps == []
    assert captured["tool_specs"] == [spec]
    assert any("output" in update for update in dummy_span.updates)
