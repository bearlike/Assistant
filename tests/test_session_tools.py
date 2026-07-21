#!/usr/bin/env python3
"""Unit tests for ``mewbo_core.session_tools``.

Covers the ``SessionToolRegistry`` contract exposed in Task 4 of the
widget-builder-as-plugin refactor:
- empty-registry behaviour,
- manifest-driven ``load_entry`` (happy path + malformed records),
- allowlist-filtered ``build_for`` per-session instantiation.
"""

from __future__ import annotations

import sys
import types

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker
from mewbo_core.session_tools import SessionToolFactory, SessionToolRegistry

# ---------------------------------------------------------------------------
# Fixture: a minimal SessionTool used as the import target for load_entry.
# ---------------------------------------------------------------------------


class _FakeSessionTool:
    """Lightweight SessionTool implementation used by the tests.

    Subclasses override ``tool_id`` / ``schema`` to be distinguishable.
    """

    tool_id: str = "fake_tool"
    schema: dict[str, object] = {"type": "function", "function": {"name": "fake_tool"}}

    def __init__(self, *, session_id: str, event_logger=None) -> None:
        self.session_id = session_id
        self.event_logger = event_logger

    async def handle(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False


class _FakeSessionToolA(_FakeSessionTool):
    tool_id: str = "a"
    schema: dict[str, object] = {"type": "function", "function": {"name": "a"}}


class _FakeSessionToolB(_FakeSessionTool):
    tool_id: str = "b"
    schema: dict[str, object] = {"type": "function", "function": {"name": "b"}}


# ---------------------------------------------------------------------------
# build_for
# ---------------------------------------------------------------------------


class TestBuildFor:
    def test_empty_registry_returns_empty_list(self):
        reg = SessionToolRegistry()
        assert (
            reg.build_for(["anything"], session_id="s1", event_logger=None) == []
        )

    def test_build_for_none_allowed_returns_empty(self):
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="fake_tool",
                build=lambda sid, el: _FakeSessionTool(session_id=sid, event_logger=el),
            )
        )
        assert reg.build_for(None, session_id="s1", event_logger=None) == []

    def test_build_for_empty_list_returns_empty(self):
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="fake_tool",
                build=lambda sid, el: _FakeSessionTool(session_id=sid, event_logger=el),
            )
        )
        assert reg.build_for([], session_id="s1", event_logger=None) == []

    def test_build_for_unknown_tool_returns_empty(self):
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="fake_tool",
                build=lambda sid, el: _FakeSessionTool(session_id=sid, event_logger=el),
            )
        )
        assert reg.build_for(["unknown"], session_id="s1", event_logger=None) == []

    def test_build_for_matching_id_instantiates_tool(self):
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="fake_tool",
                build=lambda sid, el: _FakeSessionTool(session_id=sid, event_logger=el),
            )
        )
        tools = reg.build_for(
            ["fake_tool"],
            session_id="sess-42",
            event_logger=None,
        )
        assert len(tools) == 1
        assert tools[0].tool_id == "fake_tool"
        assert tools[0].session_id == "sess-42"

    def test_build_for_filters_by_allowed_tools(self):
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="a",
                build=lambda sid, el: _FakeSessionToolA(
                    session_id=sid, event_logger=el
                ),
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="b",
                build=lambda sid, el: _FakeSessionToolB(
                    session_id=sid, event_logger=el
                ),
            )
        )
        both = reg.build_for(["a", "b"], session_id="s1", event_logger=None)
        assert [t.tool_id for t in both] == ["a", "b"]
        only_b = reg.build_for(["b"], session_id="s1", event_logger=None)
        assert [t.tool_id for t in only_b] == ["b"]


# ---------------------------------------------------------------------------
# build_for — capability gate (runtime-granted capability bridge)
# ---------------------------------------------------------------------------


