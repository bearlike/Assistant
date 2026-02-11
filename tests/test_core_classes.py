"""Tests for core class behaviors."""

import json
import os

import meeseeks_core.classes as classes
import pytest
from meeseeks_core.classes import ActionStep, TaskQueue, create_task_queue, set_available_tools
from meeseeks_core.config import set_config_override


def test_action_step_normalization():
    """Normalize action step fields and tool identifiers."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        tool_id="HOME_ASSISTANT_TOOL",
        operation="SET",
        tool_input="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].tool_id == "home_assistant_tool"
    assert queue.action_steps[0].operation == "set"


def test_action_step_accepts_dict_argument():
    """Allow structured tool inputs for schema-based tools."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="set",
        tool_input={"message": "hello"},
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].tool_input == {"message": "hello"}


def test_action_step_invalid_entries():
    """Normalize invalid tool/action entries to lower case."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        tool_id="UNKNOWN_TOOL",
        operation="GET",
        tool_input="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].tool_id == "unknown_tool"
    assert queue.action_steps[0].operation == "get"


def test_action_step_validation_logs_for_invalid_entries():
    """Trigger validation warnings for invalid action data."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep.construct(
        tool_id="bad_tool",
        operation="bad",
        tool_input=None,
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].tool_id == "bad_tool"
    assert queue.action_steps[0].operation == "bad"


def test_save_json(tmp_path, monkeypatch):
    """Write JSON payloads using the tool helper."""
    from meeseeks_core.classes import AbstractTool

    class DummyTool(AbstractTool):
        def set_state(self, action_step=None):
            raise NotImplementedError

        def get_state(self, *args, **kwargs):
            raise NotImplementedError

    set_config_override({"runtime": {"cache_dir": str(tmp_path)}})
    tool = DummyTool.__new__(DummyTool)
    tool.cache_dir = str(tmp_path)
    tool._save_json({"value": 1}, "data.json")
    assert os.path.exists(tmp_path / "data.json")


def test_create_task_queue_and_examples():
    """Create task queues and validate example lookup errors."""
    action_data = [
        {
            "tool_id": "home_assistant_tool",
            "operation": "get",
            "tool_input": "hello",
        }
    ]
    queue = create_task_queue(action_data=action_data, is_example=False)
    assert queue.action_steps[0].tool_input == "hello"
    examples = classes.get_task_master_examples(0, available_tools=["home_assistant_tool"])
    assert "steps" in examples
    with pytest.raises(ValueError):
        classes.get_task_master_examples(99, available_tools=["home_assistant_tool"])


def test_create_task_queue_requires_data():
    """Raise when action data is missing."""
    with pytest.raises(ValueError):
        create_task_queue(action_data=None)


def test_create_plan_requires_data():
    """Raise when plan data is missing."""
    with pytest.raises(ValueError):
        classes.create_plan(step_data=None)


def test_examples_skip_home_assistant_when_unavailable():
    """Ensure examples omit disabled tools."""
    examples = classes.get_task_master_examples(0, available_tools=[])
    payload = json.loads(examples)
    assert payload["steps"] == []


def test_examples_use_available_tools_by_default():
    """Use global available tools when not provided."""
    set_available_tools(["home_assistant_tool"])
    examples = classes.get_task_master_examples(0, available_tools=None)
    payload = json.loads(examples)
    assert payload["steps"]


def test_abstract_tool_init_and_run(monkeypatch, tmp_path):
    """Initialize AbstractTool and run placeholder output."""

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            if "temperature" in kwargs and kwargs["temperature"] is None:
                raise ValueError("temperature cannot be None")

    import sys
    import types

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)
    set_config_override({"runtime": {"cache_dir": str(tmp_path)}})

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool")

    tool = DummyTool()
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="set",
        tool_input="hello",
    )
    result = tool.run(step)
    assert result.content == "Not implemented yet."


def test_abstract_tool_cache_dir_missing(monkeypatch):
    """Raise when CACHE_DIR is unset."""

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool", use_llm=False)

    set_config_override({"runtime": {"cache_dir": ""}})
    with pytest.raises(ValueError):
        DummyTool()


def test_abstract_tool_rag_helpers(monkeypatch, tmp_path):
    """Load RAG documents from JSON files."""

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool", use_llm=False)

    class DummyLoader:
        def __init__(self, file_path, **_kwargs):
            self.file_path = file_path

        def load(self):
            return [{"path": self.file_path}]

    monkeypatch.setattr(classes, "JSONLoader", DummyLoader)
    cache_root = tmp_path / "cache-root"
    set_config_override({"runtime": {"cache_dir": str(cache_root)}})
    tool = DummyTool()
    tool._save_json([{"foo": "bar"}], "rag.json")
    docs = tool._load_rag_json("rag.json")
    assert docs
    docs = tool._load_rag_documents(["rag.json"])
    assert docs


def test_abstract_tool_run_variants(monkeypatch, tmp_path):
    """Execute get/set paths and invalid action types."""

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool", use_llm=False)

    cache_root = tmp_path / "cache-root"
    set_config_override({"runtime": {"cache_dir": str(cache_root)}})
    tool = DummyTool()
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="get",
        tool_input="hello",
    )
    assert tool.run(step).content == "Not implemented yet."
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="bad",
        tool_input="hello",
    )
    with pytest.raises(ValueError):
        tool.run(step)
