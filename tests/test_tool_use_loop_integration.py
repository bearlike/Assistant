#!/usr/bin/env python3
"""Integration tests for tool_use_loop uncovered behaviors.

Targets (by missing-line analysis):
- _partition_tool_calls: concurrency_safe vs exclusive batching
- _safe_execute: timeout path, exception path
- Doom-loop halt (identical tool+input repeated to threshold)
- Interrupt step draining between loop turns
- Message queue draining between loop turns
- _check_file_read_cache / _populate_file_read_cache / _CachedFileRead
- _append_lsp_feedback (module-level function)
- _coerce_mcp_tool_input edge cases (JSON-string, missing-required, array coercion)
- _infer_operation edge cases (fetch, query, lookup keywords)
- repair_tool_pairing driving orphan-drop and stub-insertion
- _should_compact_messages (token threshold logic)
- _plan_mode_permission branches (shell allowlist, MCP, agent mgmt)
- User message-queue draining in loop
- Interrupt-step clearing in loop
- Tool execution: arun path, sync tool.run via asyncio.to_thread
- Permission ASK + approval_callback (approved / denied)
- Failure feedback SystemMessage injection after failed tool
- _extract_text_content with list content and no-content placeholder
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep
from mewbo_core.context import ContextSnapshot
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHypervisor
from mewbo_core.llm_resilience import DoomLoopGuard, repair_tool_pairing
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.token_budget import TokenBudget
from mewbo_core.tool_registry import ToolRegistry, ToolSpec
from mewbo_core.tool_use_loop import (
    ToolUseLoop,
    _append_lsp_feedback,
    _CachedFileRead,
    _coerce_mcp_tool_input,
    _infer_operation,
)

# ---------------------------------------------------------------------------
# Shared helpers (DRY — mirrors test_tool_use_loop.py conventions)
# ---------------------------------------------------------------------------


def _make_context() -> ContextSnapshot:
    return ContextSnapshot(
        summary=None,
        recent_events=[],
        selected_events=None,
        events=[],
        budget=TokenBudget(
            total_tokens=0,
            summary_tokens=0,
            event_tokens=0,
            context_window=128000,
            remaining_tokens=128000,
            utilization=0.0,
            threshold=0.8,
        ),
    )


def _make_spec(
    tool_id: str = "test_tool",
    description: str = "A test tool",
    concurrency_safe: bool = True,
    read_only: bool = False,
    kind: str = "local",
    metadata: dict | None = None,
    timeout: float = 120.0,
) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=description,
        factory=lambda: MagicMock(),
        enabled=True,
        kind=kind,
        concurrency_safe=concurrency_safe,
        read_only=read_only,
        timeout=timeout,
        metadata=metadata
        or {
            "schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            }
        },
    )


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _deny_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.DENY
    return policy


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _make_agent_context(
    *,
    model_name: str = "test-model",
    should_cancel=None,
    max_depth: int = 5,
    event_logger=None,
) -> AgentContext:
    """Create a root AgentContext for tests."""
    return AgentContext.root(
        model_name=model_name,
        max_depth=max_depth,
        should_cancel=should_cancel,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=event_logger,
    )


def _text_response(content: str) -> AIMessage:
    """AIMessage with no tool calls (text-only)."""
    return AIMessage(content=content)


def _tool_call_response(tool_id: str, args: dict, call_id: str = "call_1") -> AIMessage:
    """AIMessage with a single tool call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_id, "args": args, "id": call_id}],
    )


def _make_loop(
    registry: ToolRegistry | None = None,
    *,
    policy: PermissionPolicy | None = None,
    hook_manager: HookManager | None = None,
    agent_context: AgentContext | None = None,
    event_logger=None,
    approval_callback=None,
    cwd: str | None = None,
    session_id: str | None = None,
) -> ToolUseLoop:
    """Build a ToolUseLoop with sensible defaults."""
    if agent_context is None:
        agent_context = _make_agent_context(event_logger=event_logger)
    return ToolUseLoop(
        agent_context=agent_context,
        tool_registry=registry or _make_registry(),
        permission_policy=policy or _allow_all_policy(),
        hook_manager=hook_manager or _make_hook_manager(),
        approval_callback=approval_callback,
        cwd=cwd,
        session_id=session_id,
    )


def _build_bound(responses: list[AIMessage]) -> tuple[MagicMock, MagicMock]:
    """Return (fake_model, bound) with ainvoke configured."""
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(side_effect=list(responses))
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke
    return fake_model, bound


# ---------------------------------------------------------------------------
# _infer_operation — uncovered keyword families
# ---------------------------------------------------------------------------


class TestInferOperationExtended:
    """Cover keyword families not exercised by existing tests."""

    @pytest.mark.parametrize(
        "tool_id,expected",
        [
            ("fetch_data", "get"),
            ("query_db", "get"),
            ("lookup_user", "get"),
            ("web_url_read", "get"),
            ("create_file", "set"),
            ("delete_record", "set"),
            ("update_config", "set"),
            ("patch_resource", "set"),
            ("insert_row", "set"),
            ("append_log", "set"),
            ("replace_content", "set"),
            ("upload_file", "set"),
            ("post_message", "set"),
            ("put_object", "set"),
            ("remove_entry", "set"),
            ("apply_patch", "set"),
        ],
    )
    def test_keyword_mapping(self, tool_id: str, expected: str):
        assert _infer_operation(tool_id) == expected


