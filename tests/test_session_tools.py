#!/usr/bin/env python3
"""Unit tests for ``truss_core.session_tools``.

Covers the ``SessionToolRegistry`` contract exposed in Task 4 of the
widget-builder-as-plugin refactor:
- empty-registry behaviour,
- manifest-driven ``load_entry`` (happy path + malformed records),
- allowlist-filtered ``build_for`` per-session instantiation.
"""

from __future__ import annotations

import sys
import types

from truss_core.classes import ActionStep
from truss_core.common import MockSpeaker
from truss_core.session_tools import SessionToolFactory, SessionToolRegistry

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
# load_entry
# ---------------------------------------------------------------------------


class TestLoadEntry:
    def _make_fixture_module(self, name: str, cls: type) -> None:
        """Install a throwaway module into ``sys.modules`` for importlib."""
        mod = types.ModuleType(name)
        setattr(mod, cls.__name__, cls)
        sys.modules[name] = mod

    def test_load_entry_valid_record_registers_factory(self):
        module_name = "truss_test_session_tools_fixture"
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
            {"tool_id": "fake_tool", "module": "truss_core.session_tools"}
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
        module_name = "truss_test_session_tools_attr_fixture"
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
        module_a = "truss_test_session_tools_first"
        module_b = "truss_test_session_tools_second"
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
