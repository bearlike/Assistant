"""Tests for intent-based tool scoping in the planner."""

import pytest
from langchain_core.runnables import RunnableLambda
from meeseeks_core import planning
from meeseeks_core.classes import PlanStep
from meeseeks_core.planning import Planner, PlanUpdater, StepExecutor, ToolSelector
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec


def _spec(tool_id: str, *, kind: str = "local") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description="test",
        factory=lambda: object(),
        kind=kind,
    )


def test_filter_specs_by_intent_prefers_matching_tools():
    """Filter tool specs to the intent-matched subset when possible."""
    planner = Planner(ToolRegistry())
    specs = [
        _spec("mcp_utils_internet_search_searxng_web_search", kind="mcp"),
        _spec("aider_read_file_tool", kind="local"),
        _spec("aider_edit_block_tool", kind="local"),
    ]
    filtered = planner._filter_specs_by_intent(specs, "Search the web for the latest news")
    tool_ids = {spec.tool_id for spec in filtered}
    assert tool_ids == {"mcp_utils_internet_search_searxng_web_search"}


def test_filter_specs_by_intent_falls_back_when_no_match():
    """Keep the original list when intent filtering yields no matches."""
    planner = Planner(ToolRegistry())
    specs = [_spec("aider_read_file_tool", kind="local")]
    filtered = planner._filter_specs_by_intent(specs, "Search the web for the latest news")
    assert filtered == specs


def test_filter_specs_by_intent_keeps_multiple_capabilities():
    """Return a mixed set when the query implies multiple intents."""
    planner = Planner(ToolRegistry())
    specs = [
        _spec("mcp_utils_internet_search_searxng_web_search", kind="mcp"),
        _spec("aider_read_file_tool", kind="local"),
        _spec("home_assistant_tool", kind="local"),
    ]
    filtered = planner._filter_specs_by_intent(specs, "Search the web and open the local file")
    tool_ids = {spec.tool_id for spec in filtered}
    assert tool_ids == {
        "mcp_utils_internet_search_searxng_web_search",
        "aider_read_file_tool",
    }


def test_filter_specs_by_intent_includes_shell_tool():
    """Include shell tool when the query asks to run commands."""
    planner = Planner(ToolRegistry())
    specs = [
        _spec("aider_shell_tool", kind="local"),
        _spec("aider_read_file_tool", kind="local"),
    ]
    filtered = planner._filter_specs_by_intent(specs, "Run a command in the terminal")
    tool_ids = {spec.tool_id for spec in filtered}
    assert tool_ids == {"aider_shell_tool"}


def test_spec_capabilities_prefers_metadata():
    """Prefer explicit capability metadata when provided."""
    planner = Planner(ToolRegistry())
    spec = ToolSpec(
        tool_id="custom_tool",
        name="custom_tool",
        description="test",
        factory=lambda: object(),
        metadata={"capabilities": ["web_search"]},
    )
    assert planner._spec_capabilities(spec) == {"web_search"}


def test_spec_capabilities_infers_web_read():
    """Infer web_read capability from tool id."""
    planner = Planner(ToolRegistry())
    spec = _spec("mcp_utils_internet_search_web_url_read", kind="mcp")
    assert "web_read" in planner._spec_capabilities(spec)


def test_tool_selector_includes_web_read_for_search(monkeypatch):
    """Include web_url_read tool when web_search is selected."""
    selector = ToolSelector(ToolRegistry())
    specs = [
        _spec("mcp_utils_internet_search_searxng_web_search", kind="mcp"),
        _spec("mcp_utils_internet_search_web_url_read", kind="mcp"),
    ]

    def _fake_model(_inputs):
        return (
            '{"tool_required": true, '
            '"tool_ids": ["mcp_utils_internet_search_searxng_web_search"], '
            '"rationale": "search"}'
        )

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    selection = selector.select("Find recent news", "gpt-5.2", tool_specs=specs)
    assert "mcp_utils_internet_search_web_url_read" in selection.tool_ids


def test_tool_selector_requires_registry():
    """Raise when tool registry is missing."""
    selector = ToolSelector(None)
    with pytest.raises(ValueError):
        selector.select("hello", "gpt-5.2", tool_specs=[])


def test_step_executor_requires_registry():
    """Raise when step executor has no tool registry."""
    executor = StepExecutor(None)
    with pytest.raises(ValueError):
        executor.decide(
            "hello",
            PlanStep(title="Say hello", description="Respond to the user."),
            "gpt-5.2",
            allowed_tools=[],
        )


def test_step_executor_decides_response(monkeypatch):
    """Return a parsed decision from the step executor."""
    executor = StepExecutor(ToolRegistry())
    spec = ToolSpec(
        tool_id="dummy_tool",
        name="Dummy",
        description="test",
        factory=lambda: object(),
        metadata={"schema": {"type": "object"}},
    )

    def _fake_model(_inputs):
        return '{"decision": "respond", "response": "ok"}'

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    decision = executor.decide(
        "hello",
        PlanStep(title="Say hello", description="Respond to the user."),
        "gpt-5.2",
        allowed_tools=[spec],
    )
    assert decision.decision == "respond"
    assert decision.response == "ok"


def test_plan_updater_returns_steps(monkeypatch):
    """Return updated remaining steps from the plan updater."""
    updater = PlanUpdater(ToolRegistry())

    def _fake_model(_inputs):
        return '{"steps": [{"title": "Next", "description": "Do it"}]}'

    monkeypatch.setattr(
        planning,
        "build_chat_model",
        lambda **_kwargs: RunnableLambda(_fake_model),
    )

    steps = updater.update(
        "hello",
        "gpt-5.2",
        completed_step=PlanStep(title="Step 1", description="Do it"),
        last_result=None,
        remaining_steps=[],
        context=None,
    )
    assert steps
    assert steps[0].title == "Next"
