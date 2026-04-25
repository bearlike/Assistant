#!/usr/bin/env python3
"""Tests for agent_registry.py — AgentDef, AgentRegistry, parse_agent_file, map_cc_tool_names."""

from meeseeks_core.agent_registry import (
    CC_TOOL_MAP,
    AgentDef,
    AgentRegistry,
    map_cc_tool_names,
    parse_agent_file,
)


def test_parse_agent_file(tmp_path):
    md = tmp_path / "reviewer.md"
    md.write_text(
        "---\nname: reviewer\ndescription: Reviews code for bugs\nmodel: sonnet\n"
        "tools: read_file aider_shell_tool\n---\nYou are a code reviewer.\n"
    )
    agent = parse_agent_file(md, source="plugin:feature-dev")
    assert agent is not None
    assert agent.name == "reviewer"
    assert agent.description == "Reviews code for bugs"
    assert agent.model == "sonnet"
    assert agent.allowed_tools == ["read_file", "aider_shell_tool"]
    assert "code reviewer" in agent.body


def test_parse_agent_file_cc_tool_names(tmp_path):
    """CC tool names in frontmatter should be mapped to Meeseeks IDs."""
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\ndescription: test\ntools: Read Glob Bash\n---\nBody")
    agent = parse_agent_file(md, source="plugin:test")
    assert agent is not None
    assert "read_file" in agent.allowed_tools
    assert "aider_list_dir_tool" in agent.allowed_tools
    assert "aider_shell_tool" in agent.allowed_tools


def test_parse_agent_file_inherit_model(tmp_path):
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\ndescription: test\nmodel: inherit\n---\nBody")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert agent.model is None


def test_parse_agent_file_name_from_filename(tmp_path):
    """When name is missing from frontmatter, use filename stem."""
    md = tmp_path / "my-agent.md"
    md.write_text("---\ndescription: Does stuff\n---\nBody text")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert agent.name == "my-agent"


def test_parse_agent_file_tools_as_list(tmp_path):
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\ndescription: test\ntools:\n  - Read\n  - Bash\n---\nBody")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert "read_file" in agent.allowed_tools


def test_parse_agent_file_disallowed_tools(tmp_path):
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\ndescription: test\ndisallowedTools: Write Edit\n---\nBody")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert agent.denied_tools is not None
    assert "file_edit_tool" in agent.denied_tools or "aider_edit_block_tool" in agent.denied_tools


def test_parse_agent_file_when_to_use(tmp_path):
    """Claude Code uses 'when-to-use' as alias for 'description'."""
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\nwhen-to-use: When doing X\n---\nBody")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert agent.description == "When doing X"


def test_parse_agent_file_no_frontmatter(tmp_path):
    """Files without YAML frontmatter infer name from filename and body."""
    md = tmp_path / "agent.md"
    md.write_text("# My Agent\n\nJust a plain body without frontmatter.")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert agent.name == "agent"
    assert agent.description == "My Agent"
    assert "Just a plain body" in agent.body
    assert agent.allowed_tools is None


def test_parse_agent_file_invalid_yaml(tmp_path):
    """Files with invalid YAML should return None."""
    md = tmp_path / "agent.md"
    md.write_text("---\n: bad: yaml: [\n---\nBody")
    agent = parse_agent_file(md, source="test")
    assert agent is None


def test_parse_agent_file_source_path(tmp_path):
    """source_path should be absolute path string of the file."""
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\ndescription: desc\n---\nBody")
    agent = parse_agent_file(md, source="personal")
    assert agent is not None
    assert agent.source_path == str(md)
    assert agent.source == "personal"


def test_parse_agent_file_body_content(tmp_path):
    """Body is everything after the closing frontmatter ---."""
    md = tmp_path / "agent.md"
    md.write_text("---\nname: test\ndescription: desc\n---\nHello world\nSecond line\n")
    agent = parse_agent_file(md, source="test")
    assert agent is not None
    assert "Hello world" in agent.body
    assert "Second line" in agent.body


def test_agent_registry_operations():
    registry = AgentRegistry()
    assert registry.get("reviewer") is None
    agent = AgentDef(
        name="reviewer",
        description="Reviews",
        source_path="/tmp/r.md",
        source="plugin:test",
        body="Review.",
        allowed_tools=None,
        denied_tools=None,
        model=None,
    )
    registry.register(agent)
    assert registry.get("reviewer") is not None
    assert len(registry.list_all()) == 1