# ---------------------------------------------------------------------------
# _coerce_mcp_tool_input — uncovered edge cases
# ---------------------------------------------------------------------------


class TestCoerceMcpToolInputEdgeCases:
    """Cover the branches not exercised by test_tool_use_loop.py."""

    def _mcp_spec(self, required: list[str], properties: dict) -> ToolSpec:
        return ToolSpec(
            tool_id="mcp_tool",
            name="mcp_tool",
            description="mcp",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={"schema": {"required": required, "properties": properties}},
        )

    def test_json_string_parsed_to_dict(self):
        """A JSON-encoded string input is parsed to dict and re-checked for required."""
        spec = self._mcp_spec(["query"], {"query": {"type": "string"}})
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input='{"query": "hello"}')
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None
        assert step.tool_input == {"query": "hello"}

    def test_invalid_json_string_falls_to_field_mapping(self):
        """Malformed JSON string with single expected_field gets mapped."""
        spec = self._mcp_spec(["query"], {"query": {"type": "string"}})
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input="{not valid json}")
        result = _coerce_mcp_tool_input(step, spec)
        # Single required field → mapped
        assert result is None
        assert step.tool_input == {"query": "{not valid json}"}

    def test_string_no_preferred_field_among_multiple(self):
        """String input with multiple fields none matching preferred → error."""
        spec = self._mcp_spec(
            ["alpha", "beta"],
            {"alpha": {"type": "string"}, "beta": {"type": "string"}},
        )
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input="raw")
        result = _coerce_mcp_tool_input(step, spec)
        assert result is not None
        assert "alpha" in result or "beta" in result or "Expected JSON" in result

    def test_preferred_field_among_multiple(self):
        """String input with 'query' among multiple fields maps to query."""
        spec = self._mcp_spec(
            ["query", "limit"],
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
        )
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input="find me")
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None
        assert step.tool_input == {"query": "find me"}

    def test_dict_missing_required_field(self):
        """Dict missing required fields → error."""
        spec = self._mcp_spec(
            ["query", "limit"],
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
        )
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input={"other": "val"})
        result = _coerce_mcp_tool_input(step, spec)
        assert result is not None
        assert "Missing required fields" in result

    def test_single_required_array_field_coerces_string_to_list(self):
        """Single required field of type array: string value → [string]."""
        spec = self._mcp_spec(
            ["items"],
            {"items": {"type": "array", "items": {"type": "string"}}},
        )
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input={"wrong_key": "foo"})
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None
        assert step.tool_input == {"items": ["foo"]}

    def test_single_required_string_field_coerces_list_to_string(self):
        """Single required field of type string: list [x] → x."""
        spec = self._mcp_spec(["msg"], {"msg": {"type": "string"}})
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input={"wrong_key": ["hello"]})
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None
        assert step.tool_input == {"msg": "hello"}

    def test_dict_with_all_required_fields_passes(self):
        """Dict with all required fields → None (no error)."""
        spec = self._mcp_spec(
            ["query", "limit"],
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
        )
        step = ActionStep(
            tool_id="mcp_tool",
            operation="get",
            tool_input={"query": "hello", "limit": "5"},  # passes as-is
        )
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None

    def test_no_schema_in_metadata(self):
        """MCP spec with no schema → None (pass-through)."""
        spec = ToolSpec(
            tool_id="mcp_tool",
            name="mcp_tool",
            description="mcp",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={},
        )
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input="anything")
        assert _coerce_mcp_tool_input(step, spec) is None

    def test_string_no_expected_fields(self):
        """String input with empty required + no properties → error."""
        spec = ToolSpec(
            tool_id="mcp_tool",
            name="mcp_tool",
            description="mcp",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={"schema": {"required": [], "properties": {}}},
        )
        step = ActionStep(tool_id="mcp_tool", operation="get", tool_input="raw")
        result = _coerce_mcp_tool_input(step, spec)
        # No expected fields → error message
        assert result is not None
        assert "Expected JSON" in result


# ---------------------------------------------------------------------------
# _CachedFileRead dataclass
# ---------------------------------------------------------------------------


