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


class TestAgentPromptResolvesPluginPath:
    """The agent's example paths must resolve through the REAL substitution seam.

    Regression: the prompt used the unbraced ``$CLAUDE_PLUGIN_ROOT``, which
    ``substitute_agent_body`` pass 1 (``${KEY}`` from ``subs``) never matches —
    it fell through to pass 3 (plain ``$VAR`` from the process env), where the
    var is unset, so the shell later expanded ``ls "$CLAUDE_PLUGIN_ROOT/…"`` to
    ``ls "/examples/components/"`` and the sub-agent thrashed hunting for a
    directory that never existed. Braced ``${CLAUDE_PLUGIN_ROOT}`` resolves
    deterministically from the spawn's ``plugin_root``, independent of any
    runtime shell env.
    """

    def test_example_paths_resolve_to_the_real_plugin_root(self):
        from mewbo_core.agent_registry import parse_agent_file
        from mewbo_core.spawn_agent import substitute_agent_body

        agent_md = _plugin_root() / "agents" / "st-widget-builder.md"
        agent_def = parse_agent_file(agent_md, source="built-in:widget-builder")
        assert agent_def is not None

        # Substitute with the real seam, an EMPTY env (mobile/deployed reality
        # where CLAUDE_PLUGIN_ROOT is not exported to the shell), so only the
        # ${...} substitution from subs can resolve the path.
        rendered = substitute_agent_body(
            agent_def.body,
            {"SESSION_ID": "s1", "CLAUDE_PLUGIN_ROOT": "/opt/plugin"},
            env={},
        )

        # Every example path must point at the real plugin root...
        assert "/opt/plugin/examples/components" in rendered
        # ...and NO unresolved CLAUDE_PLUGIN_ROOT reference may survive (either
        # form) into the text the sub-agent's shell will run.
        assert "$CLAUDE_PLUGIN_ROOT" not in rendered
        assert "${CLAUDE_PLUGIN_ROOT}" not in rendered


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


class TestWidgetReadyPayload:
    """The typed ``WidgetReadyPayload`` contract — wire-frozen snake_case keys
    mirrored by the console TS type and an Android Kotlin type."""

    FROZEN_KEYS = {"widget_id", "session_id", "files", "requirements", "summary"}

    def test_round_trip_matches_frozen_wire_shape(self):
        from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
            WidgetReadyPayload,
        )

        payload = WidgetReadyPayload(
            widget_id="w1",
            session_id="s1",
            files={"app.py": "import streamlit as st\n", "data.json": "{}"},
            requirements=["pandas"],
            summary="a widget",
        )
        dumped = payload.model_dump()

        assert set(dumped) == self.FROZEN_KEYS
        assert dumped["widget_id"] == "w1"
        assert dumped["session_id"] == "s1"
        assert dumped["files"] == {"app.py": "import streamlit as st\n", "data.json": "{}"}
        assert dumped["requirements"] == ["pandas"]
        assert dumped["summary"] == "a widget"
        assert isinstance(dumped["widget_id"], str)
        assert isinstance(dumped["session_id"], str)
        assert isinstance(dumped["files"], dict)
        assert isinstance(dumped["requirements"], list)
        assert isinstance(dumped["summary"], str)

    def test_handle_emits_widget_ready_event_matching_frozen_shape(self, tmp_path, monkeypatch):
        """End-to-end: a successful ``submit_widget`` call emits a
        ``widget_ready`` event whose payload is exactly the frozen shape —
        not a hand-assembled dict that happens to look right."""
        import asyncio

        from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
            SubmitWidgetTool,
        )
        from mewbo_core.classes import ActionStep

        widget_root = tmp_path / "widgets"
        widget_dir = widget_root / "s1" / "w1"
        widget_dir.mkdir(parents=True)
        (widget_dir / "app.py").write_text("import streamlit as st\n", encoding="utf-8")
        (widget_dir / "data.json").write_text("{}", encoding="utf-8")

        monkeypatch.setenv("MEWBO_WIDGET_ROOT", str(widget_root))

        events: list[dict] = []
        tool = SubmitWidgetTool(session_id="s1", event_logger=events.append)

        step = ActionStep(
            tool_id="submit_widget",
            operation="run",
            tool_input={"widget_id": "w1", "requirements": ["pandas"], "summary": "demo"},
        )
        result = asyncio.run(tool.handle(step))

        assert "submitted successfully" in result.content
        assert len(events) == 1
        event = events[0]
        assert event["type"] == "widget_ready"
        payload = event["payload"]
        assert set(payload) == self.FROZEN_KEYS
        assert payload == {
            "widget_id": "w1",
            "session_id": "s1",
            "files": {"app.py": "import streamlit as st\n", "data.json": "{}"},
            "requirements": ["pandas"],
            "summary": "demo",
        }

    def test_rejects_extra_key(self):
        from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
            WidgetReadyPayload,
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            WidgetReadyPayload(
                widget_id="w1",
                session_id="s1",
                files={"app.py": "x", "data.json": "{}"},
                extra_field="not allowed",
            )

    @pytest.mark.parametrize(
        "bad_files",
        [
            {"app.py": "x"},  # missing data.json
            {"data.json": "{}"},  # missing app.py
            {"app.py": "x", "data.json": "{}", "extra.txt": "y"},  # extra file key
            {},  # empty
        ],
    )
    def test_rejects_incomplete_or_extra_file_keys(self, bad_files):
        from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
            WidgetReadyPayload,
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            WidgetReadyPayload(
                widget_id="w1",
                session_id="s1",
                files=bad_files,
            )


def test_successful_submit_does_not_force_loop_termination(tmp_path, monkeypatch):
    """A successful submit must NOT force the run to terminate.

    Regression for Aura session ``e58f7cf7…``: ``submit_widget`` set a
    terminate flag, so the loop's terminal poll
    (``tool_use_loop.py``: ``should_terminate_run()`` then
    ``terminal_reason()``) read ``terminal_reason()`` — which this tool never
    defined — and crashed the run with ``AttributeError`` *after* the widget
    was already built and ``widget_ready`` emitted. The widget renders off the
    event, not off termination, so the tool is terminal-free like
    ``update_todos``: the agent stops naturally on its next (text) turn.

    This replicates the loop's poll verbatim against the real tool so it fails
    the exact way production did if the termination behaviour ever returns.
    """
    import asyncio

    from mewbo_core.builtin_plugins.widget_builder.submit_widget import (
        SubmitWidgetTool,
    )
    from mewbo_core.classes import ActionStep

    widget_root = tmp_path / "widgets"
    widget_dir = widget_root / "s1" / "w1"
    widget_dir.mkdir(parents=True)
    (widget_dir / "app.py").write_text("import streamlit as st\n", encoding="utf-8")
    (widget_dir / "data.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MEWBO_WIDGET_ROOT", str(widget_root))

    events: list[dict] = []
    tool = SubmitWidgetTool(session_id="s1", event_logger=events.append)
    step = ActionStep(
        tool_id="submit_widget",
        operation="run",
        tool_input={"widget_id": "w1"},
    )
    result = asyncio.run(tool.handle(step))

    assert "submitted successfully" in result.content
    assert len(events) == 1 and events[0]["type"] == "widget_ready"

    # Verbatim replica of the loop's terminal poll (tool_use_loop.py:767-773).
    terminating = [t for t in [tool] if t.should_terminate_run()]
    done_reason = terminating[0].terminal_reason() if terminating else "completed"
    assert terminating == []
    assert done_reason == "completed"


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