def test_agent_registry_no_override():
    """First registered agent wins (like SkillRegistry)."""
    registry = AgentRegistry()
    a1 = AgentDef(name="x", description="First", source_path="/a", source="a", body="1")
    a2 = AgentDef(name="x", description="Second", source_path="/b", source="b", body="2")
    registry.register(a1)
    registry.register(a2)
    assert registry.get("x").description == "First"


def test_agent_registry_render_catalog():
    registry = AgentRegistry()
    agent = AgentDef(
        name="reviewer",
        description="Reviews code",
        source_path="/tmp/r.md",
        source="test",
        body="...",
        allowed_tools=None,
        denied_tools=None,
        model=None,
    )
    registry.register(agent)
    catalog = registry.render_catalog()
    assert "reviewer" in catalog
    assert "Reviews code" in catalog
    assert "spawn_agent" in catalog


def test_agent_registry_empty_catalog():
    registry = AgentRegistry()
    assert registry.render_catalog() == ""


def test_map_cc_tool_names():
    mapped = map_cc_tool_names(["Read", "Glob", "Grep", "Bash", "Edit", "Write"])
    assert "read_file" in mapped
    assert "aider_shell_tool" in mapped
    assert "aider_list_dir_tool" in mapped


def test_map_cc_tool_names_dedup():
    """Multiple CC names mapping to same Meeseeks ID should be deduped."""
    mapped = map_cc_tool_names(["Bash", "Grep", "BashOutput"])
    assert mapped.count("aider_shell_tool") == 1


def test_map_cc_tool_names_passthrough():
    """Unknown CC names pass through unchanged."""
    mapped = map_cc_tool_names(["read_file", "custom_tool"])
    assert "read_file" in mapped
    assert "custom_tool" in mapped


def test_cc_tool_map_has_expected_keys():
    """CC_TOOL_MAP should contain all expected Claude Code tool names."""
    expected_keys = {
        "Read",
        "Glob",
        "Grep",
        "Bash",
        "BashOutput",
        "KillShell",
        "Edit",
        "Write",
        "LS",
        "NotebookRead",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
    }
    for key in expected_keys:
        assert key in CC_TOOL_MAP, f"Missing key: {key}"


def test_agent_def_frozen():
    """AgentDef is a frozen dataclass — mutation should raise."""
    agent = AgentDef(
        name="x", description="desc", source_path="/tmp/x.md", source="test", body="body"
    )
    try:
        agent.name = "y"  # type: ignore[misc]
        assert False, "Expected FrozenInstanceError"
    except Exception:
        pass  # Expected


def test_spawn_agent_schema_has_agent_type():
    from meeseeks_core.spawn_agent import SPAWN_AGENT_SCHEMA

    props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
    assert "agent_type" in props
    assert "string" in str(props["agent_type"]["type"])


def test_agent_def_parses_requires_capabilities_list(tmp_path):
    """Frontmatter ``requires-capabilities`` list is parsed into AgentDef."""
    md = tmp_path / "widget.md"
    md.write_text(
        "---\n"
        "name: widget\n"
        "description: Builds widgets\n"
        "requires-capabilities: [stlite]\n"
        "---\n"
        "Body text.\n"
    )
    agent = parse_agent_file(md, source="plugin:widget-builder")
    assert agent is not None
    assert agent.requires_capabilities == ("stlite",)


def test_agent_registry_filters_by_session_capabilities():
    """render_catalog + get honour session_capabilities for gated agents."""
    registry = AgentRegistry()
    free = AgentDef(
        name="free",
        description="No gate",
        source_path="/tmp/free.md",
        source="test",
        body="body",
    )
    gated = AgentDef(
        name="gated",
        description="Needs stlite",
        source_path="/tmp/gated.md",
        source="test",
        body="body",
        requires_capabilities=("stlite",),
    )
    registry.register(free)
    registry.register(gated)

    # Session without the capability sees only the free agent.
    catalog_no_caps = registry.render_catalog(session_capabilities=())
    assert "free" in catalog_no_caps
    assert "gated" not in catalog_no_caps
    assert registry.get("gated", session_capabilities=()) is None
    assert registry.get("free", session_capabilities=()) is not None

    # Session with the capability sees both.
    catalog_with_caps = registry.render_catalog(session_capabilities=("stlite",))
    assert "free" in catalog_with_caps
    assert "gated" in catalog_with_caps
    assert registry.get("gated", session_capabilities=("stlite",)) is not None
