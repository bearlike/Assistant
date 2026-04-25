#!/usr/bin/env python3
"""Unit + integration smoke for the bundled ``widget-builder`` plugin.

These tests intentionally operate on the real plugin tree shipped under
``mewbo_core/builtin_plugins/widget_builder/``. Keeping them integration-
shaped catches regressions in the plugin discovery pipeline (manifest parse,
session-tool instantiation, capability gating) end-to-end, which is the whole
point of making widget-builder a first-party built-in plugin in the first
place.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers — resolve the shipped plugin tree once per test session.
# ---------------------------------------------------------------------------


def _plugin_root() -> Path:
    traversable = importlib.resources.files("mewbo_core") / "builtin_plugins" / "widget_builder"
    return Path(str(traversable))


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_parses_with_stlite_capability(self):
        from mewbo_core.plugins import parse_plugin_manifest

        manifest = parse_plugin_manifest(_plugin_root())
        assert manifest is not None
        assert manifest.name == "widget-builder"
        assert manifest.requires_capabilities == ("stlite",)

    def test_manifest_declares_submit_widget_session_tool(self):
        raw = json.loads(
            (_plugin_root() / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        entries = raw["session_tools"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["tool_id"] == "submit_widget"
        assert entry["module"].endswith("widget_builder.submit_widget")
        assert entry["class"] == "SubmitWidgetTool"


# ---------------------------------------------------------------------------
# Skill + agent frontmatter
# ---------------------------------------------------------------------------


class TestAgentAndSkill:
    def test_skill_parses_with_agent_and_capability(self):
        # ``_parse_skill_file`` is intentionally module-private; tests use it
        # because the plugin teaches LLMs to delegate to the agent — verifying
        # frontmatter round-trips end-to-end is the single source of truth.
        from mewbo_core.skills import _parse_skill_file

        skill_md = _plugin_root() / "skills" / "st-widget-builder" / "SKILL.md"
        spec = _parse_skill_file(skill_md, source="built-in:widget-builder")
        assert spec is not None
        assert spec.name == "st-widget-builder"
        assert spec.requires_capabilities == ("stlite",)
        # The teaching skill points the LLM at the spawnable sub-agent.
        assert spec.agent == "st-widget-builder"

    def test_agent_parses_with_submit_widget_and_capability(self):
        from mewbo_core.agent_registry import parse_agent_file

        agent_md = _plugin_root() / "agents" / "st-widget-builder.md"
        agent_def = parse_agent_file(agent_md, source="built-in:widget-builder")
        assert agent_def is not None
        assert agent_def.name == "st-widget-builder"
        assert agent_def.requires_capabilities == ("stlite",)
        assert agent_def.allowed_tools is not None
        assert "submit_widget" in agent_def.allowed_tools


# ---------------------------------------------------------------------------
# SessionToolRegistry.load_entry → SubmitWidgetTool
# ---------------------------------------------------------------------------


class TestSessionToolLoad:
    def test_load_entry_imports_submit_widget_class(self):
        from mewbo_core.session_tools import SessionToolRegistry

        raw = json.loads(
            (_plugin_root() / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        entry = raw["session_tools"][0]

        reg = SessionToolRegistry()
        reg.load_entry(entry)

        tools = reg.build_for(["submit_widget"], session_id="sess", event_logger=None)
        assert len(tools) == 1
        tool = tools[0]
        assert tool.tool_id == "submit_widget"

    def test_submit_widget_tool_direct_construction(self):
        from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
            SubmitWidgetTool,
        )

        tool = SubmitWidgetTool(session_id="s1", event_logger=None)
        assert tool.tool_id == "submit_widget"
        assert tool.schema["function"]["name"] == "submit_widget"  # type: ignore[index]
        assert tool.modes == frozenset({"act"})


# ---------------------------------------------------------------------------
# Pydantic path-traversal guard — the security boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_widget_id",
    ["..", "../etc", "a/b", "a\\b", ".hidden", "", "./x"],
)
def test_widget_id_rejects_traversal_attempts(bad_widget_id):
    from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
        SubmitWidgetArgs,
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitWidgetArgs(widget_id=bad_widget_id)


def test_widget_id_accepts_plain_identifier():
    from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
        SubmitWidgetArgs,
    )

    args = SubmitWidgetArgs(widget_id="widget_123")
    assert args.widget_id == "widget_123"


def test_handle_runtime_traversal_guard_rejects_symlink_escape(tmp_path, monkeypatch):
    """A validator-passing widget_id that resolves outside the root is rejected.

    Exercises the runtime guard in ``handle`` — ``Path.resolve()`` +
    ``relative_to(root)`` — which the Pydantic validator never reaches.
    Uses a symlink from inside the widget root pointing at the filesystem
    root so the resolved path escapes without using any ``..`` in the id.
    """
    import asyncio

    from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
        SubmitWidgetTool,
    )
    from mewbo_core.classes import ActionStep

    widget_root = tmp_path / "widgets"
    session_dir = widget_root / "s1"
    session_dir.mkdir(parents=True)

    # Create a symlinked widget id whose target lives OUTSIDE the widget root.
    escape_target = tmp_path / "outside"
    escape_target.mkdir()
    (session_dir / "escape").symlink_to(escape_target, target_is_directory=True)

    monkeypatch.setenv("MEWBO_WIDGET_ROOT", str(widget_root))
    tool = SubmitWidgetTool(session_id="s1", event_logger=None)

    step = ActionStep(
        tool_id="submit_widget",
        operation="run",
        tool_input={"widget_id": "escape"},
    )
    result = asyncio.run(tool.handle(step))

    assert "escapes the widget root" in result.content


# ---------------------------------------------------------------------------
# End-to-end capability gating: the agent is invisible without stlite.
# ---------------------------------------------------------------------------


def test_capability_gating_hides_agent_without_stlite():
    from mewbo_core.agent_registry import (
        AgentRegistry,
        parse_agent_file,
    )

    agent_md = _plugin_root() / "agents" / "st-widget-builder.md"
    agent_def = parse_agent_file(agent_md, source="built-in:widget-builder")
    assert agent_def is not None

    registry = AgentRegistry()
    registry.register(
        agent_def,
        capabilities=("stlite",),
        plugin_root=str(_plugin_root()),
    )

    # Session that hasn't advertised stlite can't see the agent.
    assert registry.get("st-widget-builder", ()) is None

    # Session that has advertised stlite gets the agent back.
    visible = registry.get("st-widget-builder", ("stlite",))
    assert visible is not None
    assert visible.name == "st-widget-builder"
    # plugin_root is stamped through the register() kwarg.
    assert visible.plugin_root == str(_plugin_root())