class TestBuildForCapabilityGate:
    """A capability-gated session tool surfaces from the session caps alone.

    Regression: a runtime capability grant (the ``scg`` provider)
    unions the capability into ``session_capabilities``, but session tools were
    selected ONLY by ``allowed_tools`` — so the root agent of an ordinary
    session never got ``scg_*`` and answered ``TOOLS-MISSING`` on re-engagement.
    These tests pin the bridge at the real ``build_for`` seam.
    """

    def _gated_registry(self) -> SessionToolRegistry:
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="a",
                build=lambda sid, el: _FakeSessionToolA(session_id=sid, event_logger=el),
                requires_capabilities=("scg",),
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="b",
                build=lambda sid, el: _FakeSessionToolB(session_id=sid, event_logger=el),
            )
        )
        return reg

    def test_capability_grant_builds_gated_tool_without_allowlist(self):
        """``scg`` in session caps builds the gated tool even with no allowlist.

        Pre-fix this returned ``[]`` (``allowed_tools`` was the sole gate), which
        is exactly the failure: the runtime grant never reached the build.
        """
        reg = self._gated_registry()
        tools = reg.build_for(
            None,
            session_id="s1",
            event_logger=None,
            session_capabilities=("scg",),
        )
        assert [t.tool_id for t in tools] == ["a"]

    def test_ungated_tool_stays_allowlist_only(self):
        """A tool with no ``requires_capabilities`` is NOT built by caps alone."""
        reg = self._gated_registry()
        tools = reg.build_for(
            None,
            session_id="s1",
            event_logger=None,
            session_capabilities=("scg",),
        )
        assert "b" not in [t.tool_id for t in tools]

    def test_missing_capability_withholds_gated_tool(self):
        """No matching capability ⇒ the gated tool stays hidden (no allowlist)."""
        reg = self._gated_registry()
        assert reg.build_for(None, session_id="s1", event_logger=None) == []
        assert (
            reg.build_for(
                None, session_id="s1", event_logger=None, session_capabilities=("wiki",)
            )
            == []
        )

    def test_allowlist_and_capability_union_without_duplicates(self):
        """A tool selected by BOTH gates is instantiated exactly once."""
        reg = self._gated_registry()
        tools = reg.build_for(
            ["a", "b"],
            session_id="s1",
            event_logger=None,
            session_capabilities=("scg",),
        )
        ids = [t.tool_id for t in tools]
        assert sorted(ids) == ["a", "b"]
        assert ids.count("a") == 1

    def test_partial_capability_subset_required(self):
        """A multi-capability gate needs the FULL subset present in session caps."""
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="a",
                build=lambda sid, el: _FakeSessionToolA(session_id=sid, event_logger=el),
                requires_capabilities=("scg", "wiki"),
            )
        )
        # Only one of two required caps present → withheld.
        assert (
            reg.build_for(
                None, session_id="s1", event_logger=None, session_capabilities=("scg",)
            )
            == []
        )
        # Both present → built.
        tools = reg.build_for(
            None,
            session_id="s1",
            event_logger=None,
            session_capabilities=("scg", "wiki"),
        )
        assert [t.tool_id for t in tools] == ["a"]


# ---------------------------------------------------------------------------
# build_for — explicit-scope structural ceiling (Phase 3)
# ---------------------------------------------------------------------------