class TestCachedFileRead:
    """Verify _CachedFileRead field storage (covers the dataclass lines)."""

    def test_fields_stored(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("hello")
        mtime = os.path.getmtime(str(f))
        cached = _CachedFileRead(path=str(f), offset=10, limit=100, mtime=mtime)
        assert cached.path == str(f)
        assert cached.offset == 10
        assert cached.limit == 100
        assert cached.mtime == mtime

    def test_no_limit(self, tmp_path: Path):
        f = tmp_path / "f.py"
        f.write_text("x")
        mtime = os.path.getmtime(str(f))
        cached = _CachedFileRead(path=str(f), offset=0, limit=None, mtime=mtime)
        assert cached.limit is None


# ---------------------------------------------------------------------------
# File-read cache: _check_file_read_cache and _populate_file_read_cache
# ---------------------------------------------------------------------------


class TestFileReadCache:
    """Exercise the file read dedup cache methods directly."""

    def _loop_with_file(self, tmp_path: Path) -> ToolUseLoop:
        return _make_loop(cwd=str(tmp_path))

    def test_cache_miss_returns_none(self, tmp_path: Path):
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(tmp_path / "nofile.py")},
        )
        assert loop._check_file_read_cache(step) is None

    def test_cache_hit_same_params_returns_stub(self, tmp_path: Path):
        f = tmp_path / "foo.py"
        f.write_text("content")
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(f), "offset": 0},
        )
        loop._populate_file_read_cache(step)
        result = loop._check_file_read_cache(step)
        assert result is not None
        assert "unchanged" in result.lower()

    def test_cache_miss_different_offset(self, tmp_path: Path):
        f = tmp_path / "bar.py"
        f.write_text("content")
        loop = self._loop_with_file(tmp_path)
        step0 = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(f), "offset": 0},
        )
        loop._populate_file_read_cache(step0)
        step1 = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(f), "offset": 10},
        )
        assert loop._check_file_read_cache(step1) is None

    def test_cache_invalidated_on_mtime_change(self, tmp_path: Path):
        f = tmp_path / "baz.py"
        f.write_text("v1")
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(f), "offset": 0},
        )
        loop._populate_file_read_cache(step)
        # Mutate file (ensure mtime changes)
        time.sleep(0.01)
        f.write_text("v2")
        os.utime(str(f), (time.time() + 1, time.time() + 1))
        result = loop._check_file_read_cache(step)
        assert result is None

    def test_cache_no_path_returns_none(self, tmp_path: Path):
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(tool_id="read_file", operation="get", tool_input={})
        assert loop._check_file_read_cache(step) is None

    def test_populate_skips_nonexistent_file(self, tmp_path: Path):
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(tmp_path / "missing.py")},
        )
        loop._populate_file_read_cache(step)  # should not raise
        assert loop._file_read_cache == {}

    def test_limit_coercion_to_int(self, tmp_path: Path):
        """limit as string is coerced to int during populate and check."""
        f = tmp_path / "lim.py"
        f.write_text("abc")
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(f), "offset": 0, "limit": "50"},
        )
        loop._populate_file_read_cache(step)
        result = loop._check_file_read_cache(step)
        assert result is not None

    def test_cache_evicted_when_file_deleted(self, tmp_path: Path):
        """If file is deleted after caching, check returns None and evicts entry."""
        f = tmp_path / "del.py"
        f.write_text("data")
        loop = self._loop_with_file(tmp_path)
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(f), "offset": 0},
        )
        loop._populate_file_read_cache(step)
        f.unlink()
        result = loop._check_file_read_cache(step)
        assert result is None


# ---------------------------------------------------------------------------
# _append_lsp_feedback — module-level function
# ---------------------------------------------------------------------------


class TestAppendLspFeedback:
    """Cover _append_lsp_feedback with LSP available and unavailable."""

    def test_lsp_not_installed_returns_original(self):
        """When mewbo_tools is absent, content is unchanged."""
        with patch.dict(
            "sys.modules",
            {
                "mewbo_tools": None,
                "mewbo_tools.integration": None,
                "mewbo_tools.integration.lsp": None,
            },
        ):
            result = _append_lsp_feedback("original content", "/some/file.py", "/cwd")
        assert result == "original content"

    def test_lsp_feedback_appended(self):
        """When get_passive_diagnostics returns text, it's appended."""
        mock_lsp = MagicMock()
        mock_lsp.get_passive_diagnostics.return_value = "line 1: error E001"
        with patch.dict(
            "sys.modules",
            {"mewbo_tools.integration.lsp": mock_lsp},
        ):
            result = _append_lsp_feedback("edit result", "/f.py", "/cwd")
        assert "edit result" in result
        assert "E001" in result
        assert "Passive Feedback" in result

    def test_lsp_feedback_empty_no_append(self):
        """When get_passive_diagnostics returns falsy, content is unchanged."""
        mock_lsp = MagicMock()
        mock_lsp.get_passive_diagnostics.return_value = ""
        with patch.dict("sys.modules", {"mewbo_tools.integration.lsp": mock_lsp}):
            result = _append_lsp_feedback("original", "/f.py", "/cwd")
        assert result == "original"

    def test_lsp_exception_suppressed(self):
        """Exception inside the LSP call is swallowed; original content returned."""
        mock_lsp = MagicMock()
        mock_lsp.get_passive_diagnostics.side_effect = RuntimeError("LSP boom")
        with patch.dict("sys.modules", {"mewbo_tools.integration.lsp": mock_lsp}):
            result = _append_lsp_feedback("safe", "/f.py", "/cwd")
        assert result == "safe"


# ---------------------------------------------------------------------------
# _partition_tool_calls — concurrency_safe vs exclusive batching
# ---------------------------------------------------------------------------


