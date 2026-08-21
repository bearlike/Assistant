"""The bundled ``harness`` plugin — the ONE ungated built-in skill.

Integration-shaped against the real plugin tree, like
``test_generative_ui_plugin.py``: what matters is that discovery admits this
skill for a session advertising NOTHING, because an empty auto-invocable
catalogue does not merely hide skills — it unbinds ``activate_skill`` entirely
(``ToolUseLoop._directly_bound_tool_schemas`` injects
``ACTIVATE_SKILL_SCHEMA`` only when ``list_auto_invocable`` is truthy).

The parity block at the bottom exists because the skill body states live
constants. A doc asserting a number is a hypothesis about code; these tests are
what make it fail when the code moves.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

import pytest
from mewbo_core.agents.hypervisor import AgentStatus
from mewbo_core.tooling.plugins import discover_builtin_plugins
from mewbo_core.tooling.session_tools import DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS
from mewbo_core.tooling.skills import SkillRegistry
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec, _default_registry

HARNESS_SKILL = "mewbo-harness"


def _builtin_root() -> Path:
    traversable = importlib.resources.files("mewbo_core") / "builtin_plugins"
    return Path(str(traversable))


def _skill_path() -> Path:
    return _builtin_root() / "harness" / "skills" / HARNESS_SKILL / "SKILL.md"


@pytest.fixture(scope="module")
def default_registry() -> ToolRegistry:
    """The built-in tool specs, no MCP discovery — the caps live on these."""
    return _default_registry()


@pytest.fixture
def registry() -> SkillRegistry:
    """A registry holding ONLY the built-in plugin skills.

    Deliberately not ``load()`` — that scans the host's ``~/.claude`` and the
    cwd, so a developer's personal skills would mask the very emptiness this
    module is asserting against.
    """
    reg = SkillRegistry()
    for pc in discover_builtin_plugins(_builtin_root()):
        assert pc.manifest is not None
        for sd in pc.skill_dirs:
            reg.load_extra_dir(
                sd,
                source=f"plugin:{pc.manifest.name}",
                requires_capabilities=pc.manifest.requires_capabilities,
            )
    return reg


# ------------------------------------------------------------------
# The gate
# ------------------------------------------------------------------


def test_manifest_declares_no_capabilities() -> None:
    """The manifest must stay ungated — the gate is what this plugin exists to avoid."""
    manifest = json.loads(
        (_builtin_root() / "harness" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert "requires-capabilities" not in manifest
    assert "requires-capability" not in manifest

    components = next(
        pc
        for pc in discover_builtin_plugins(_builtin_root())
        if pc.manifest is not None and pc.manifest.name == "harness"
    )
    assert components.manifest is not None
    assert components.manifest.requires_capabilities == ()


def test_visible_to_a_session_with_no_capabilities(registry: SkillRegistry) -> None:
    """Zero advertised capabilities still sees the harness skill — and only it."""
    visible = {s.name for s in registry.visible_for(())}
    assert HARNESS_SKILL in visible
    # The other built-ins are capability-gated; if one becomes ungated this
    # assertion is the place to notice, not a silently larger catalogue.
    assert visible == {HARNESS_SKILL}


def test_activate_skill_stays_bound_for_a_zero_capability_session(
    registry: SkillRegistry,
) -> None:
    """The regression that matters: an empty catalogue unbinds ``activate_skill``.

    The loop injects ``ACTIVATE_SKILL_SCHEMA`` only when
    ``list_auto_invocable(...)`` is truthy, so the mechanism for reaching ANY
    skill disappears below one visible skill. This asserts the floor.
    """
    assert registry.list_auto_invocable(()) != []
    assert HARNESS_SKILL in {s.name for s in registry.list_auto_invocable(())}


def test_gated_builtins_stay_gated(registry: SkillRegistry) -> None:
    """Sanity on the other half: the capability-gated skills are still gated."""
    ungated = {s.name for s in registry.visible_for(())}
    with_caps = {s.name for s in registry.visible_for(("stlite", "generative_ui"))}
    assert ungated < with_caps


def test_activate_skill_description_covers_harness_knowledge() -> None:
    """Nothing else nudges a model toward a skill for harness semantics."""
    from mewbo_core.tooling.skills import ACTIVATE_SKILL_SCHEMA

    description = ACTIVATE_SKILL_SCHEMA["function"]["description"]
    assert "harness" in str(description).lower()


# ------------------------------------------------------------------
# Parity — every live constant the skill body asserts
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def skill_text() -> str:
    return _skill_path().read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("stated", "live"),
    [
        ("**2000 characters**", ToolSpec.max_result_chars),
        ("**200000 characters**", DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS),
        ("**120 seconds**", int(ToolSpec.timeout)),
    ],
)
def test_stated_number_matches_the_live_default(
    skill_text: str, stated: str, live: int
) -> None:
    """A number in the body is only allowed to appear if the code still says it."""
    assert str(live) in stated, "the parametrization itself drifted"
    assert stated in skill_text


def test_shell_result_cap_matches_the_shell_specs(
    skill_text: str, default_registry: ToolRegistry
) -> None:
    """The shell cap is DECLARED per-spec, so read it off the specs, not a constant."""
    caps = {
        spec.max_result_chars
        for spec in default_registry.list_specs()
        if spec.tool_id in ("aider_shell_tool", "shell_session_tool")
    }
    assert len(caps) == 1, f"shell specs disagree on their cap: {caps}"
    assert f"**{caps.pop()} characters**" in skill_text


def test_read_file_paging_arguments_still_exist(
    skill_text: str, default_registry: ToolRegistry
) -> None:
    """``offset``/``limit`` and the 2000-line default are the paging advice."""
    spec = default_registry.get_spec("read_file")
    assert spec is not None
    properties = spec.metadata["schema"]["properties"]
    assert {"offset", "limit"} <= set(properties)
    assert "2000" in properties["limit"]["description"]
    assert "`offset`" in skill_text and "`limit`" in skill_text


def test_agent_lifecycle_vocabulary_matches(skill_text: str) -> None:
    """Six states, four terminal — named individually in the body."""
    states = set(AgentStatus.__args__)
    assert states == {
        "submitted",
        "running",
        "completed",
        "failed",
        "cancelled",
        "rejected",
    }
    assert "six states" in skill_text
    for state in states:
        assert f"`{state}`" in skill_text


# The two present_ui guards that used to live here moved with their subject.
# The panel vocabulary and the worked example are now owned by the
# generative-ui skill, and pinned by tests/test_generative_ui_vocabulary.py
# — which additionally compares the union against the console's renderer
# allowlist. Restating either check here would be a second source of truth
# for a fact that already has one.
