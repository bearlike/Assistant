#!/usr/bin/env python3
"""Tests for plan-mode approval gating.

Covers:
- Path helpers (``plan_dir_for``, ``plan_file_for``, ``ensure_plan_dir``,
  ``is_inside_plan_dir``).
- Schema filtering in ``ToolUseLoop._build_tool_schemas_for_mode``.
- Plan-mode permission branch (``_plan_mode_permission``).
- ``ExitPlanModeTool`` handler — file-missing error, approve, reject,
  revision counter.
- End-to-end mode transition: plan → exit_plan_mode → approval → act mode
  tool rebinding.
- Orchestrator mode resolution (keyword heuristics removed).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from meeseeks_core.agent_context import AgentContext
from meeseeks_core.classes import ActionStep
from meeseeks_core.context import ContextSnapshot
from meeseeks_core.exit_plan_mode import (
    PLAN_DIR_ROOT,
    ExitPlanModeTool,
    ensure_plan_dir,
    is_inside_plan_dir,
    is_shell_command_plan_safe,
    plan_dir_for,
    plan_file_for,
)
from meeseeks_core.hooks import HookManager
from meeseeks_core.hypervisor import AgentHypervisor
from meeseeks_core.orchestrator import Orchestrator
from meeseeks_core.permissions import PermissionDecision, PermissionPolicy
from meeseeks_core.token_budget import TokenBudget
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec
from meeseeks_core.tool_use_loop import ToolUseLoop

# ---------------------------------------------------------------------------
# Helpers (mirroring test_tool_use_loop.py)
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
    tool_id: str,
    *,
    read_only: bool = False,
) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Spec for {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        read_only=read_only,
        metadata={
            "schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
            }
        },
    )


def _make_edit_spec() -> ToolSpec:
    return ToolSpec(
        tool_id="aider_edit_block_tool",
        name="aider_edit_block_tool",
        description="Edit files via SEARCH/REPLACE blocks.",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        concurrency_safe=False,
        metadata={
            "schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
            },
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


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _make_agent_context(
    *,
    event_logger=None,
) -> AgentContext:
    return AgentContext.root(
        model_name="test-model",
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=10),
        event_logger=event_logger,
    )


def _text_response(content: str) -> AIMessage:
    return AIMessage(content=content)


def _tool_call_response(tool_id: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_id, "args": args, "id": call_id}],
    )


def _patch_plan_config(
    *,
    shell_allowlist: list[str] | None = None,
    allow_mcp: bool = True,
    edit_tool: str = "search_replace_block",
):
    """Context manager that patches ``get_config_value`` in ``tool_use_loop``.

    Delegates to the real config loader for any key we don't explicitly
    override, so tests do not accidentally break unrelated config reads.
    """
    from meeseeks_core import tool_use_loop as _tul

    real_get = _tul.get_config_value
    overrides = {
        ("agent", "plan_mode_shell_allowlist"): shell_allowlist or [],
        ("agent", "plan_mode_allow_mcp"): allow_mcp,
        ("agent", "edit_tool"): edit_tool,
    }

    def fake_get(section, key, default=None, **kwargs):
        if (section, key) in overrides:
            return overrides[(section, key)]
        return real_get(section, key, default=default, **kwargs)

    return patch("meeseeks_core.tool_use_loop.get_config_value", side_effect=fake_get)


def _make_mcp_spec(tool_id: str) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"MCP spec for {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="mcp",
        read_only=False,
        metadata={
            "schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            }
        },
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_plan_dir_for_is_session_scoped(self):
        assert plan_dir_for("abc123").endswith("/tmp/meeseeks/plans/abc123")

    def test_plan_file_for_points_to_plan_md(self):
        assert plan_file_for("abc123").endswith("plan.md")

    def test_ensure_plan_dir_creates_directory_and_plan_file(self):
        sid = "test_ensure_plan_dir_" + os.urandom(4).hex()
        path = ensure_plan_dir(sid)
        assert os.path.isdir(path)
        assert os.path.isfile(plan_file_for(sid))
        # second call must not raise
        path2 = ensure_plan_dir(sid)
        assert path == path2
        shutil.rmtree(path)

    def test_concurrent_sessions_have_isolated_dirs(self):
        sid_a = "test_isolation_a_" + os.urandom(4).hex()
        sid_b = "test_isolation_b_" + os.urandom(4).hex()
        path_a = ensure_plan_dir(sid_a)
        path_b = ensure_plan_dir(sid_b)
        assert path_a != path_b
        assert os.path.isdir(path_a)
        assert os.path.isdir(path_b)
        shutil.rmtree(path_a)
        shutil.rmtree(path_b)

    def test_is_inside_plan_dir_accepts_plan_md(self):
        sid = "test_is_inside_ok_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        assert is_inside_plan_dir(plan_file_for(sid), sid) is True
        shutil.rmtree(plan_dir_for(sid), ignore_errors=True)

    def test_is_inside_plan_dir_rejects_traversal(self):
        sid_a = "test_traversal_" + os.urandom(4).hex()
        ensure_plan_dir(sid_a)
        # Attempt to escape with ..
        bad = os.path.join(plan_dir_for(sid_a), "..", "other", "plan.md")
        assert is_inside_plan_dir(bad, sid_a) is False
        shutil.rmtree(plan_dir_for(sid_a), ignore_errors=True)

    def test_is_inside_plan_dir_rejects_unrelated_path(self):
        sid = "test_unrelated_" + os.urandom(4).hex()
        assert is_inside_plan_dir("/etc/passwd", sid) is False


# ---------------------------------------------------------------------------
# Shell command allowlist matcher
# ---------------------------------------------------------------------------


class TestShellCommandAllowlist:
    """Unit tests for ``is_shell_command_plan_safe``."""

    # ---- Allowed cases ------------------------------------------------

    def test_single_token_exact_match(self):
        assert is_shell_command_plan_safe("ls", ["ls"]) is True

    def test_single_token_with_flags(self):
        assert is_shell_command_plan_safe("ls -la", ["ls"]) is True

    def test_single_token_with_path(self):
        assert is_shell_command_plan_safe("cat README.md", ["cat"]) is True

    def test_multi_token_prefix_match(self):
        assert is_shell_command_plan_safe("git log --oneline", ["git log"]) is True

    def test_multi_token_prefix_exact(self):
        assert is_shell_command_plan_safe("git status", ["git status"]) is True

    def test_multi_token_prefix_with_args(self):
        assert is_shell_command_plan_safe("git diff HEAD~5..HEAD --stat", ["git diff"]) is True

    def test_matches_any_in_list(self):
        assert is_shell_command_plan_safe("find . -name '*.py'", ["ls", "find", "grep"]) is True

    def test_quoted_arg_tokenization(self):
        # shlex correctly parses the quoted regex — still matches "grep"
        assert is_shell_command_plan_safe('grep -rn "class Foo" .', ["grep"]) is True

    # ---- Word-boundary / prefix rejection -----------------------------

    def test_lsof_does_not_match_ls(self):
        assert is_shell_command_plan_safe("lsof", ["ls"]) is False

    def test_git_alone_does_not_match_git_log(self):
        assert is_shell_command_plan_safe("git", ["git log"]) is False

    def test_git_logger_does_not_match_git_log(self):
        assert is_shell_command_plan_safe("git logger", ["git log"]) is False

    def test_git_status_does_not_match_git_log(self):
        assert is_shell_command_plan_safe("git status", ["git log"]) is False

    # ---- Metacharacter rejection --------------------------------------

    def test_pipe_rejected(self):
        assert is_shell_command_plan_safe("ls | head", ["ls", "head"]) is False

    def test_redirect_output_rejected(self):
        assert is_shell_command_plan_safe("ls > /etc/hosts", ["ls"]) is False

    def test_redirect_input_rejected(self):
        assert is_shell_command_plan_safe("cat < /etc/passwd", ["cat"]) is False

    def test_append_redirect_rejected(self):
        assert is_shell_command_plan_safe("echo hi >> x", ["echo"]) is False

    def test_semicolon_chain_rejected(self):
        assert is_shell_command_plan_safe("ls; rm -rf /", ["ls"]) is False

    def test_and_chain_rejected(self):
        assert is_shell_command_plan_safe("ls && rm x", ["ls"]) is False

    def test_backgrounding_rejected(self):
        assert is_shell_command_plan_safe("find . -type f &", ["find"]) is False

    def test_command_substitution_dollar_rejected(self):
        assert is_shell_command_plan_safe("cat $(cat /etc/passwd)", ["cat"]) is False

    def test_command_substitution_backtick_rejected(self):
        assert is_shell_command_plan_safe("echo `whoami`", ["echo"]) is False

    def test_variable_expansion_rejected(self):
        assert is_shell_command_plan_safe("echo $HOME", ["echo"]) is False

    # ---- Edge cases ---------------------------------------------------

    def test_empty_allowlist_denies_everything(self):
        assert is_shell_command_plan_safe("ls", []) is False

    def test_empty_command_denied(self):
        assert is_shell_command_plan_safe("", ["ls"]) is False

    def test_whitespace_only_command_denied(self):
        assert is_shell_command_plan_safe("   ", ["ls"]) is False

    def test_unbalanced_quotes_denied(self):
        assert is_shell_command_plan_safe('echo "unterminated', ["echo"]) is False

    def test_empty_string_entries_in_allowlist_ignored(self):
        # Whitespace-only allowlist entries should not accidentally match.
        assert is_shell_command_plan_safe("ls", ["", "   "]) is False
        assert is_shell_command_plan_safe("ls", ["", "ls"]) is True


class TestQuotedMetacharAllowed:
    """Metacharacters inside quoted arguments must not trigger rejection.

    The metachar guard is meant to block shell operators (pipes, redirects,
    chaining, expansion, substitution) that would evade the allowlist. A
    metachar sitting inside a single- or double-quoted argument is literal
    data to the shell and poses no evasion risk, so it must be allowed.
    This was the regression in session 3183ad54… where
    ``grep "JAZZ\\|BREEZY"`` was rejected.
    """

    def test_quoted_alternation_allowed(self):
        assert is_shell_command_plan_safe(r'grep -n "JAZZ\|BREEZY" file.py', ["grep"]) is True

    def test_quoted_dollar_anchor_allowed(self):
        assert is_shell_command_plan_safe('grep -n "end$" file.py', ["grep"]) is True

    def test_quoted_ampersand_in_find_allowed(self):
        assert is_shell_command_plan_safe('find . -name "foo&bar.py"', ["find"]) is True

    def test_git_log_grep_quoted_alternation_allowed(self):
        assert is_shell_command_plan_safe(r'git log --grep="fix\|feat"', ["git log"]) is True

    def test_single_quoted_metachar_allowed(self):
        assert is_shell_command_plan_safe("grep 'a|b' file.py", ["grep"]) is True

    def test_nested_quotes_allowed(self):
        # Single quote inside double quotes is literal — no toggle.
        # Dollar sign inside double quotes is shell-data, not our concern.
        assert is_shell_command_plan_safe('grep "it\'s $HOME" file', ["grep"]) is True

    def test_quoted_redirect_char_allowed(self):
        assert is_shell_command_plan_safe('grep "<tag>" file.html', ["grep"]) is True

    def test_escaped_metachar_outside_quotes_still_rejected(self):
        # Backslash-escaped metachar at the command level is a second
        # evasion syntax and is intentionally still rejected.
        assert is_shell_command_plan_safe(r"echo foo\|bar", ["echo"]) is False

    def test_trailing_backslash_does_not_crash(self):
        # Walker must not index past end of string. shlex.split raises
        # ValueError on unterminated escape, which the function catches
        # and returns False — the key assertion is "no IndexError".
        assert is_shell_command_plan_safe("echo foo\\", ["echo"]) is False


# ---------------------------------------------------------------------------
# Schema filtering
# ---------------------------------------------------------------------------


class TestPlanModeSchemaFiltering:
    """In plan mode the schema contains only read-only + edit + exit_plan_mode."""

    def _make_loop(self, *, current_mode: str) -> ToolUseLoop:
        ctx = _make_agent_context()
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=ToolRegistry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="schema_test_sid",
        )
        loop._current_mode = current_mode
        return loop

    def test_plan_mode_root_excludes_edit_tool(self):
        """Root (depth=0) in plan mode gets read-only + shell, but NOT the
        edit tool — root orchestrates, the plan sub-agent drafts.
        """
        read_spec = _make_spec("read_file", read_only=True)
        list_spec = _make_spec("aider_list_dir_tool", read_only=True)
        edit_spec = _make_edit_spec()
        shell_spec = _make_spec("aider_shell_tool", read_only=False)
        loop = self._make_loop(current_mode="plan")
        schemas = loop._build_tool_schemas_for_mode(
            [read_spec, list_spec, edit_spec, shell_spec],
            "plan",
        )
        names = {s["function"]["name"] for s in schemas}
        assert "read_file" in names
        assert "aider_list_dir_tool" in names
        assert "aider_edit_block_tool" not in names
        assert "aider_shell_tool" in names

    def test_act_mode_keeps_all(self):
        read_spec = _make_spec("read_file", read_only=True)
        shell_spec = _make_spec("aider_shell_tool", read_only=False)
        loop = self._make_loop(current_mode="act")
        schemas = loop._build_tool_schemas_for_mode(
            [read_spec, shell_spec],
            "act",
        )
        names = {s["function"]["name"] for s in schemas}
        assert names == {"read_file", "aider_shell_tool"}

    def test_plan_mode_includes_shell_when_allowlist_nonempty(self):
        read_spec = _make_spec("read_file", read_only=True)
        shell_spec = _make_spec("aider_shell_tool", read_only=False)
        loop = self._make_loop(current_mode="plan")
        with _patch_plan_config(shell_allowlist=["ls"], allow_mcp=False):
            schemas = loop._build_tool_schemas_for_mode(
                [read_spec, shell_spec],
                "plan",
            )
        names = {s["function"]["name"] for s in schemas}
        assert "aider_shell_tool" in names
        assert "read_file" in names

    def test_plan_mode_excludes_shell_when_allowlist_empty(self):
        read_spec = _make_spec("read_file", read_only=True)
        shell_spec = _make_spec("aider_shell_tool", read_only=False)
        loop = self._make_loop(current_mode="plan")
        with _patch_plan_config(shell_allowlist=[], allow_mcp=False):
            schemas = loop._build_tool_schemas_for_mode(
                [read_spec, shell_spec],
                "plan",
            )
        names = {s["function"]["name"] for s in schemas}
        assert "aider_shell_tool" not in names

    def test_plan_mode_includes_mcp_when_flag_true(self):
        read_spec = _make_spec("read_file", read_only=True)
        mcp_spec = _make_mcp_spec("mcp__devin__ask_question")
        loop = self._make_loop(current_mode="plan")
        with _patch_plan_config(shell_allowlist=[], allow_mcp=True):
            schemas = loop._build_tool_schemas_for_mode(
                [read_spec, mcp_spec],
                "plan",
            )
        names = {s["function"]["name"] for s in schemas}
        assert "mcp__devin__ask_question" in names

    def test_plan_mode_excludes_mcp_when_flag_false(self):
        read_spec = _make_spec("read_file", read_only=True)
        mcp_spec = _make_mcp_spec("mcp__devin__ask_question")
        loop = self._make_loop(current_mode="plan")
        with _patch_plan_config(shell_allowlist=[], allow_mcp=False):
            schemas = loop._build_tool_schemas_for_mode(
                [read_spec, mcp_spec],
                "plan",
            )
        names = {s["function"]["name"] for s in schemas}
        assert "mcp__devin__ask_question" not in names

    def test_v1_strict_behaviour_regression_guard(self):
        """Regression guard: empty allowlist + allow_mcp=False for root
        now yields read-only only (edit excluded for root by depth guard).
        """
        read_spec = _make_spec("read_file", read_only=True)
        edit_spec = _make_edit_spec()
        shell_spec = _make_spec("aider_shell_tool", read_only=False)
        mcp_spec = _make_mcp_spec("mcp__some__tool")
        loop = self._make_loop(current_mode="plan")
        with _patch_plan_config(shell_allowlist=[], allow_mcp=False):
            schemas = loop._build_tool_schemas_for_mode(
                [read_spec, edit_spec, shell_spec, mcp_spec],
                "plan",
            )
        names = {s["function"]["name"] for s in schemas}
        assert names == {"read_file"}

    def test_plan_mode_root_gets_agent_management_tools(self):
        """Plan-mode root (depth=0) gets spawn_agent, check_agents, steer_agent via _bind_model."""
        read_spec = _make_spec("read_file", read_only=True)
        edit_spec = _make_edit_spec()
        loop = self._make_loop(current_mode="plan")
        tool_schemas = loop._build_tool_schemas_for_mode(
            [read_spec, edit_spec],
            "plan",
        )
        with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
            model_obj = MagicMock()
            mock_build.return_value = model_obj
            model_obj.bind_tools.return_value = MagicMock()
            loop._bind_model(tool_schemas)
            # Inspect what was passed to bind_tools
            bound_schemas = model_obj.bind_tools.call_args[0][0]
            names = {s["function"]["name"] for s in bound_schemas}
            assert "spawn_agent" in names
            assert "check_agents" in names
            assert "steer_agent" in names
            assert "exit_plan_mode" in names


# ---------------------------------------------------------------------------
# Permission branch
# ---------------------------------------------------------------------------


class TestPlanModePermission:
    def _make_loop(self, session_id: str = "perm_test_sid") -> ToolUseLoop:
        ctx = _make_agent_context()
        read_spec = _make_spec("read_file", read_only=True)
        edit_spec = _make_edit_spec()
        shell_spec = _make_spec("aider_shell_tool", read_only=False)
        registry = _make_registry(read_spec, edit_spec, shell_spec)
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id=session_id,
        )
        loop._current_mode = "plan"
        return loop

    def test_read_only_tool_allowed(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": "/tmp/anywhere.txt"},
        )
        assert loop._check_permission(step) is True

    def test_edit_inside_plan_dir_allowed(self):
        sid = "perm_ok_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        loop = self._make_loop(session_id=sid)
        step = ActionStep(
            tool_id="aider_edit_block_tool",
            operation="set",
            tool_input={"file_path": plan_file_for(sid), "old_string": "", "new_string": "x"},
        )
        assert loop._check_permission(step) is True
        shutil.rmtree(plan_dir_for(sid), ignore_errors=True)

    def test_edit_outside_plan_dir_denied_with_actionable_message(self):
        sid = "perm_bad_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        loop = self._make_loop(session_id=sid)
        step = ActionStep(
            tool_id="aider_edit_block_tool",
            operation="set",
            tool_input={
                "file_path": "/tmp/not_the_plan.md",
                "old_string": "",
                "new_string": "x",
            },
        )
        assert loop._check_permission(step) is False
        msg = str(step.result.content) if step.result else ""
        assert "Plan mode: edits restricted to" in msg
        assert "/tmp/not_the_plan.md" in msg
        shutil.rmtree(plan_dir_for(sid), ignore_errors=True)

    def test_shell_tool_denied_when_allowlist_empty(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="aider_shell_tool",
            operation="set",
            tool_input={"command": "rm -rf /"},
        )
        with _patch_plan_config(shell_allowlist=[], allow_mcp=False):
            assert loop._check_permission(step) is False
        msg = str(step.result.content) if step.result else ""
        assert "Plan mode" in msg

    def test_allowlisted_shell_command_allowed(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="aider_shell_tool",
            operation="set",
            tool_input={"command": "git log --oneline -n 5"},
        )
        with _patch_plan_config(shell_allowlist=["git log"], allow_mcp=False):
            assert loop._check_permission(step) is True

    def test_non_allowlisted_shell_command_denied_with_allowlist_in_message(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="aider_shell_tool",
            operation="set",
            tool_input={"command": "rm -rf /"},
        )
        with _patch_plan_config(
            shell_allowlist=["ls", "git log"],
            allow_mcp=False,
        ):
            assert loop._check_permission(step) is False
        msg = str(step.result.content) if step.result else ""
        assert "shell command blocked" in msg
        assert "rm -rf /" in msg
        # Allowlist preview should include the configured entries
        assert "ls" in msg
        assert "git log" in msg

    def test_shell_command_with_pipe_denied(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="aider_shell_tool",
            operation="set",
            tool_input={"command": "ls | xargs rm"},
        )
        with _patch_plan_config(shell_allowlist=["ls"], allow_mcp=False):
            assert loop._check_permission(step) is False
        msg = str(step.result.content) if step.result else ""
        assert "shell command blocked" in msg

    def test_shell_allowlist_allows_quoted_metachar_command(self):
        # Regression: the substring metachar guard used to reject this
        # because of the `|` inside the quoted regex. grep with quoted
        # alternation is the canonical plan-mode exploration pattern.
        loop = self._make_loop()
        step = ActionStep(
            tool_id="aider_shell_tool",
            operation="set",
            tool_input={"command": 'grep -n "a|b" file.py'},
        )
        with _patch_plan_config(shell_allowlist=["grep"], allow_mcp=False):
            assert loop._check_permission(step) is True

    def test_mcp_tool_allowed_when_flag_true(self):
        mcp_spec = _make_mcp_spec("mcp__devin__ask_question")
        ctx = _make_agent_context()
        registry = _make_registry(mcp_spec)
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="mcp_perm_sid",
        )
        loop._current_mode = "plan"
        step = ActionStep(
            tool_id="mcp__devin__ask_question",
            operation="get",
            tool_input={"query": "x"},
        )
        with _patch_plan_config(shell_allowlist=[], allow_mcp=True):
            assert loop._check_permission(step) is True

    def test_mcp_tool_denied_when_flag_false(self):
        mcp_spec = _make_mcp_spec("mcp__devin__ask_question")
        ctx = _make_agent_context()
        registry = _make_registry(mcp_spec)
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="mcp_perm_sid",
        )
        loop._current_mode = "plan"
        step = ActionStep(
            tool_id="mcp__devin__ask_question",
            operation="get",
            tool_input={"query": "x"},
        )
        with _patch_plan_config(shell_allowlist=[], allow_mcp=False):
            assert loop._check_permission(step) is False

    def test_exit_plan_mode_tool_allowed(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="exit_plan_mode",
            operation="set",
            tool_input={},
        )
        assert loop._check_permission(step) is True

    def test_spawn_agent_allowed_for_root_in_plan_mode(self):
        """Root (depth=0) can call spawn_agent in plan mode."""
        loop = self._make_loop()
        step = ActionStep(
            tool_id="spawn_agent",
            operation="set",
            tool_input={"task": "plan something", "max_steps": 10},
        )
        assert loop._check_permission(step) is True

    def test_check_agents_allowed_for_root_in_plan_mode(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="check_agents",
            operation="get",
            tool_input={"wait": True},
        )
        assert loop._check_permission(step) is True

    def test_steer_agent_allowed_for_root_in_plan_mode(self):
        loop = self._make_loop()
        step = ActionStep(
            tool_id="steer_agent",
            operation="set",
            tool_input={"agent_id": "abc", "action": "cancel"},
        )
        assert loop._check_permission(step) is True


# ---------------------------------------------------------------------------
# ExitPlanModeTool handler
# ---------------------------------------------------------------------------


class TestExitPlanModeHandler:
    def _fresh_session(self) -> str:
        sid = "handler_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        return sid

    def _cleanup(self, sid: str) -> None:
        for name in os.listdir(plan_dir_for(sid)) or []:
            os.remove(os.path.join(plan_dir_for(sid), name))
        shutil.rmtree(plan_dir_for(sid), ignore_errors=True)

    def test_handler_with_empty_file_warns_then_overrides(self):
        """First call on empty file warns; second call overrides and exits."""
        sid = self._fresh_session()
        try:
            handler = ExitPlanModeTool(
                session_id=sid,
                event_logger=lambda e: None,
            )
            step = ActionStep(tool_id="exit_plan_mode", operation="set", tool_input={})
            # First call: warning, no termination.
            r1 = asyncio.run(handler.handle(step))
            assert "empty" in r1.content
            assert "override" in r1.content
            assert handler.should_terminate_run() is False
            # Second call: override, terminates.
            r2 = asyncio.run(handler.handle(step))
            assert "Plan proposed" in r2.content
            assert handler.should_terminate_run() is True
        finally:
            self._cleanup(sid)

    def test_handler_with_whitespace_only_plan_file_warns(self):
        sid = self._fresh_session()
        try:
            with open(plan_file_for(sid), "w") as f:
                f.write("   \n")
            handler = ExitPlanModeTool(
                session_id=sid,
                event_logger=None,
            )
            step = ActionStep(tool_id="exit_plan_mode", operation="set", tool_input={})
            result = asyncio.run(handler.handle(step))
            assert "empty" in result.content
            assert "override" in result.content
        finally:
            self._cleanup(sid)

    def test_handler_returns_immediately_with_termination_signal(self):
        sid = self._fresh_session()
        try:
            with open(plan_file_for(sid), "w") as f:
                f.write("# Plan\n\nDo the thing.\n")
            events: list[dict] = []
            handler = ExitPlanModeTool(
                session_id=sid,
                event_logger=lambda e: events.append(e),
            )
            step = ActionStep(tool_id="exit_plan_mode", operation="set", tool_input={})
            result = asyncio.run(handler.handle(step))
            # Handler returns immediately — no queue needed
            assert "Plan proposed" in result.content
            assert "terminate" in result.content
            # Termination flag is set
            assert handler.should_terminate_run() is True
            # Flag consumed on second call
            assert handler.should_terminate_run() is False
            # Only plan_proposed emitted — no plan_approved or plan_rejected
            event_types = [e["type"] for e in events]
            assert "plan_proposed" in event_types
            assert "plan_approved" not in event_types
            assert "plan_rejected" not in event_types
        finally:
            self._cleanup(sid)

    def test_revision_counter_increments_across_calls(self):
        sid = self._fresh_session()
        try:
            with open(plan_file_for(sid), "w") as f:
                f.write("# Plan\n\nv1.\n")
            events: list[dict] = []
            handler = ExitPlanModeTool(
                session_id=sid,
                event_logger=lambda e: events.append(e),
            )
            step = ActionStep(tool_id="exit_plan_mode", operation="set", tool_input={})
            asyncio.run(handler.handle(step))
            # Consume the termination flag before the next call
            handler.should_terminate_run()
            asyncio.run(handler.handle(step))
            proposed = [e for e in events if e["type"] == "plan_proposed"]
            revisions = [e["payload"]["revision"] for e in proposed]
            assert revisions == [1, 2]
        finally:
            self._cleanup(sid)


# ---------------------------------------------------------------------------
# End-to-end mode transition
# ---------------------------------------------------------------------------


class TestPlanModeEndToEnd:
    """Driving a loop through: exit_plan_mode call → termination with awaiting_approval."""

    def test_exit_plan_mode_terminates_loop_with_awaiting_approval(self):
        sid = "e2e_terminate_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        with open(plan_file_for(sid), "w") as f:
            f.write("# Plan\n\nStep 1.\n")
        try:
            events: list[dict] = []
            ctx = _make_agent_context(event_logger=lambda e: events.append(e))
            read_spec = _make_spec("read_file", read_only=True)
            registry = _make_registry(read_spec)

            # Model calls exit_plan_mode — the loop should terminate immediately.
            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(
                return_value=_tool_call_response("exit_plan_mode", {}, "call_1"),
            )
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke

            with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
                model_obj = MagicMock()
                model_obj.bind_tools.return_value = bound
                mock_build.return_value = model_obj

                loop = ToolUseLoop(
                    agent_context=ctx,
                    tool_registry=registry,
                    permission_policy=_allow_all_policy(),
                    hook_manager=_make_hook_manager(),
                    session_id=sid,
                )
                _tq, state = asyncio.run(
                    loop.run(
                        "Draft a plan.",
                        tool_specs=[read_spec],
                        context=_make_context(),
                        mode="plan",
                    )
                )

            assert state.done is True
            assert state.done_reason == "awaiting_approval"
            # plan_proposed event must have been emitted
            event_types = [e["type"] for e in events]
            assert "plan_proposed" in event_types
        finally:
            for name in os.listdir(plan_dir_for(sid)) or []:
                os.remove(os.path.join(plan_dir_for(sid), name))
            shutil.rmtree(plan_dir_for(sid), ignore_errors=True)


class TestPlanModeHypervisorIntegration:
    """Integration: plan-mode root spawn_agent is not denied.

    Drives ToolUseLoop.run() to exercise both schema (_bind_model) and
    permission (_plan_mode_permission) in a single test.  This is the
    test that would have caught the schema-permission gap in production
    session 58e760587c6e.
    """

    def test_root_spawn_agent_not_denied_by_permission(self):
        """spawn_agent in plan mode is NOT denied when depth=0."""
        sid = "hyp_integ_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        with open(plan_file_for(sid), "w") as f:
            f.write("# Plan\n\nDo the thing.\n")
        try:
            events: list[dict] = []
            ctx = _make_agent_context(event_logger=lambda e: events.append(e))
            read_spec = _make_spec("read_file", read_only=True)
            registry = _make_registry(read_spec)

            # Step 1: model calls spawn_agent → should not be denied
            # Step 2: model calls exit_plan_mode → loop terminates
            call_count = 0

            async def fake_ainvoke(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return _tool_call_response(
                        "spawn_agent",
                        {"task": "draft plan"},
                        "call_spawn",
                    )
                return _tool_call_response("exit_plan_mode", {}, "call_exit")

            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(side_effect=fake_ainvoke)
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke

            with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
                model_obj = MagicMock()
                model_obj.bind_tools.return_value = bound
                mock_build.return_value = model_obj

                loop = ToolUseLoop(
                    agent_context=ctx,
                    tool_registry=registry,
                    permission_policy=_allow_all_policy(),
                    hook_manager=_make_hook_manager(),
                    session_id=sid,
                )
                _tq, state = asyncio.run(
                    loop.run(
                        "Draft a plan.",
                        tool_specs=[read_spec],
                        context=_make_context(),
                        mode="plan",
                    )
                )

            # spawn_agent should NOT have been denied by permission
            permission_denials = [
                e
                for e in events
                if e.get("type") == "permission"
                and e.get("payload", {}).get("decision") == "deny"
                and e.get("payload", {}).get("tool_id") == "spawn_agent"
            ]
            assert permission_denials == [], (
                "spawn_agent was denied by _plan_mode_permission — "
                "schema-permission gap still exists"
            )
        finally:
            for name in os.listdir(plan_dir_for(sid)) or []:
                os.remove(os.path.join(plan_dir_for(sid), name))
            shutil.rmtree(plan_dir_for(sid), ignore_errors=True)


# ---------------------------------------------------------------------------
# Orchestrator mode resolution
# ---------------------------------------------------------------------------


class TestOrchestratorModeResolution:
    def test_explicit_plan_respected(self):
        assert Orchestrator._resolve_mode("plan") == "plan"

    def test_explicit_act_respected(self):
        assert Orchestrator._resolve_mode("act") == "act"

    def test_none_defaults_to_act(self):
        assert Orchestrator._resolve_mode(None) == "act"

    def test_unknown_string_defaults_to_act(self):
        assert Orchestrator._resolve_mode("bogus") == "act"


# ---------------------------------------------------------------------------
# Path guards — lower guard (resolve_safe_path) must know about plan dir
# ---------------------------------------------------------------------------


class TestAllowedRootsIncludesPlanDir:
    """The edit tool's lower path guard must accept plan-dir writes.

    Plan mode sends edits to ``/tmp/meeseeks/plans/<session_id>/plan.md``.
    The upper guard (``_plan_mode_permission``) approves such writes via
    ``is_inside_plan_dir``. The lower guard (``resolve_safe_path`` in
    ``meeseeks_tools.core``) must also accept them — otherwise edits are
    approved upstream and rejected downstream, and plan.md can never be
    written. This was the showstopper in session 3183ad5449…
    """

    def test_get_allowed_roots_includes_plan_dir_root(self):
        from pathlib import Path

        from meeseeks_tools.core import _get_allowed_roots

        roots = _get_allowed_roots()
        assert Path(PLAN_DIR_ROOT).resolve() in roots

    def test_resolve_safe_path_accepts_plan_file(self):
        from meeseeks_tools.core import resolve_safe_path

        sid = "roots_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        try:
            target = plan_file_for(sid)
            resolved = resolve_safe_path(target)
            assert str(resolved) == target
        finally:
            shutil.rmtree(plan_dir_for(sid), ignore_errors=True)

    def test_resolve_safe_path_still_rejects_unrelated_tmp_paths(self):
        """Widening the allowlist must not let /tmp/* outside the plan dir through."""
        from meeseeks_tools.core import resolve_safe_path

        try:
            resolve_safe_path("/tmp/not-a-plan-file.sh")
        except ValueError as exc:
            assert "outside all allowed project roots" in str(exc)
        else:
            raise AssertionError("expected ValueError for /tmp/not-a-plan-file.sh")

    def test_resolve_safe_path_still_rejects_system_paths(self):
        from meeseeks_tools.core import resolve_safe_path

        try:
            resolve_safe_path("/etc/passwd")
        except ValueError as exc:
            assert "outside all allowed project roots" in str(exc)
        else:
            raise AssertionError("expected ValueError for /etc/passwd")


class TestEpisodicPlanApproval:
    """SessionRuntime.approve_plan/reject_plan emit transcript events."""

    def _make_runtime(self):
        from meeseeks_core.session_runtime import SessionRuntime
        from meeseeks_core.session_store import create_session_store

        store = create_session_store()
        return SessionRuntime(session_store=store)

    def test_approve_emits_event(self):
        runtime = self._make_runtime()
        sid = runtime.resolve_session()
        runtime.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "plan something"}}
        )
        runtime.session_store.append_event(
            sid,
            {
                "type": "plan_proposed",
                "payload": {
                    "plan_path": "/tmp/test/plan.md",
                    "revision": 1,
                    "content": "# Plan",
                    "summary": "test",
                },
            },
        )
        runtime.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "awaiting_approval"},
            },
        )
        assert not runtime.is_running(sid)
        ok = runtime.approve_plan(sid)
        assert ok is True
        events = runtime.session_store.load_transcript(sid)
        approved = [e for e in events if e["type"] == "plan_approved"]
        assert len(approved) == 1
        assert approved[0]["payload"]["revision"] == 1

    def test_reject_emits_event_no_new_run(self):
        runtime = self._make_runtime()
        sid = runtime.resolve_session()
        runtime.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "plan something"}}
        )
        runtime.session_store.append_event(
            sid,
            {
                "type": "plan_proposed",
                "payload": {
                    "plan_path": "/tmp/test/plan.md",
                    "revision": 1,
                    "content": "# Plan",
                    "summary": "",
                },
            },
        )
        runtime.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "awaiting_approval"},
            },
        )
        ok = runtime.reject_plan(sid)
        assert ok is True
        events = runtime.session_store.load_transcript(sid)
        rejected = [e for e in events if e["type"] == "plan_rejected"]
        assert len(rejected) == 1
        assert not runtime.is_running(sid)

    def test_approve_fails_when_no_pending_proposal(self):
        runtime = self._make_runtime()
        sid = runtime.resolve_session()
        runtime.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "just a query"}}
        )
        runtime.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "completed"},
            },
        )
        ok = runtime.approve_plan(sid)
        assert ok is False

    def test_approve_fails_when_already_resolved(self):
        runtime = self._make_runtime()
        sid = runtime.resolve_session()
        runtime.session_store.append_event(
            sid,
            {
                "type": "plan_proposed",
                "payload": {"plan_path": "/tmp/p.md", "revision": 1, "content": "x", "summary": ""},
            },
        )
        runtime.session_store.append_event(
            sid,
            {
                "type": "plan_approved",
                "payload": {"plan_path": "/tmp/p.md", "revision": 1},
            },
        )
        ok = runtime.approve_plan(sid)
        assert ok is False

    def test_summarize_session_awaiting_approval_status(self):
        runtime = self._make_runtime()
        sid = runtime.resolve_session()
        runtime.session_store.append_event(sid, {"type": "user", "payload": {"text": "plan x"}})
        runtime.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "awaiting_approval"},
            },
        )
        summary = runtime.summarize_session(sid)
        assert summary["status"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# Depth guidance
# ---------------------------------------------------------------------------


class TestPlanModeDepthGuidance:
    """Depth guidance text reflects plan-mode hypervisor role for root."""

    def test_plan_mode_root_returns_hypervisor_guidance(self):
        ctx = _make_agent_context()
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=ToolRegistry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="depth_test_sid",
        )
        loop._current_mode = "plan"
        guidance = loop._build_depth_guidance()
        assert "plan mode" in guidance
        assert "approved plan" in guidance
        assert "sub-agent" in guidance
        assert "exit_plan_mode" in guidance
        # Shared root sections are preserved (KISS/DRY — not replaced).
        assert "Async delegation protocol" in guidance
        assert "Safety" in guidance
        assert "System awareness" in guidance
        assert "When to stop" in guidance

    def test_act_mode_root_returns_standard_guidance(self):
        ctx = _make_agent_context()
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=ToolRegistry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="depth_test_sid",
        )
        loop._current_mode = "act"
        guidance = loop._build_depth_guidance()
        assert "Root hypervisor" in guidance
        assert "Direct execution" in guidance


# ---------------------------------------------------------------------------
# System prompt template selection
# ---------------------------------------------------------------------------


class TestPlanModeSystemPrompt:
    """Root agent in plan mode uses plan_hypervisor.txt, not plan_mode_reminder.txt."""

    def test_root_uses_hypervisor_prompt_not_reminder(self):
        sid = "sysprompt_test_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        try:
            ctx = _make_agent_context()
            read_spec = _make_spec("read_file", read_only=True)
            registry = _make_registry(read_spec)
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                session_id=sid,
            )
            loop._current_mode = "plan"
            messages = loop._build_messages(
                "Draft a plan.",
                _make_context(),
                None,
                agent_tree="",
            )
            system_content = messages[0].content
            # Root gets plan_hypervisor.txt content
            assert "root hypervisor" in system_content.lower()
            # Root does NOT get plan_mode_reminder.txt content (sub-agent prompt)
            assert "planning agent" not in system_content.lower()
        finally:
            shutil.rmtree(plan_dir_for(sid), ignore_errors=True)


class TestEditToolWritesPlanMdEndToEnd:
    """Bridges both guards: plan-mode edit via the real FileEditTool writes
    to the session plan dir successfully. Would have caught the lower-guard
    rejection that made plan mode unreachable.
    """

    def test_file_edit_tool_writes_plan_md(self):
        from meeseeks_core.classes import ActionStep as AS
        from meeseeks_tools.integration.file_edit_tool import FileEditTool

        sid = "e2e_" + os.urandom(4).hex()
        ensure_plan_dir(sid)
        target = plan_file_for(sid)
        try:
            tool = FileEditTool()
            # Create the file by inserting content (old_string="" = append/create).
            step = AS(
                tool_id="file_edit_tool",
                operation="set",
                tool_input={
                    "file_path": target,
                    "old_string": "",
                    "new_string": "# Plan\n\n1. Do the thing.\n",
                    "root": PLAN_DIR_ROOT,
                },
            )
            result = tool.set_state(step)
            assert result is not None
            assert os.path.exists(target)
            with open(target, encoding="utf-8") as handle:
                content = handle.read()
            assert "Do the thing" in content
        finally:
            if os.path.exists(target):
                os.remove(target)
            shutil.rmtree(plan_dir_for(sid), ignore_errors=True)