class TestPartitionToolCalls:
    """Verify _partition_tool_calls produces correct ToolBatch groupings."""

    def _make_loop(self) -> ToolUseLoop:
        return _make_loop()

    def _tc(self, name: str, call_id: str = "c1") -> dict:
        return {"name": name, "args": {}, "id": call_id}

    def test_all_concurrent_single_batch(self):
        safe = _make_spec("safe_tool", concurrency_safe=True)
        loop = self._make_loop()
        specs_map = {safe.tool_id: safe}
        calls = [self._tc("safe_tool", "c1"), self._tc("safe_tool", "c2")]
        batches = loop._partition_tool_calls(calls, specs_map)
        assert len(batches) == 1
        assert batches[0].concurrent is True
        assert len(batches[0].calls) == 2

    def test_exclusive_tool_isolated(self):
        excl = _make_spec("excl_tool", concurrency_safe=False)
        loop = self._make_loop()
        specs_map = {excl.tool_id: excl}
        calls = [self._tc("excl_tool")]
        batches = loop._partition_tool_calls(calls, specs_map)
        assert len(batches) == 1
        assert batches[0].concurrent is False

    def test_mixed_concurrent_exclusive_concurrent(self):
        safe = _make_spec("safe", concurrency_safe=True)
        excl = _make_spec("excl", concurrency_safe=False)
        loop = self._make_loop()
        specs_map = {safe.tool_id: safe, excl.tool_id: excl}
        calls = [
            self._tc("safe", "c1"),
            self._tc("safe", "c2"),
            self._tc("excl", "c3"),
            self._tc("safe", "c4"),
        ]
        batches = loop._partition_tool_calls(calls, specs_map)
        # [safe,safe] → [excl] → [safe]
        assert len(batches) == 3
        assert batches[0].concurrent is True and len(batches[0].calls) == 2
        assert batches[1].concurrent is False
        assert batches[2].concurrent is True and len(batches[2].calls) == 1

    def test_unknown_tool_defaults_to_concurrent(self):
        """Unknown tool_id (not in specs_map) is treated as concurrency_safe."""
        loop = self._make_loop()
        calls = [self._tc("unknown_tool")]
        batches = loop._partition_tool_calls(calls, {})
        assert len(batches) == 1
        assert batches[0].concurrent is True

    def test_empty_calls_returns_empty(self):
        loop = self._make_loop()
        assert loop._partition_tool_calls([], {}) == []

    def test_exclusive_between_two_concurrent_groups(self):
        safe = _make_spec("safe", concurrency_safe=True)
        excl = _make_spec("excl", concurrency_safe=False)
        loop = self._make_loop()
        specs_map = {safe.tool_id: safe, excl.tool_id: excl}
        calls = [
            self._tc("safe", "c1"),
            self._tc("excl", "c2"),
            self._tc("safe", "c3"),
            self._tc("safe", "c4"),
        ]
        batches = loop._partition_tool_calls(calls, specs_map)
        assert len(batches) == 3
        assert batches[0].concurrent and len(batches[0].calls) == 1
        assert not batches[1].concurrent
        assert batches[2].concurrent and len(batches[2].calls) == 2


# ---------------------------------------------------------------------------
# _safe_execute — timeout and generic exception
# ---------------------------------------------------------------------------


class TestSafeExecute:
    """Cover timeout and generic exception branches in _safe_execute."""

    def test_timeout_returns_error_result(self):
        """When the tool hangs, _safe_execute returns a failure result (no raise)."""
        spec = _make_spec("slow_tool", timeout=0.01)
        registry = _make_registry(spec)

        async def _slow(_step):
            await asyncio.sleep(10)

        mock_tool = MagicMock()
        mock_tool.arun = _slow

        async def run():
            with patch.object(registry, "get", return_value=mock_tool):
                loop = _make_loop(registry)
                tc = {"name": "slow_tool", "args": {}, "id": "tc1"}
                return await loop._safe_execute(tc, [spec])

        result = asyncio.run(run())
        assert result.success is False
        assert "timed out" in result.content.lower()
        assert result.tool_id == "slow_tool"

    def test_generic_exception_returns_error_result(self):
        """An unexpected exception inside the tool returns a failure ToolCallResult."""
        spec = _make_spec("boom_tool")
        registry = _make_registry(spec)

        # Use spec=['run'] so hasattr(tool, 'arun') returns False,
        # forcing the sync path (asyncio.to_thread). Then raise there.
        mock_tool = MagicMock(spec=["run"])
        mock_tool.run.side_effect = ValueError("boom")

        async def run():
            with patch.object(registry, "get", return_value=mock_tool):
                loop = _make_loop(registry)
                tc = {"name": "boom_tool", "args": {}, "id": "tc2"}
                return await loop._safe_execute(tc, [spec])

        result = asyncio.run(run())
        assert result.success is False
        assert "boom" in result.content

    def test_timeout_sibling_is_not_cancelled(self):
        """Two concurrent tools: one times out, the other completes normally."""
        fast = _make_spec("fast_tool", timeout=5.0)
        slow = _make_spec("slow_tool", timeout=0.05)
        registry = _make_registry(fast, slow)

        fast_result = MagicMock()
        fast_result.content = "fast result"

        async def _slow_arun(_step):
            await asyncio.sleep(10)

        async def run():
            def _get_tool(tool_id):
                if tool_id == "fast_tool":
                    # Sync-only tool (no arun attr) so asyncio.to_thread is used
                    t = MagicMock(spec=["run"])
                    t.run.return_value = fast_result
                    return t
                else:
                    # Async tool that hangs
                    t = MagicMock(spec=["arun"])
                    t.arun = _slow_arun
                    return t

            with patch.object(registry, "get", side_effect=_get_tool):
                loop = _make_loop(registry)
                tc_fast = {"name": "fast_tool", "args": {}, "id": "f1"}
                tc_slow = {"name": "slow_tool", "args": {}, "id": "s1"}
                results = await asyncio.gather(
                    loop._safe_execute(tc_fast, [fast, slow]),
                    loop._safe_execute(tc_slow, [fast, slow]),
                )
            return results

        results = asyncio.run(run())
        fast_r, slow_r = results
        assert fast_r.success is True
        assert slow_r.success is False
        assert "timed out" in slow_r.content


