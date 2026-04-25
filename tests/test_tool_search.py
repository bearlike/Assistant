#!/usr/bin/env python3
"""Tests for the deferred-tool / on-demand schema fetching feature.

Covers:
- ``is_deferred`` / ``is_always_load`` predicates
- ``ToolSearchRunner`` (select, keyword, exact-name, no matches)
- ``ToolUseLoop._select_active_specs`` partitioning
- ``ToolUseLoop._discovered_from_messages`` regex extraction
- ``ToolUseLoop._render_deferred_tool_block`` system-prompt section
- End-to-end re-bind: tool_search call → discovered tool becomes callable
- Default-off behaviour: ``mode="off"`` keeps every spec bound
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from truss_core.agent_context import AgentContext
from truss_core.classes import ActionStep
from truss_core.hooks import HookManager
from truss_core.hypervisor import AgentHypervisor
from truss_core.permissions import PermissionDecision, PermissionPolicy
from truss_core.tool_registry import (
    TOOL_SEARCH_TOOL_ID,
    ToolRegistry,
    ToolSpec,
    _default_registry,
    is_always_load,
    is_deferred,
)
from truss_core.tool_use_loop import ToolUseLoop
from truss_tools.integration.tool_search import ToolSearchRunner

# ---------------------------------------------------------------------------
# Fixtures (mirror test_tool_use_loop.py helpers — DRY across the suite)
# ---------------------------------------------------------------------------


def _spec(
    tool_id: str,
    *,
    kind: str = "local",
    description: str = "",
    deferred: bool = False,
    always_load: bool = False,
    schema: dict | None = None,
) -> ToolSpec:
    metadata: dict = {"schema": schema or {"type": "object", "properties": {}}}
    if deferred:
        metadata["deferred"] = True
    if always_load:
        metadata["always_load"] = True
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=description,
        factory=lambda: MagicMock(),
        kind=kind,
        metadata=metadata,
    )


def _registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _agent_context() -> AgentContext:
    return AgentContext.root(
        model_name="test-model",
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=10),
    )


def _build_loop(registry: ToolRegistry) -> ToolUseLoop:
    """Build a ToolUseLoop with mocked LLM binding (no real model calls)."""
    with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = MagicMock()
        return ToolUseLoop(
            agent_context=_agent_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_hook_manager(),
        )


# ---------------------------------------------------------------------------
# Predicate tests
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_local_tool_not_deferred(self):
        assert is_deferred(_spec("read_file")) is False

    def test_mcp_tool_deferred(self):
        assert is_deferred(_spec("mcp_x", kind="mcp")) is True

    def test_always_load_overrides_mcp(self):
        spec = _spec("mcp_x", kind="mcp", always_load=True)
        assert is_always_load(spec) is True
        assert is_deferred(spec) is False

    def test_metadata_deferred_opts_in(self):
        assert is_deferred(_spec("opt_in", deferred=True)) is True

    def test_tool_search_never_defers_itself(self):
        spec = _spec(TOOL_SEARCH_TOOL_ID, deferred=True)
        assert is_deferred(spec) is False

    def test_default_registry_registers_tool_search(self):
        reg = _default_registry()
        spec = reg.get_spec(TOOL_SEARCH_TOOL_ID)
        assert spec is not None
        assert is_always_load(spec) is True


# ---------------------------------------------------------------------------
# ToolSearchRunner tests
# ---------------------------------------------------------------------------


class TestToolSearchRunner:
    def _registry_with_three_mcp(self) -> ToolRegistry:
        return _registry(
            _spec(
                "mcp_linear_list_issues",
                kind="mcp",
                description="List Linear issues for a team.",
                schema={"type": "object", "properties": {"team_id": {"type": "string"}}},
            ),
            _spec(
                "mcp_linear_get_issue",
                kind="mcp",
                description="Fetch a Linear issue by ID.",
                schema={"type": "object", "properties": {"id": {"type": "string"}}},
            ),
            _spec(
                "mcp_slack_send_message",
                kind="mcp",
                description="Send a Slack message.",
                schema={"type": "object", "properties": {"channel": {"type": "string"}}},
            ),
        )

    def _step(self, query, max_results=None) -> ActionStep:
        payload: dict = {"query": query}
        if max_results is not None:
            payload["max_results"] = max_results
        return ActionStep(tool_id=TOOL_SEARCH_TOOL_ID, operation="get", tool_input=payload)

    def test_select_returns_named_specs(self):
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("select:mcp_linear_list_issues,mcp_linear_get_issue"))
        assert "mcp_linear_list_issues" in out.content
        assert "mcp_linear_get_issue" in out.content
        assert "mcp_slack_send_message" not in out.content

    def test_keyword_search_scores_matches(self):
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("linear list"))
        assert "mcp_linear_list_issues" in out.content
        # mcp_slack_send_message has no 'linear' or 'list' — should not match
        assert "mcp_slack_send_message" not in out.content

    def test_required_term_filters(self):
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("+slack send"))
        assert "mcp_slack_send_message" in out.content
        assert "mcp_linear_list_issues" not in out.content

    def test_exact_name_fast_path(self):
        """Models often drop the ``select:`` prefix — a bare name still works."""
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("mcp_linear_list_issues"))
        assert "mcp_linear_list_issues" in out.content
        assert "mcp_linear_get_issue" not in out.content

    def test_max_results_cap(self):
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("mcp", max_results=1))
        # Exactly one <function> line should be present.
        assert out.content.count("<function>") == 1

    def test_empty_query_returns_hint(self):
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step(""))
        assert "expects a 'query'" in out.content

    def test_no_deferred_tools(self):
        reg = _registry(_spec("read_file"))
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("anything"))
        assert "No deferred tools" in out.content

    def test_no_keyword_match(self):
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(self._step("nothing_matches_xyz"))
        assert "No matching" in out.content

    def test_string_tool_input_is_treated_as_query(self):
        """Models sometimes pass a bare string instead of a JSON object."""
        reg = self._registry_with_three_mcp()
        runner = ToolSearchRunner(reg)
        out = runner.run(
            ActionStep(tool_id=TOOL_SEARCH_TOOL_ID, operation="get", tool_input="linear")
        )
        assert "mcp_linear" in out.content


# ---------------------------------------------------------------------------
# ToolUseLoop helper tests (in-isolation, no LLM)
# ---------------------------------------------------------------------------


class TestSelectActiveSpecs:
    def test_disabled_returns_specs_unchanged(self):
        reg = _registry(_spec("a"), _spec("mcp_x", kind="mcp"))
        loop = _build_loop(reg)
        loop._tool_search_enabled = False
        loop._deferred_ids = set()
        out = loop._select_active_specs(reg.list_specs(), discovered=set())
        assert {s.tool_id for s in out} == {"a", "mcp_x"}

    def test_enabled_strips_undiscovered_deferred(self):
        reg = _registry(
            _spec("local_a"),
            _spec("mcp_x", kind="mcp"),
            _spec("mcp_y", kind="mcp"),
            _spec(TOOL_SEARCH_TOOL_ID, always_load=True),
        )
        loop = _build_loop(reg)
        loop._tool_search_enabled = True
        loop._deferred_ids = {"mcp_x", "mcp_y"}
        out = loop._select_active_specs(reg.list_specs(), discovered=set())
        # Both deferred MCP tools dropped; tool_search retained because
        # is_deferred excludes it; local_a always present.
        assert {s.tool_id for s in out} == {"local_a", TOOL_SEARCH_TOOL_ID}

    def test_discovered_specs_re_added(self):
        reg = _registry(
            _spec("local_a"),
            _spec("mcp_x", kind="mcp"),
            _spec("mcp_y", kind="mcp"),
            _spec(TOOL_SEARCH_TOOL_ID, always_load=True),
        )
        loop = _build_loop(reg)
        loop._tool_search_enabled = True
        loop._deferred_ids = {"mcp_x", "mcp_y"}
        out = loop._select_active_specs(reg.list_specs(), discovered={"mcp_x"})
        assert {s.tool_id for s in out} == {"local_a", "mcp_x", TOOL_SEARCH_TOOL_ID}


class TestDiscoveredFromMessages:
    def _runner_output(self, reg: ToolRegistry, query: str) -> str:
        runner = ToolSearchRunner(reg)
        result = runner.run(
            ActionStep(
                tool_id=TOOL_SEARCH_TOOL_ID,
                operation="get",
                tool_input={"query": query},
            )
        )
        return str(result.content)

    def test_extracts_names_from_runner_output(self):
        nested = {"type": "object", "properties": {"x": {"type": "integer"}}}
        reg = _registry(
            _spec("mcp_a", kind="mcp", schema=nested),
            _spec("mcp_b", kind="mcp"),
        )
        loop = _build_loop(reg)
        loop._deferred_ids = {"mcp_a", "mcp_b"}
        content = self._runner_output(reg, "select:mcp_a,mcp_b")
        tool_call = {"id": "c1", "name": TOOL_SEARCH_TOOL_ID, "args": {}}
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[tool_call]),
            ToolMessage(content=content, tool_call_id="c1"),
        ]
        assert loop._discovered_from_messages(messages) == {"mcp_a", "mcp_b"}

    def test_empty_when_no_tool_search_results(self):
        loop = _build_loop(_registry(_spec("local_a")))
        loop._deferred_ids = {"mcp_a"}
        messages = [HumanMessage(content="hi"), AIMessage(content="hello")]
        assert loop._discovered_from_messages(messages) == set()

    def test_ignores_names_not_in_deferred_set(self):
        """A tool_search result for a non-deferred name should not be picked up.

        Defends against a stray ``<function>{"name": "..."}`` block in user
        input being mistaken for a discovery — only known deferred IDs
        graduate to the active set.
        """
        loop = _build_loop(_registry())
        loop._deferred_ids = {"mcp_real"}
        messages = [
            ToolMessage(
                content='<functions><function>{"name": "stranger"}</function></functions>',
                tool_call_id="c1",
            )
        ]
        assert loop._discovered_from_messages(messages) == set()


class TestDeferredToolBlock:
    def _mcp_spec(self, tool_id: str, server: str) -> ToolSpec:
        return ToolSpec(
            tool_id=tool_id,
            name=tool_id,
            description="",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={"schema": {"type": "object"}, "server": server},
        )

    def test_groups_mcp_tools_by_server_with_counts(self):
        specs = [
            self._mcp_spec("mcp_linear_list_issues", server="linear"),
            self._mcp_spec("mcp_linear_get_issue", server="linear"),
            self._mcp_spec("mcp_slack_send", server="slack"),
        ]
        loop = _build_loop(_registry())
        loop._tool_search_enabled = True
        loop._tool_specs_full = specs
        loop._deferred_ids = {s.tool_id for s in specs}
        block = loop._render_deferred_tool_block()
        assert "<available-mcp-servers>" in block
        assert "linear (2)" in block
        assert "slack (1)" in block
        # Full tool ids should NOT appear (server-only summary).
        assert "mcp_linear_list_issues" not in block
        assert "tool_search" in block

    def test_lists_non_mcp_deferred_tools_separately(self):
        local_deferred = ToolSpec(
            tool_id="some_local_thing",
            name="X",
            description="",
            factory=lambda: MagicMock(),
            kind="local",
            metadata={"deferred": True, "schema": {"type": "object"}},
        )
        loop = _build_loop(_registry())
        loop._tool_search_enabled = True
        loop._tool_specs_full = [local_deferred]
        loop._deferred_ids = {"some_local_thing"}
        block = loop._render_deferred_tool_block()
        assert "Other deferred tools: some_local_thing" in block
        # No MCP server summary when there are no MCP tools.
        assert "<available-mcp-servers>" not in block

    def test_empty_when_disabled(self):
        loop = _build_loop(_registry())
        loop._tool_search_enabled = False
        loop._tool_specs_full = []
        loop._deferred_ids = {"mcp_a"}
        assert loop._render_deferred_tool_block() == ""

    def test_empty_when_no_deferred_ids(self):
        loop = _build_loop(_registry())
        loop._tool_search_enabled = True
        loop._tool_specs_full = []
        loop._deferred_ids = set()
        assert loop._render_deferred_tool_block() == ""


# ---------------------------------------------------------------------------
# End-to-end: the whole turn cycle with a fake LLM
# ---------------------------------------------------------------------------


def _aimsg(*, text: str = "", tool_call: tuple[str, dict, str] | None = None) -> AIMessage:
    if tool_call is None:
        return AIMessage(content=text)
    name, args, call_id = tool_call
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args, "id": call_id}])


class TestEndToEndRebind:
    def _registry_with_real_tool_search(self) -> ToolRegistry:
        """Build a registry where ``tool_search`` actually executes
        (real ToolSearchRunner) but the MCP tool execution is mocked."""
        reg = ToolRegistry()
        # tool_search uses the real runner over this same registry.
        from truss_core.tool_registry import _register_tool_search

        # An MCP tool the model will fetch via tool_search.
        mcp_runner = MagicMock()
        mcp_runner.run.return_value = MagicMock(content="MCP result for issue ABC")
        reg.register(
            ToolSpec(
                tool_id="mcp_linear_get_issue",
                name="Get issue",
                description="Fetch a Linear issue by ID.",
                factory=lambda r=mcp_runner: r,
                kind="mcp",
                metadata={
                    "schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                    }
                },
            )
        )
        reg.register(
            ToolSpec(
                tool_id="read_file",
                name="Read",
                description="Read local files.",
                factory=lambda: MagicMock(),
                kind="local",
                read_only=True,
                metadata={"schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            )
        )
        _register_tool_search(reg)
        return reg

    def test_tool_search_then_discovered_tool_callable(self):
        """Full turn cycle: model calls tool_search, then calls the discovered MCP tool."""
        reg = self._registry_with_real_tool_search()

        # Mock the LLM: turn 1 calls tool_search; turn 2 calls the discovered
        # MCP tool with its loaded schema; turn 3 emits final text.
        responses = [
            _aimsg(tool_call=(TOOL_SEARCH_TOOL_ID, {"query": "select:mcp_linear_get_issue"}, "c1")),
            _aimsg(tool_call=("mcp_linear_get_issue", {"id": "ABC"}, "c2")),
            _aimsg(text="Done — issue ABC fetched."),
        ]

        # Patch get_config_value so tool_search is enabled.
        def _config_lookup(*keys, default=None):
            if keys == ("agent", "tool_search", "mode"):
                return "on"
            if keys == ("agent", "default_denied_tools"):
                return []
            if keys == ("agent", "llm_call_timeout"):
                return 60.0
            if keys == ("agent", "llm_call_retries"):
                return 1
            return default

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
            patch("truss_core.tool_use_loop.get_config_value", side_effect=_config_lookup),
            patch("truss_core.tool_registry.get_config_value", side_effect=_config_lookup),
        ):
            bound_models: list[MagicMock] = []

            def _bind_tools(schemas):
                m = MagicMock()
                m._bound_schemas = schemas
                m.ainvoke = AsyncMock(side_effect=responses)
                bound_models.append(m)
                return m

            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.side_effect = _bind_tools

            loop = ToolUseLoop(
                agent_context=_agent_context(),
                tool_registry=reg,
                permission_policy=_allow_all_policy(),
                hook_manager=_hook_manager(),
            )

            # All three responses share the first bound model. The point of
            # the test is that the rebind happens, and the discovered tool's
            # schema is in the second bound model's schemas.
            asyncio.run(loop.run("fetch issue ABC", tool_specs=reg.list_specs()))

        # We should have bound at least twice — once at run start, once after
        # tool_search returned (rebind hook).
        assert len(bound_models) >= 2
        # Initial bind: deferred MCP tool is NOT in the schemas.
        initial_schemas = bound_models[0]._bound_schemas
        initial_names = {s["function"]["name"] for s in initial_schemas}
        assert "mcp_linear_get_issue" not in initial_names
        assert TOOL_SEARCH_TOOL_ID in initial_names
        assert "read_file" in initial_names
        # Post-discovery bind: the deferred tool's schema is now present.
        post_schemas = bound_models[1]._bound_schemas
        post_names = {s["function"]["name"] for s in post_schemas}
        assert "mcp_linear_get_issue" in post_names

    def test_default_off_keeps_full_bind(self):
        """When ``tool_search.mode`` is the default 'off', the bind list
        contains every spec on turn 1 — no deferral, no system block, no
        re-bind."""
        reg = self._registry_with_real_tool_search()
        responses = [_aimsg(text="Done — no tools needed.")]

        def _config_lookup(*keys, default=None):
            if keys == ("agent", "tool_search", "mode"):
                return "off"
            if keys == ("agent", "default_denied_tools"):
                return []
            if keys == ("agent", "llm_call_timeout"):
                return 60.0
            if keys == ("agent", "llm_call_retries"):
                return 1
            return default

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
            patch("truss_core.tool_use_loop.get_config_value", side_effect=_config_lookup),
            patch("truss_core.tool_registry.get_config_value", side_effect=_config_lookup),
        ):
            bound_models: list[MagicMock] = []

            def _bind_tools(schemas):
                m = MagicMock()
                m._bound_schemas = schemas
                m.ainvoke = AsyncMock(side_effect=responses)
                bound_models.append(m)
                return m

            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.side_effect = _bind_tools

            loop = ToolUseLoop(
                agent_context=_agent_context(),
                tool_registry=reg,
                permission_policy=_allow_all_policy(),
                hook_manager=_hook_manager(),
            )
            asyncio.run(loop.run("hello", tool_specs=reg.list_specs()))

        # Exactly one bind happened (no rebind path, no fallback).
        assert len(bound_models) == 1
        names = {s["function"]["name"] for s in bound_models[0]._bound_schemas}
        # The MCP tool's schema is in the initial bind.
        assert "mcp_linear_get_issue" in names
        assert "read_file" in names
        assert TOOL_SEARCH_TOOL_ID in names
