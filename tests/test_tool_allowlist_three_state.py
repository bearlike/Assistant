#!/usr/bin/env python3
"""``allowed_tools`` is THREE states, and collapsing two of them fails open.

* ``None`` — unrestricted: impose no allowlist at all.
* ``[]`` — explicitly none: this principal is granted NO tools.
* a non-empty list — exactly those.

The dangerous collapse is ``[]`` → ``None``: a scope that deliberately granted
zero tools silently becoming every tool. It is the same shape as the credential
limb in :mod:`tests.iam.test_key_scope_three_state`, one layer down, and it
recurred independently at seven sites because every one of them tested the field
with truthiness.

These drive the law at each layer that owns a copy of the decision — the
registry filter, the loop's spawn gate, the session-tool gates, both parse
seams, and the delegation path where the argument is MODEL-SUPPLIED — rather
than asserting it once centrally, because a collapse reintroduced at any single
site is exactly as fail-open as the original.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from mewbo_core.agent_registry import parse_agent_file
from mewbo_core.session_tools import SessionToolFactory, SessionToolRegistry
from mewbo_core.skills import _parse_skill_file, activate_skill
from mewbo_core.tool_registry import ToolSpec, filter_specs


def _spec(tool_id: str, *, always_load: bool = False) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"{tool_id} description",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={"always_load": True} if always_load else {},
    )


# ── the registry filter: the root copy of the decision ───────────────────────


class TestFilterSpecsThreeState:
    """``filter_specs`` is the gate every other layer ultimately funnels into."""

    def test_none_imposes_no_allowlist(self):
        specs = [_spec("read_file"), _spec("shell")]

        kept = {s.tool_id for s in filter_specs(specs, allowed=None)}

        assert kept == {"read_file", "shell"}

    def test_empty_list_grants_nothing(self):
        # The fail-open bug: under ``if allowed:`` the gate was skipped
        # ENTIRELY and every spec survived, so "no tools" bound the full set.
        specs = [_spec("read_file"), _spec("shell")]

        kept = {s.tool_id for s in filter_specs(specs, allowed=[])}

        assert kept == set(), "an empty allowlist must grant no tools"

    def test_non_empty_list_grants_exactly_those(self):
        specs = [_spec("read_file"), _spec("shell"), _spec("write_file")]

        kept = {s.tool_id for s in filter_specs(specs, allowed=["read_file"])}

        assert kept == {"read_file"}

    def test_always_load_stays_exempt_under_an_empty_allowlist(self):
        # The always_load exemption is an INDEPENDENT law (a scoped agent must
        # keep the means to fetch its deferred MCP tools). Pinned here so the
        # three-state fix is not mistaken for a licence to drop it.
        specs = [_spec("read_file"), _spec("tool_search", always_load=True)]

        kept = {s.tool_id for s in filter_specs(specs, allowed=[])}

        assert kept == {"tool_search"}


# ── the session-tool gates ───────────────────────────────────────────────────


class TestSessionToolScopeThreeState:
    """An empty allowlist is an explicit scope, so both auto-surfaces are capped."""

    def _registry(self) -> SessionToolRegistry:
        registry = SessionToolRegistry()
        registry.register(
            SessionToolFactory(
                tool_id="wiki_search_pages",
                build=lambda **_kwargs: MagicMock(),
                requires_capabilities=("wiki",),
            )
        )
        registry.register(
            SessionToolFactory(
                tool_id="schedule_trigger",
                build=lambda **_kwargs: MagicMock(),
                unconditional=True,
            )
        )
        return registry

    def test_none_lets_the_capability_gate_auto_surface(self):
        ids = self._registry().ids_for(None, session_capabilities=("wiki",))

        assert "wiki_search_pages" in ids

    def test_empty_allowlist_caps_the_capability_auto_surface(self):
        # Truthiness read ``[]`` as "no scope declared" and handed the session
        # every capability-matched tool the empty grant exists to withhold.
        ids = self._registry().ids_for([], session_capabilities=("wiki",))

        assert "wiki_search_pages" not in ids

    def test_empty_strict_allowlist_caps_the_unconditional_tool(self):
        ids = self._registry().ids_for([], strict_tool_scope=True)

        assert ids == []

    def test_the_df875_permissive_carve_out_survives_the_fix(self):
        # An unconditional tool is capped only under STRICT scope. A permissive
        # empty ceiling must still surface it, or the mobile reminder flow dies.
        ids = self._registry().ids_for([], strict_tool_scope=False)

        assert "schedule_trigger" in ids


# ── the parse seams: intent must survive being read off disk ─────────────────


class TestParsedScopePreservesEmpty:
    """``tools: []`` / ``allowed-tools: []`` are authored declarations, not noise.

    Both parsers normalized empty → ``None`` at construction, destroying the
    author's intent before any consumer could honour it. Plugin-supplied agents
    and skills make these a real trust boundary.
    """

    def test_agent_def_keeps_an_explicit_empty_tools_list(self, tmp_path: Path):
        path = tmp_path / "leaf.md"
        path.write_text(
            "---\nname: leaf\ndescription: A leaf agent\ntools: []\n---\nBody.\n"
        )

        agent_def = parse_agent_file(path, "project")

        assert agent_def is not None
        assert agent_def.allowed_tools == [], "an explicit empty tools: must persist"

    def test_agent_def_absent_tools_stays_unrestricted(self, tmp_path: Path):
        path = tmp_path / "open.md"
        path.write_text("---\nname: open\ndescription: An agent\n---\nBody.\n")

        agent_def = parse_agent_file(path, "project")

        assert agent_def is not None
        assert agent_def.allowed_tools is None

    def test_agent_def_denied_tools_is_deliberately_not_three_state(
        self, tmp_path: Path
    ):
        # Deny is purely subtractive, so an empty denylist and no denylist are
        # the same SET — there is no third state to lose. Pinned so the sweep
        # that fixed ``allowed_tools`` is not extended here by symmetry.
        path = tmp_path / "nodeny.md"
        path.write_text(
            "---\nname: nodeny\ndescription: An agent\ndisallowedTools: []\n---\nBody.\n"
        )

        agent_def = parse_agent_file(path, "project")

        assert agent_def is not None
        assert agent_def.denied_tools is None

    def test_skill_keeps_an_explicit_empty_allowed_tools_list(self, tmp_path: Path):
        path = tmp_path / "SKILL.md"
        path.write_text(
            "---\nname: scoped\ndescription: A skill\nallowed-tools: []\n---\nBody.\n"
        )

        skill = _parse_skill_file(path, "project")

        assert skill is not None
        assert skill.allowed_tools == []

    def test_skill_absent_allowed_tools_stays_unrestricted(self, tmp_path: Path):
        path = tmp_path / "SKILL.md"
        path.write_text("---\nname: open\ndescription: A skill\n---\nBody.\n")

        skill = _parse_skill_file(path, "project")

        assert skill is not None
        assert skill.allowed_tools is None


# ── the consumers: preserving intent is worthless if the reader drops it ─────


class TestConsumersHonourTheParsedEmptyScope:
    """Fixing the parse seam alone would have been a no-op.

    Both consumers guarded on truthiness, so a preserved ``[]`` fell through to
    the very same unrestricted branch the parser used to produce directly. The
    two halves only close the hole together.
    """

    def test_activate_skill_scopes_an_empty_allowed_tools_to_nothing(
        self, tmp_path: Path
    ):
        path = tmp_path / "SKILL.md"
        path.write_text(
            "---\nname: scoped\ndescription: A skill\nallowed-tools: []\n---\nBody.\n"
        )
        skill = _parse_skill_file(path, "project")
        assert skill is not None

        _body, scoped = activate_skill(skill, "", [_spec("read_file"), _spec("shell")])

        assert scoped == [], "an empty allowed-tools must scope the skill to no tools"

    def test_activate_skill_leaves_an_absent_scope_unrestricted(self, tmp_path: Path):
        path = tmp_path / "SKILL.md"
        path.write_text("---\nname: open\ndescription: A skill\n---\nBody.\n")
        skill = _parse_skill_file(path, "project")
        assert skill is not None
        specs = [_spec("read_file"), _spec("shell")]

        _body, scoped = activate_skill(skill, "", specs)

        assert scoped is not None
        assert {s.tool_id for s in scoped} == {"read_file", "shell"}
