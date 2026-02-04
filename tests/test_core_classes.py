"""Tests for core class behaviors."""

import os

import meeseeks_core.classes as classes
import pytest
from meeseeks_core.classes import ActionStep, TaskQueue, create_task_queue, set_available_tools


def test_action_step_normalization():
    """Normalize action step fields and tool identifiers."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        action_consumer="HOME_ASSISTANT_TOOL",
        action_type="SET",
        action_argument="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_consumer == "home_assistant_tool"
    assert queue.action_steps[0].action_type == "set"


def test_action_step_accepts_dict_argument():
    """Allow structured action arguments for schema-based tools."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument={"message": "hello"},
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_argument == {"message": "hello"}


def test_action_step_invalid_entries():
    """Normalize invalid tool/action entries to lower case."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        action_consumer="UNKNOWN_TOOL",
        action_type="GET",
        action_argument="hello",
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_consumer == "unknown_tool"
    assert queue.action_steps[0].action_type == "get"


def test_action_step_validation_logs_for_invalid_entries():
    """Trigger validation warnings for invalid action data."""
    set_available_tools(["home_assistant_tool"])
    step = ActionStep.construct(
        action_consumer="bad_tool",
        action_type="bad",
        action_argument=None,
    )
    queue = TaskQueue(action_steps=[step])
    assert queue.action_steps[0].action_consumer == "bad_tool"
    assert queue.action_steps[0].action_type == "bad"


def test_save_json(tmp_path, monkeypatch):
    """Write JSON payloads using the tool helper."""
    from meeseeks_core.classes import AbstractTool

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
            "action_consumer": "home_assistant_tool",
            "action_type": "get",
            "action_argument": "hello",
        }
    ]
    queue = create_task_queue(action_data=action_data, is_example=False)
    assert queue.action_steps[0].action_argument == "hello"
    examples = classes.get_task_master_examples(0, available_tools=["home_assistant_tool"])
    assert "action_steps" in examples
    with pytest.raises(ValueError):
        classes.get_task_master_examples(99, available_tools=["home_assistant_tool"])


def test_create_task_queue_requires_data():
    """Raise when action data is missing."""
    with pytest.raises(ValueError):
        create_task_queue(action_data=None)


def test_examples_skip_home_assistant_when_unavailable():
    """Ensure examples omit disabled tools."""
    examples = classes.get_task_master_examples(0, available_tools=[])
    assert "home_assistant_tool" not in examples


def test_examples_use_available_tools_by_default():
    """Use global available tools when not provided."""
    set_available_tools(["home_assistant_tool"])
    examples = classes.get_task_master_examples(0, available_tools=None)
    assert "home_assistant_tool" in examples


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
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool")

    tool = DummyTool()
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument="hello",
    )
    result = tool.run(step)
    assert result.content == "Not implemented yet."


def test_abstract_tool_cache_dir_missing(monkeypatch):
    """Raise when CACHE_DIR is unset."""

    class DummyTool(classes.AbstractTool):
        def __init__(self):
            super().__init__(name="Dummy", description="Test tool", use_llm=False)

    monkeypatch.delenv("CACHE_DIR", raising=False)
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
    monkeypatch.setenv("CACHE_DIR", str(cache_root))
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
    monkeypatch.setenv("CACHE_DIR", str(cache_root))
    tool = DummyTool()
    set_available_tools(["home_assistant_tool"])
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="hello",
    )
    assert tool.run(step).content == "Not implemented yet."
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="bad",
        action_argument="hello",
    )
    with pytest.raises(ValueError):
        tool.run(step)
