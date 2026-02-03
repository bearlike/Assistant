"""Tests for core class behaviors."""
import os

import pytest

import core.classes as classes
from core.classes import ActionStep, TaskQueue, create_task_queue, set_available_tools


def test_action_step_normalization():
    """Normalize action step fields and tool identifiers."""
    set_available_tools(["talk_to_user_tool", "home_assistant_tool"])
    step = ActionStep(
        action_consumer="TALK_TO_USER_TOOL",
        action_type="SET",
        action_argument="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_consumer == "talk_to_user_tool"
    assert queue.action_steps[0].action_type == "set"


def test_action_step_invalid_entries():
    """Normalize invalid tool/action entries to lower case."""
    set_available_tools(["talk_to_user_tool"])
    step = ActionStep(
        action_consumer="UNKNOWN_TOOL",
        action_type="GET",
        action_argument="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_consumer == "unknown_tool"
    assert queue.action_steps[0].action_type == "get"


def test_action_step_talk_to_user_get():
    """Allow talk-to-user get actions to pass through."""
    set_available_tools(["talk_to_user_tool"])
    step = ActionStep(
        action_consumer="talk_to_user_tool",
        action_type="get",
        action_argument="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_type == "get"


def test_save_json(tmp_path, monkeypatch):
    """Write JSON payloads using the tool helper."""
    from core.classes import AbstractTool

    class DummyTool(AbstractTool):
        def set_state(self, action_step=None):
            raise NotImplementedError

        def get_state(self, *args, **kwargs):
            raise NotImplementedError

    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    tool = DummyTool.__new__(DummyTool)
    tool.cache_dir = str(tmp_path)
    tool._save_json({"value": 1}, "data.json")
    assert os.path.exists(tmp_path / "data.json")


def test_create_task_queue_and_examples():
    """Create task queues and validate example lookup errors."""
    action_data = [
        {
            "action_consumer": "talk_to_user_tool",
            "action_type": "set",
            "action_argument": "hello",
        }
    ]
    queue = create_task_queue(action_data=action_data, is_example=False)
    assert queue.action_steps[0].action_argument == "hello"
    examples = classes.get_task_master_examples(
        0, available_tools=["talk_to_user_tool"]
    )
    assert "action_steps" in examples
    with pytest.raises(ValueError):
        classes.get_task_master_examples(99, available_tools=["talk_to_user_tool"])


def test_examples_skip_home_assistant_when_unavailable():
    """Ensure examples omit disabled tools."""
    examples = classes.get_task_master_examples(
        0, available_tools=["talk_to_user_tool"]
    )
    assert "home_assistant_tool" not in examples


def test_abstract_tool_init_and_run(monkeypatch, tmp_path):
    """Initialize AbstractTool and run placeholder output."""
    class DummyChatOpenAI:
        def __init__(self, **kwargs):
            if "temperature" in kwargs and kwargs["temperature"] is None:
                raise ValueError("temperature cannot be None")

    import sys
    import types

    module = types.ModuleType("langchain_openai")
    module.ChatOpenAI = DummyChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool")

    tool = DummyTool()
    step = ActionStep(
        action_consumer="talk_to_user_tool",
        action_type="set",
        action_argument="hello",
    )
    result = tool.run(step)
    assert result.content == "Not implemented yet."