# ---------------------------------------------------------------------------
# Doom-loop halt: repeated identical tool calls halt with halted_no_progress
# ---------------------------------------------------------------------------


class TestDoomLoopHalt:
    """Verify the doom-loop guard halts after threshold identical calls."""

    def test_doom_loop_halts_with_correct_done_reason(self):
        """Repeating the same tool+args exactly doom_threshold times → halted_no_progress.

        We inject a DoomLoopGuard with threshold=3 via patch so only 3 identical
        tool calls are needed, keeping the test fast and deterministic.
        """
        spec = _make_spec("aider_shell_tool", "shell")
        registry = _make_registry(spec)

        doom_threshold = 3
        # Need threshold+1 calls to trigger is_stuck (observe is called after invoke)
        responses = [
            _tool_call_response("aider_shell_tool", {"command": "ls"}, f"c{i}")
            for i in range(doom_threshold + 2)
        ]
        fake_model, bound = _build_bound(responses)

        mock_tool = MagicMock(spec=["run"])
        mock_speaker = MagicMock()
        mock_speaker.content = "file.txt"
        mock_tool.run.return_value = mock_speaker

        events: list[dict] = []

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
            patch(
                "mewbo_core.tool_use_loop.DoomLoopGuard.from_config",
                return_value=DoomLoopGuard(threshold=doom_threshold),
            ),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = _make_loop(registry, event_logger=events.append)
            tq, state = asyncio.run(loop.run("do work", tool_specs=[spec], context=_make_context()))

        assert state.done_reason == "halted_no_progress"
        halt_events = [e for e in events if e.get("type") == "recovery"]
        assert any(e["payload"].get("action") == "halt_no_progress" for e in halt_events)

    def test_doom_loop_disabled_when_threshold_zero(self):
        """DoomLoopGuard with threshold=0 never trips."""
        guard = DoomLoopGuard(threshold=0)
        calls = [{"name": "t", "args": {}, "id": "x"}]
        for _ in range(10):
            guard.observe(calls)
        assert guard.is_stuck() is False

    def test_doom_loop_empty_signature_not_stuck(self):
        """Empty tool calls produce empty signature; is_stuck returns False."""
        guard = DoomLoopGuard(threshold=3)
        for _ in range(5):
            guard.observe([])
        assert guard.is_stuck() is False

    def test_doom_loop_different_args_not_stuck(self):
        guard = DoomLoopGuard(threshold=3)
        for i in range(5):
            guard.observe([{"name": "tool", "args": {"n": i}, "id": "x"}])
        assert guard.is_stuck() is False


# ---------------------------------------------------------------------------
# repair_tool_pairing — orphan drops and stub insertion
# ---------------------------------------------------------------------------


class TestRepairToolPairing:
    """Exercise repair_tool_pairing branches."""

    def test_no_repair_needed(self):
        ai = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "id1"}])
        tm = ToolMessage(content="result", tool_call_id="id1")
        msgs = [ai, tm]
        n = repair_tool_pairing(msgs)
        assert n == 0
        assert len(msgs) == 2

    def test_orphan_tool_result_dropped(self):
        """ToolMessage with no matching AIMessage tool_call id is dropped."""
        tm_orphan = ToolMessage(content="orphan", tool_call_id="ghost_id")
        ai = AIMessage(content="text")
        msgs = [ai, tm_orphan]
        n = repair_tool_pairing(msgs)
        assert n == 1
        assert all(not isinstance(m, ToolMessage) for m in msgs)

    def test_unanswered_tool_call_gets_stub(self):
        """AIMessage tool_call with no matching ToolMessage gets an interrupted stub."""
        ai = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "dangling"}])
        msgs = [ai]
        n = repair_tool_pairing(msgs)
        assert n == 1
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "dangling"

    def test_multiple_unanswered_get_stubs(self):
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "a", "args": {}, "id": "id_a"},
                {"name": "b", "args": {}, "id": "id_b"},
            ],
        )
        msgs = [ai]
        repair_tool_pairing(msgs)
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        ids = {m.tool_call_id for m in tool_msgs}
        assert "id_a" in ids and "id_b" in ids

    def test_mixed_answered_and_unanswered(self):
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "a", "args": {}, "id": "ok"},
                {"name": "b", "args": {}, "id": "missing"},
            ],
        )
        answered = ToolMessage(content="ok result", tool_call_id="ok")
        msgs = [ai, answered]
        n = repair_tool_pairing(msgs)
        assert n == 1
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        ids = {m.tool_call_id for m in tool_msgs}
        assert "ok" in ids and "missing" in ids


# ---------------------------------------------------------------------------
# User message-queue draining between loop steps
# ---------------------------------------------------------------------------