class TestBuildForExplicitScopeCeiling:
    """A non-empty ``allowed_tools`` caps the capability gate.

    Regression for Phase 3 (closes the debt): the ``wiki`` plugin
    stamps ``requires-capabilities: ["wiki"]`` on ALL its session tools, so a
    session holding the ``wiki`` capability used to auto-surface the FULL wiki
    retrieval surface onto EVERY agent via the capability gate — even the
    ``wiki-qa`` hypervisor whose AgentDef ``tools:`` deliberately omits retrieval
    tools to force probe-delegation. Once a caller declares an explicit
    ``allowed_tools`` scope, that list is now the structural ceiling: a
    capability-matched tool is built only if it is ALSO in the allowlist. The
    fully-open capability grant survives for the plain-session case
    (``allowed_tools is None``).
    """

    @staticmethod
    def _build(tool_id: str):
        """A builder whose instance reports *tool_id* (not the fake class attr)."""

        def _factory(sid: str, el) -> _FakeSessionTool:
            tool = _FakeSessionTool(session_id=sid, event_logger=el)
            tool.tool_id = tool_id  # type: ignore[misc]
            return tool

        return _factory

    def _wiki_registry(self) -> SessionToolRegistry:
        """Two wiki-gated tools: one the wiki-qa root allows, one it excludes."""
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="wiki_list_pages",
                build=self._build("wiki_list_pages"),
                requires_capabilities=("wiki",),
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="wiki_read_page",
                build=self._build("wiki_read_page"),
                requires_capabilities=("wiki",),
            )
        )
        return reg

    def test_gated_tool_excluded_when_absent_from_nonempty_allowlist(self):
        """The regression this fix targets.

        ``wiki_read_page`` is capability-matched (session holds ``wiki``) but the
        caller's explicit allowlist omits it, so it is NOT built. Pre-fix the
        capability gate overrode the allowlist and surfaced it anyway.
        """
        reg = self._wiki_registry()
        tools = reg.build_for(
            ["wiki_list_pages"],
            session_id="s1",
            event_logger=None,
            session_capabilities=("wiki",),
        )
        ids = [t.tool_id for t in tools]
        assert ids == ["wiki_list_pages"]
        assert "wiki_read_page" not in ids

    def test_gated_tool_included_when_allowlist_is_none(self):
        """Same factory, no explicit scope ⇒ capability gate still fires."""
        reg = self._wiki_registry()
        tools = reg.build_for(
            None,
            session_id="s1",
            event_logger=None,
            session_capabilities=("wiki",),
        )
        assert set(t.tool_id for t in tools) == {"wiki_list_pages", "wiki_read_page"}

    def test_gated_tool_included_when_present_in_nonempty_allowlist(self):
        """The allowlist gate still builds a gated tool that IS listed."""
        reg = self._wiki_registry()
        tools = reg.build_for(
            ["wiki_read_page"],
            session_id="s1",
            event_logger=None,
            session_capabilities=("wiki",),
        )
        assert [t.tool_id for t in tools] == ["wiki_read_page"]

    def test_allowlisted_tool_builds_regardless_of_capabilities(self):
        """A tool named directly in ``allowed_tools`` is unaffected by caps.

        With NO session capabilities, the explicitly-allowed tool still builds
        (allowlist gate), and the unlisted gated tool stays hidden.
        """
        reg = self._wiki_registry()
        tools = reg.build_for(
            ["wiki_list_pages"],
            session_id="s1",
            event_logger=None,
            session_capabilities=(),
        )
        assert [t.tool_id for t in tools] == ["wiki_list_pages"]


# ---------------------------------------------------------------------------
# build_for — unconditional (always-on-by-default) gate
# ---------------------------------------------------------------------------


