"""Tests for intent-based tool scoping in the planner."""

from meeseeks_core.planning import Planner
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