class TestMessageQueueDraining:
    """Verify the loop drains message_queue entries between steps."""

    def test_queued_message_appears_as_human_message(self):
        """A message put in message_queue before the run is injected mid-loop."""
        spec = _make_spec("aider_shell_tool")
        registry = _make_registry(spec)

        messages_seen: list = []

        def _capture(msgs, **_kw):
            messages_seen.extend(msgs)
            # Return text after we've seen at least one tool call result
            if any(isinstance(m, ToolMessage) for m in msgs):
                return _text_response("done after queue message")
            return _tool_call_response("aider_shell_tool", {"command": "ls"}, "c1")

        fake_model, bound = _build_bound([])
        fake_model.ainvoke = AsyncMock(side_effect=_capture)
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "result"
        mock_tool.run.return_value = mock_speaker

        ctx = _make_agent_context()
        # Put a message in queue before run
        assert ctx.message_queue is not None
        ctx.message_queue.put_nowait("user steering message")

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(loop.run("do work", tool_specs=[spec], context=_make_context()))

        assert state.done_reason == "completed"
        human_msgs = [m for m in messages_seen if isinstance(m, HumanMessage)]
        contents = [m.content for m in human_msgs]
        assert any("user steering message" in str(c) for c in contents)


# ---------------------------------------------------------------------------
# Interrupt-step draining between loop steps
# ---------------------------------------------------------------------------


class TestInterruptStep:
    """Verify the interrupt_step event is consumed and injected into messages."""

    def test_interrupt_injects_human_message(self):
        """Setting interrupt_step before the loop causes a HumanMessage injection."""
        spec = _make_spec("aider_shell_tool")
        registry = _make_registry(spec)

        messages_seen: list = []

        def _capture(msgs, **_kw):
            messages_seen.extend(msgs)
            if any(
                isinstance(m, HumanMessage) and "interrupted" in str(m.content).lower()
                for m in msgs
            ):
                return _text_response("got interrupt")
            return _tool_call_response("aider_shell_tool", {"command": "ls"}, "c1")

        fake_model, bound = _build_bound([])
        fake_model.ainvoke = AsyncMock(side_effect=_capture)
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "result"
        mock_tool.run.return_value = mock_speaker

        ctx = _make_agent_context()
        assert ctx.interrupt_step is not None
        ctx.interrupt_step.set()  # signal interrupt before run

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(loop.run("task", tool_specs=[spec], context=_make_context()))

        assert state.done_reason == "completed"
        # interrupt_step should have been cleared
        assert not ctx.interrupt_step.is_set()
        human_msgs = [m for m in messages_seen if isinstance(m, HumanMessage)]
        assert any("interrupted" in str(m.content).lower() for m in human_msgs)


# ---------------------------------------------------------------------------
# Permission: ASK decision → approval callback
# ---------------------------------------------------------------------------


class TestPermissionAskCallback:
    """Cover the approval callback branch (ASK decision)."""

    def test_ask_approved_allows_execution(self):
        spec = _make_spec("aider_shell_tool")
        registry = _make_registry(spec)

        ask_policy = MagicMock(spec=PermissionPolicy)
        ask_policy.decide.return_value = PermissionDecision.ASK

        approval_callback = MagicMock(return_value=True)

        fake_model, bound = _build_bound(
            [
                _tool_call_response("aider_shell_tool", {"command": "ls"}, "c1"),
                _text_response("done"),
            ]
        )
        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "output"
        mock_tool.run.return_value = mock_speaker

        events: list[dict] = []

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(event_logger=events.append),
                tool_registry=registry,
                permission_policy=ask_policy,
                hook_manager=_make_hook_manager(),
                approval_callback=approval_callback,
            )
            tq, state = asyncio.run(loop.run("run ls", tool_specs=[spec], context=_make_context()))

        assert state.done_reason == "completed"
        approval_callback.assert_called_once()
        perm_events = [e for e in events if e.get("type") == "permission"]
        assert any(e["payload"]["decision"] == "allow" for e in perm_events)

    def test_ask_denied_by_callback_produces_denial(self):
        spec = _make_spec("aider_shell_tool")
        registry = _make_registry(spec)

        ask_policy = MagicMock(spec=PermissionPolicy)
        ask_policy.decide.return_value = PermissionDecision.ASK
        approval_callback = MagicMock(return_value=False)

        fake_model, bound = _build_bound(
            [
                _tool_call_response("aider_shell_tool", {"command": "rm -rf /"}, "c1"),
                _text_response("Denied."),
            ]
        )
        events: list[dict] = []

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(event_logger=events.append),
                tool_registry=registry,
                permission_policy=ask_policy,
                hook_manager=_make_hook_manager(),
                approval_callback=approval_callback,
            )
            tq, state = asyncio.run(loop.run("rm", tool_specs=[spec], context=_make_context()))

        assert state.done_reason == "completed"
        perm_events = [e for e in events if e.get("type") == "permission"]
        assert any(e["payload"]["decision"] == "deny" for e in perm_events)


# ---------------------------------------------------------------------------
# Failure feedback SystemMessage injection after failed tool call
# ---------------------------------------------------------------------------