class TestBuildForUnconditional:
    """An ``unconditional`` factory is the always-on shape.

    ``schedule_trigger`` used to ride the root-only ``extra_session_tools`` seam,
    which spawned sub-agents structurally never inherit. Registered as an
    ordinary ``unconditional`` factory it surfaces to every session EXCEPT one
    under a STRICT explicit scope (an authoritative AgentDef ``tools:``) that
    omits it. The ceiling is ``strict_tool_scope``, NOT bare ``has_explicit_scope``
    — because a PERMISSIVE ``allowed_tools`` (the FE ``context.mcp_tools`` list)
    is only an MCP ceiling and must not cap a built-in session tool (the df875
    law). So the app-builder AgentDef (strict, names it) gets it, a
    differently-scoped strict sub-agent does not, and a permissive FE root
    (mcp_tools, no schedule_trigger) still gets it.
    """

    @staticmethod
    def _build(tool_id: str):
        """A builder whose instance reports *tool_id* (not the fake class attr)."""

        def _factory(sid: str, el) -> _FakeSessionTool:
            tool = _FakeSessionTool(session_id=sid, event_logger=el)
            tool.tool_id = tool_id  # type: ignore[misc]
            return tool

        return _factory

    def _registry(self) -> SessionToolRegistry:
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="schedule_trigger",
                build=self._build("schedule_trigger"),
                unconditional=True,
            )
        )
        reg.register(
            SessionToolFactory(tool_id="b", build=self._build("b"))
        )
        return reg

    def test_surfaces_to_unscoped_session_without_capability(self):
        """No allowlist, no caps ⇒ the unconditional tool builds (root shape)."""
        reg = self._registry()
        tools = reg.build_for(None, session_id="s1", event_logger=None)
        assert [t.tool_id for t in tools] == ["schedule_trigger"]

    def test_surfaces_on_empty_allowlist(self):
        """An empty list is still ``no explicit scope`` ⇒ builds."""
        reg = self._registry()
        tools = reg.build_for([], session_id="s1", event_logger=None)
        assert [t.tool_id for t in tools] == ["schedule_trigger"]

    def test_permissive_root_with_mcp_allowlist_still_gets_it(self):
        """THE df875 regression: a permissive FE root keeps schedule_trigger.

        The console/Aura ALWAYS send a large ``context.mcp_tools`` list
        (permissive: ``strict_tool_scope=False``) that never lists built-ins.
        Under the old ``extra_session_tools`` path those roots got
        ``schedule_trigger`` regardless — Aura's "set an alarm in 10 minutes"
        flow arms a time trigger through it. The permissive allowlist is an MCP
        ceiling only; it must NOT cap the unconditional tool.
        """
        reg = self._registry()
        mcp_only = [f"mcp__server__tool_{i}" for i in range(139)]  # Aura-shaped
        tools = reg.build_for(
            mcp_only,
            session_id="aura-1",
            event_logger=None,
            strict_tool_scope=False,
        )
        assert "schedule_trigger" in [t.tool_id for t in tools]

    def test_strict_scoped_child_without_it_does_not_get_it(self):
        """The load-bearing negative: a STRICT allowlist that omits it caps it.

        A sub-agent whose AgentDef ``tools:`` is authoritative
        (``strict_tool_scope=True``) and omits it does NOT inherit the
        unconditional tool.
        """
        reg = self._registry()
        tools = reg.build_for(
            ["b"], session_id="s1", event_logger=None, strict_tool_scope=True
        )
        assert [t.tool_id for t in tools] == ["b"]
        assert "schedule_trigger" not in [t.tool_id for t in tools]

    def test_strict_scoped_child_naming_it_binds_it(self):
        """The load-bearing positive: a strict child whose allowlist names it binds it.

        This was IMPOSSIBLE while the tool rode ``extra_session_tools`` (root
        only). Now it flows through the ordinary allowlist gate — the app-builder
        AgentDef shape.
        """
        reg = self._registry()
        tools = reg.build_for(
            ["schedule_trigger"],
            session_id="sess-9",
            event_logger=None,
            strict_tool_scope=True,
        )
        assert [t.tool_id for t in tools] == ["schedule_trigger"]
        assert tools[0].session_id == "sess-9"

    def test_ids_for_matches_build_for_under_both_scopes(self):
        """``ids_for`` (the InstructionContext.tools source) agrees with builds.

        The drift law: the operator's ``{{ tools }}`` catalog and the actual
        build must resolve identically under BOTH strict and permissive scope.
        """
        reg = self._registry()
        mcp_only = [f"mcp__server__tool_{i}" for i in range(139)]
        # Permissive root: surfaces under both surfaces.
        assert "schedule_trigger" in reg.ids_for(None)
        assert "schedule_trigger" in reg.ids_for(mcp_only, strict_tool_scope=False)
        # Strict scope: only when named.
        assert "schedule_trigger" not in reg.ids_for(["b"], strict_tool_scope=True)
        assert "schedule_trigger" in reg.ids_for(
            ["schedule_trigger"], strict_tool_scope=True
        )

    def test_coexists_with_capability_gate(self):
        """Unconditional + capability factories both auto-surface at an un-scoped session."""
        reg = self._registry()
        reg.register(
            SessionToolFactory(
                tool_id="scg_route",
                build=self._build("scg_route"),
                requires_capabilities=("scg",),
            )
        )
        ids = {
            t.tool_id
            for t in reg.build_for(
                None, session_id="s1", event_logger=None, session_capabilities=("scg",)
            )
        }
        assert ids == {"schedule_trigger", "scg_route"}

    def test_capability_gate_keeps_199_ceiling_under_permissive_scope(self):
        """A capability tool (NOT unconditional) still respects the ceiling.

        The df875 relaxation is for ``unconditional`` factories ONLY — a
        permissive allowlist that omits a capability-gated tool still caps it
        (that gate keeps ``has_explicit_scope`` semantics), so that ceiling is untouched.
        """
        reg = self._registry()
        reg.register(
            SessionToolFactory(
                tool_id="scg_route",
                build=self._build("scg_route"),
                requires_capabilities=("scg",),
            )
        )
        mcp_only = [f"mcp__server__tool_{i}" for i in range(3)]
        ids = {
            t.tool_id
            for t in reg.build_for(
                mcp_only,
                session_id="s1",
                event_logger=None,
                session_capabilities=("scg",),
                strict_tool_scope=False,
            )
        }
        # Unconditional surfaces (permissive), capability tool is capped.
        assert "schedule_trigger" in ids
        assert "scg_route" not in ids


