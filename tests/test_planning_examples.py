"""Tests for planner example message wrappers."""

from langchain_core.messages import AIMessage, HumanMessage
from meeseeks_core import planning as planning_module
from meeseeks_core.classes import TaskQueue
from meeseeks_core.planning import Planner
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec


def test_planner_examples_are_wrapped():
    """Ensure planner examples are tagged as illustrative only."""
    messages = Planner._build_example_messages(["home_assistant_tool"])
    assert len(messages) == 4
    for message in messages:
        assert isinstance(message, HumanMessage | AIMessage)
        assert message.content.startswith(
            '<example desc="Illustrative only; not part of the live conversation">'
        )
        assert message.content.endswith("</example>")


def test_planner_examples_wrap_when_tools_missing():
    """Ensure example tags exist even when tool examples are empty."""
    messages = Planner._build_example_messages([])
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
            return TaskQueue(action_steps=[])

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