class TestFailureFeedbackInjection:
    """Verify that a failed tool call injects a failure SystemMessage."""

    def test_failed_tool_injects_failure_feedback(self):
        spec = _make_spec("aider_shell_tool")
        registry = _make_registry(spec)

        messages_seen: list = []

        def _capture(msgs, **_kw):
            messages_seen.extend(msgs)
            if any(isinstance(m, SystemMessage) and "failed" in m.content.lower() for m in msgs):
                return _text_response("got failure feedback")
            return _tool_call_response("aider_shell_tool", {"command": "bad"}, "c1")

        fake_model, bound = _build_bound([])
        fake_model.ainvoke = AsyncMock(side_effect=_capture)
        bound.ainvoke = fake_model.ainvoke

        deny_policy = MagicMock(spec=PermissionPolicy)
        deny_policy.decide.return_value = PermissionDecision.DENY

        with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=deny_policy,
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("trigger failure", tool_specs=[spec], context=_make_context())
            )

        sys_msgs = [m for m in messages_seen if isinstance(m, SystemMessage)]
        assert any("failed" in m.content.lower() for m in sys_msgs)


# ---------------------------------------------------------------------------
# _extract_text_content — list content and no-content placeholder
# ---------------------------------------------------------------------------


class TestExtractTextContent:
    """Test static method _extract_text_content with various content types."""

    def test_list_with_text_block(self):
        content = [
            {"type": "text", "text": "hello world"},
            {"type": "image", "data": "..."},
        ]
        result = ToolUseLoop._extract_text_content(content)
        assert result == "hello world"

    def test_list_multiple_text_blocks_joined(self):
        content = [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ]
        result = ToolUseLoop._extract_text_content(content)
        assert "part one" in result
        assert "part two" in result

    def test_no_content_placeholder_filtered(self):
        result = ToolUseLoop._extract_text_content("(no content)")
        assert result == ""

    def test_plain_string_returned(self):
        assert ToolUseLoop._extract_text_content("hello") == "hello"

    def test_empty_string_returns_empty(self):
        assert ToolUseLoop._extract_text_content("") == ""

    def test_none_returns_empty(self):
        assert ToolUseLoop._extract_text_content(None) == ""

    def test_list_no_text_blocks_returns_empty(self):
        content = [{"type": "image", "data": "..."}]
        result = ToolUseLoop._extract_text_content(content)
        assert result == ""


# ---------------------------------------------------------------------------
# _should_compact_messages — token threshold logic
# ---------------------------------------------------------------------------


class TestShouldCompactMessages:
    """Cover _should_compact_messages with zero and non-zero token counts."""

    def test_returns_false_when_no_tokens_yet(self):
        loop = _make_loop()
        loop._last_input_tokens = 0
        assert loop._should_compact_messages([]) is False

    def test_returns_true_when_over_threshold(self):
        loop = _make_loop(agent_context=_make_agent_context(model_name="gpt-4"))
        # Set a high token count that exceeds any reasonable threshold
        loop._last_input_tokens = 999_999_999
        assert loop._should_compact_messages([]) is True

    def test_returns_false_when_under_threshold(self):
        loop = _make_loop(agent_context=_make_agent_context(model_name="gpt-4"))
        loop._last_input_tokens = 1  # Very low usage
        assert loop._should_compact_messages([]) is False


# ---------------------------------------------------------------------------
# _plan_mode_permission — key branches
# ---------------------------------------------------------------------------


class TestPlanModePermission:
    """Cover the plan-mode permission branches."""

    def _make_plan_loop(self, session_id: str = "sess-001", depth: int = 0) -> ToolUseLoop:
        ctx = AgentContext.root(
            model_name="test-model",
            max_depth=5,
            registry=AgentHypervisor(max_concurrent=100),
        )
        registry = _make_registry(
            _make_spec("read_file", read_only=True),
            _make_spec("aider_shell_tool", read_only=False),
            _make_spec("mcp_search", kind="mcp", metadata={"server": "search", "schema": {}}),
        )
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id=session_id,
        )
        loop._current_mode = "plan"
        return loop

    def test_exit_plan_mode_always_allowed(self):
        loop = self._make_plan_loop()
        step = ActionStep(tool_id="exit_plan_mode", operation="set", tool_input={})
        assert loop._plan_mode_permission(step) is True

    def test_read_only_tool_allowed(self):
        loop = self._make_plan_loop()
        step = ActionStep(tool_id="read_file", operation="get", tool_input={})
        assert loop._plan_mode_permission(step) is True

    def test_unregistered_tool_denied(self):
        loop = self._make_plan_loop()
        step = ActionStep(tool_id="random_write_tool", operation="set", tool_input={})
        assert loop._plan_mode_permission(step) is False

    def test_mcp_tool_allowed_when_flag_true(self):
        """MCP tools pass when plan_mode_allow_mcp is True."""
        loop = self._make_plan_loop()
        with patch(
            "mewbo_core.tool_use_loop.get_config_value",
            side_effect=lambda *args, **kw: (
                True if "plan_mode_allow_mcp" in args else kw.get("default")
            ),
        ):
            step = ActionStep(tool_id="mcp_search", operation="get", tool_input={"query": "x"})
            assert loop._plan_mode_permission(step) is True

    def test_shell_blocked_with_unsafe_command(self):
        """Shell tool is denied when command contains metacharacters."""
        registry = _make_registry(
            _make_spec("aider_shell_tool", read_only=False),
        )
        ctx = _make_agent_context()
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="sess",
        )
        loop._current_mode = "plan"

        with patch(
            "mewbo_core.tool_use_loop.get_config_value",
            side_effect=lambda *args, **kw: (
                ["ls", "cat"] if "plan_mode_shell_allowlist" in args else kw.get("default")
            ),
        ):
            step = ActionStep(
                tool_id="aider_shell_tool",
                operation="set",
                tool_input={"command": "ls && rm -rf /"},
            )
            result = loop._plan_mode_permission(step)

        assert result is False

    def test_agent_mgmt_allowed_for_root(self):
        """spawn_agent/check_agents/steer_agent are allowed at depth=0."""
        loop = self._make_plan_loop()
        for tool_id in ("spawn_agent", "check_agents", "steer_agent"):
            step = ActionStep(tool_id=tool_id, operation="set", tool_input={})
            assert loop._plan_mode_permission(step) is True, f"Expected True for {tool_id}"