# ---------------------------------------------------------------------------
# load_entry — capability stamping
# ---------------------------------------------------------------------------


class TestLoadEntryCapabilityStamp:
    def test_load_entry_stamps_requires_capabilities(self):
        """``load_entry(..., requires_capabilities=)`` makes the tool cap-gated."""
        module_name = "mewbo_test_session_tools_cap_fixture"
        mod = types.ModuleType(module_name)
        mod._FakeSessionTool = _FakeSessionTool  # type: ignore[attr-defined]
        sys.modules[module_name] = mod
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {
                    "tool_id": "fake_tool",
                    "module": module_name,
                    "class": "_FakeSessionTool",
                },
                requires_capabilities=("scg",),
            )
            # No allowlist, but the capability is present → built.
            tools = reg.build_for(
                None,
                session_id="s1",
                event_logger=None,
                session_capabilities=("scg",),
            )
            assert len(tools) == 1
            assert tools[0].tool_id == "fake_tool"
            # Without the capability → withheld.
            assert reg.build_for(None, session_id="s1", event_logger=None) == []
        finally:
            sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# load_entry
# ---------------------------------------------------------------------------


class TestLoadEntry:
    def _make_fixture_module(self, name: str, cls: type) -> None:
        """Install a throwaway module into ``sys.modules`` for importlib."""
        mod = types.ModuleType(name)
        setattr(mod, cls.__name__, cls)
        sys.modules[name] = mod

    def test_load_entry_valid_record_registers_factory(self):
        module_name = "mewbo_test_session_tools_fixture"
        self._make_fixture_module(module_name, _FakeSessionTool)
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {
                    "tool_id": "fake_tool",
                    "module": module_name,
                    "class": "_FakeSessionTool",
                }
            )
            tools = reg.build_for(
                ["fake_tool"], session_id="s1", event_logger=None
            )
            assert len(tools) == 1
            assert isinstance(tools[0], _FakeSessionTool)
            assert tools[0].session_id == "s1"
        finally:
            sys.modules.pop(module_name, None)

    def test_load_entry_unconditional_surfaces_without_the_bundle_capability(self):
        """``"unconditional": true`` lets ONE entry opt out of its bundle's gate.

        The manifest is loaded with a non-empty ``requires_capabilities`` (the
        bundle-wide gate) and the session advertises NO capability, so only the
        per-entry flag can explain the tool being selected.
        """
        module_name = "mewbo_test_session_tools_uncond"
        self._make_fixture_module(module_name, _FakeSessionTool)
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {
                    "tool_id": "fake_tool",
                    "module": module_name,
                    "class": "_FakeSessionTool",
                    "unconditional": True,
                },
                requires_capabilities=("wiki",),
            )
            assert reg.ids_for(None, session_capabilities=()) == ["fake_tool"]
        finally:
            sys.modules.pop(module_name, None)

    def test_load_entry_without_the_flag_keeps_the_bundle_capability_gate(self):
        """Absent ⇒ False ⇒ every existing manifest entry behaves byte-identically."""
        module_name = "mewbo_test_session_tools_gated"
        self._make_fixture_module(module_name, _FakeSessionTool)
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {
                    "tool_id": "fake_tool",
                    "module": module_name,
                    "class": "_FakeSessionTool",
                },
                requires_capabilities=("wiki",),
            )
            assert reg.ids_for(None, session_capabilities=()) == []
            assert reg.ids_for(None, session_capabilities=("wiki",)) == ["fake_tool"]
        finally:
            sys.modules.pop(module_name, None)

    def test_load_entry_unconditional_is_still_capped_by_a_strict_scope(self):
        """The df875 ceiling holds: a STRICT AgentDef scope must name the tool.

        This is what keeps the ``wiki-qa`` root's ``QA_TOOLS`` ceiling load-bearing
        — the ceiling that forces delegation to probes — while a PERMISSIVE
        allowlist (the FE default) still surfaces the tool.
        """
        module_name = "mewbo_test_session_tools_strict"
        self._make_fixture_module(module_name, _FakeSessionTool)
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {
                    "tool_id": "fake_tool",
                    "module": module_name,
                    "class": "_FakeSessionTool",
                    "unconditional": True,
                },
                requires_capabilities=("wiki",),
            )
            assert reg.ids_for(["other_tool"], strict_tool_scope=True) == []
            assert reg.ids_for(["other_tool"], strict_tool_scope=False) == ["fake_tool"]
        finally:
            sys.modules.pop(module_name, None)

    def test_load_entry_unknown_key_is_skipped(self):
        """``extra="forbid"`` — a misspelt flag must be loud, not silently dropped."""
        reg = SessionToolRegistry()
        reg.load_entry(
            {
                "tool_id": "fake_tool",
                "module": "os",
                "class": "PathLike",
                "uncondtional": True,  # typo
            }
        )
        assert reg.ids_for(None) == []

    def test_load_entry_missing_tool_id_is_skipped(self):
        reg = SessionToolRegistry()
        reg.load_entry({"module": "os", "class": "PathLike"})
        assert reg.build_for(["anything"], session_id="s1", event_logger=None) == []

    def test_load_entry_missing_module_is_skipped(self):
        reg = SessionToolRegistry()
        reg.load_entry({"tool_id": "fake_tool", "class": "_FakeSessionTool"})
        assert reg.build_for(["fake_tool"], session_id="s1", event_logger=None) == []

    def test_load_entry_missing_class_is_skipped(self):
        reg = SessionToolRegistry()
        reg.load_entry(
            {"tool_id": "fake_tool", "module": "mewbo_core.session_tools"}
        )
        assert reg.build_for(["fake_tool"], session_id="s1", event_logger=None) == []

    def test_load_entry_unimportable_module_is_skipped(self):
        reg = SessionToolRegistry()
        reg.load_entry(
            {
                "tool_id": "fake_tool",
                "module": "definitely_not_a_real_module_xyz",
                "class": "WhateverClass",
            }
        )
        assert reg.build_for(["fake_tool"], session_id="s1", event_logger=None) == []

    def test_load_entry_missing_class_attr_is_skipped(self):
        module_name = "mewbo_test_session_tools_attr_fixture"
        self._make_fixture_module(module_name, _FakeSessionTool)
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {
                    "tool_id": "fake_tool",
                    "module": module_name,
                    "class": "DoesNotExist",
                }
            )
            assert (
                reg.build_for(["fake_tool"], session_id="s1", event_logger=None) == []
            )
        finally:
            sys.modules.pop(module_name, None)

    def test_load_entry_first_wins_no_override(self):
        """Second registration of the same tool_id is ignored."""
        module_a = "mewbo_test_session_tools_first"
        module_b = "mewbo_test_session_tools_second"
        self._make_fixture_module(module_a, _FakeSessionToolA)
        # module_b exports a class also named "_FakeSessionToolA" but it's B.
        mod_b = types.ModuleType(module_b)
        mod_b._FakeSessionToolA = _FakeSessionToolB  # type: ignore[attr-defined]
        sys.modules[module_b] = mod_b
        try:
            reg = SessionToolRegistry()
            reg.load_entry(
                {"tool_id": "a", "module": module_a, "class": "_FakeSessionToolA"}
            )
            reg.load_entry(
                {"tool_id": "a", "module": module_b, "class": "_FakeSessionToolA"}
            )
            tools = reg.build_for(["a"], session_id="s1", event_logger=None)
            assert len(tools) == 1
            assert isinstance(tools[0], _FakeSessionToolA)
        finally:
            sys.modules.pop(module_a, None)
            sys.modules.pop(module_b, None)