# ---------------------------------------------------------------------------
# Tool arun path (async tool execution)
# ---------------------------------------------------------------------------


class TestAsyncToolExecution:
    """Verify that tools with arun are called asynchronously."""

    def test_arun_path_called_for_async_tool(self):
        spec = _make_spec("async_tool")
        registry = _make_registry(spec)

        async_result = MagicMock()
        async_result.content = "async output"

        async_tool = MagicMock()
        async_tool.arun = AsyncMock(return_value=async_result)

        fake_model, bound = _build_bound(
            [
                _tool_call_response("async_tool", {"input": "x"}, "tc1"),
                _text_response("done"),
            ]
        )

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=async_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = _make_loop(registry)
            tq, state = asyncio.run(
                loop.run("use async tool", tool_specs=[spec], context=_make_context())
            )

        async_tool.arun.assert_called_once()
        assert state.done_reason == "completed"
        assert len(tq.action_steps) == 1


# ---------------------------------------------------------------------------
# Concurrent tool execution via asyncio.gather (partition integration)
# ---------------------------------------------------------------------------


class TestConcurrentToolExecution:
    """Drive the concurrent branch of the main loop via two concurrent-safe tools."""

    def test_two_concurrent_tools_gather(self):
        safe_a = _make_spec("tool_a", concurrency_safe=True)
        safe_b = _make_spec("tool_b", concurrency_safe=True)
        registry = _make_registry(safe_a, safe_b)

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "result"
        mock_tool.run.return_value = mock_speaker

        # LLM returns two concurrent tool calls in one turn
        two_calls = AIMessage(
            content="",
            tool_calls=[
                {"name": "tool_a", "args": {"input": "a"}, "id": "id_a"},
                {"name": "tool_b", "args": {"input": "b"}, "id": "id_b"},
            ],
        )
        fake_model, bound = _build_bound([two_calls, _text_response("done")])

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = _make_loop(registry)
            tq, state = asyncio.run(
                loop.run("two tools", tool_specs=[safe_a, safe_b], context=_make_context())
            )

        assert state.done_reason == "completed"
        assert len(tq.action_steps) == 2


# ---------------------------------------------------------------------------
# MCP coercion error propagated as ToolCallResult failure
# ---------------------------------------------------------------------------


class TestMcpCoercionErrorInLoop:
    """Verify that a coercion error surfaces as a failed ToolCallResult."""

    def test_mcp_coercion_error_returns_failure(self):
        mcp_spec = ToolSpec(
            tool_id="mcp_search",
            name="mcp_search",
            description="search",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={
                "server": "search",
                "schema": {
                    "required": ["query", "limit"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            },
        )
        registry = _make_registry(mcp_spec)
        events: list[dict] = []

        fake_model, bound = _build_bound(
            [
                # LLM calls with missing 'limit' and mismatched keys
                AIMessage(
                    content="",
                    tool_calls=[{"name": "mcp_search", "args": {"wrong": "val"}, "id": "c1"}],
                ),
                _text_response("coercion failed"),
            ]
        )

        with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(event_logger=events.append),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("search", tool_specs=[mcp_spec], context=_make_context())
            )

        assert state.done_reason == "completed"
        tool_result_events = [e for e in events if e.get("type") == "tool_result"]
        assert any(not e["payload"].get("success", True) for e in tool_result_events)


# ---------------------------------------------------------------------------
# _get_tool_timeout — spec timeout vs fallback
# ---------------------------------------------------------------------------


class TestGetToolTimeout:
    """Verify timeout resolution from spec or fallback."""

    def test_spec_timeout_used(self):
        spec = _make_spec("slow", timeout=300.0)
        registry = _make_registry(spec)
        loop = _make_loop(registry)
        assert loop._get_tool_timeout("slow") == 300.0

    def test_missing_spec_returns_default(self):
        loop = _make_loop()
        assert loop._get_tool_timeout("nonexistent") == 120.0


# ---------------------------------------------------------------------------
# Cancellation mid-loop
# ---------------------------------------------------------------------------


class TestCancellationMidLoop:
    """Verify should_cancel causes state.done_reason == 'canceled'."""

    def test_cancel_before_first_tool_call(self):
        spec = _make_spec("aider_shell_tool")
        registry = _make_registry(spec)

        # Cancel immediately
        cancel_flag = threading.Event()
        cancel_flag.set()

        ctx = AgentContext.root(
            model_name="test-model",
            max_depth=5,
            should_cancel=lambda: cancel_flag.is_set(),
            registry=AgentHypervisor(max_concurrent=100),
        )

        fake_model, bound = _build_bound([_text_response("never reached")])

        with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(loop.run("do work", tool_specs=[spec], context=_make_context()))

        assert state.done_reason == "canceled"