# ---------------------------------------------------------------------------
# capabilities_for (request-scoped capability derivation)
# ---------------------------------------------------------------------------


class TestCapabilitiesFor:
    def _gated_registry(self) -> SessionToolRegistry:
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="wiki_search_pages",
                build=lambda sid, el: _FakeSessionToolA(session_id=sid, event_logger=el),
                requires_capabilities=("wiki",),
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="scg_route",
                build=lambda sid, el: _FakeSessionToolB(session_id=sid, event_logger=el),
                requires_capabilities=("scg",),
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="shell",
                build=lambda sid, el: _FakeSessionTool(session_id=sid, event_logger=el),
            )
        )
        return reg

    def test_unknown_tool_id_contributes_nothing(self):
        reg = self._gated_registry()
        assert reg.capabilities_for(["nonexistent"]) == ()

    def test_ungated_tool_contributes_nothing(self):
        """A tool with empty requires_capabilities derives no capability."""
        reg = self._gated_registry()
        assert reg.capabilities_for(["shell"]) == ()

    def test_gated_tool_derives_its_capability(self):
        reg = self._gated_registry()
        assert reg.capabilities_for(["wiki_search_pages"]) == ("wiki",)

    def test_multiple_tools_union_and_dedupe(self):
        reg = self._gated_registry()
        caps = reg.capabilities_for(["wiki_search_pages", "scg_route", "shell"])
        assert caps == ("scg", "wiki")

    def test_empty_input_returns_empty(self):
        reg = self._gated_registry()
        assert reg.capabilities_for([]) == ()

    def test_result_is_sorted(self):
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="a",
                build=lambda sid, el: _FakeSessionToolA(session_id=sid, event_logger=el),
                requires_capabilities=("zzz", "aaa"),
            )
        )
        assert reg.capabilities_for(["a"]) == ("aaa", "zzz")


# ---------------------------------------------------------------------------
# capability_mode privilege tier (Phase 1a — the two-surface
# fix: session tools attenuate under a read_only spawn just like registry tools)
# ---------------------------------------------------------------------------


class TestBuildForCapabilityMode:
    @staticmethod
    def _reg() -> SessionToolRegistry:
        """A write-tier (default) + a declared-read + an unconditional factory."""
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="a",  # default tier -> execute (a write-by-nature action)
                build=lambda sid, el: _FakeSessionToolA(session_id=sid, event_logger=el),
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="b",  # explicitly declared read-tier
                build=lambda sid, el: _FakeSessionToolB(session_id=sid, event_logger=el),
                capability="read",
            )
        )
        reg.register(
            SessionToolFactory(
                tool_id="fake_tool",  # unconditional, default execute tier
                build=lambda sid, el: _FakeSessionTool(session_id=sid, event_logger=el),
                unconditional=True,
            )
        )
        return reg

    def test_factory_default_tier_is_execute(self):
        f = SessionToolFactory(tool_id="x", build=lambda s, e: None)
        assert f.capability_tier() == "execute"

    def test_factory_declared_tier_wins(self):
        f = SessionToolFactory(tool_id="x", build=lambda s, e: None, capability="read")
        assert f.capability_tier() == "read"

    def test_read_only_drops_default_execute_keeps_declared_read(self):
        reg = self._reg()
        ids = reg.ids_for(["a", "b"], capability_mode="read_only")
        assert ids == ["b"]  # 'a' (execute) dropped, 'b' (read) survives

    def test_read_only_with_no_read_tool_yields_zero(self):
        # Safe-deny: nothing declared read ⇒ read_only admits ZERO session tools,
        # including a named write tool AND the unconditional (execute) one.
        reg = self._reg()
        assert reg.ids_for(["a"], capability_mode="read_only") == []
        assert reg.ids_for(None, capability_mode="read_only") == []  # unconditional dropped

    def test_execute_and_all_are_byte_identical(self):
        reg = self._reg()
        base = reg.ids_for(["a", "b"])  # default "all"
        assert reg.ids_for(["a", "b"], capability_mode="all") == base
        assert reg.ids_for(["a", "b"], capability_mode="execute") == base
        assert set(base) == {"a", "b", "fake_tool"}  # unconditional auto-surfaces

    def test_capability_mode_cannot_resurrect_named_write_tool(self):
        # 'a' is named in the allowlist but read_only strips it anyway.
        reg = self._reg()
        assert "a" not in reg.ids_for(["a", "b"], capability_mode="read_only")

    def test_build_for_matches_ids_for_under_mode(self):
        reg = self._reg()
        built = reg.build_for(
            ["a", "b"], session_id="s1", event_logger=None, capability_mode="read_only"
        )
        assert [t.tool_id for t in built] == ["b"]
