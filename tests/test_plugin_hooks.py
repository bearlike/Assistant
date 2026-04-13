# tests/test_plugin_hooks.py
"""Tests for plugin hook format translation (merge_plugin_hooks)."""

from __future__ import annotations

from meeseeks_core.hooks import HookManager, merge_plugin_hooks


def test_merge_plugin_hooks_session_start():
    manager = HookManager()
    assert len(manager.on_session_start) == 0
    hooks_json = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo ${CLAUDE_PLUGIN_ROOT}/start.sh"}]}
            ],
        }
    }
    merge_plugin_hooks(manager, hooks_json, plugin_root="/opt/plugins/test")
    assert len(manager.on_session_start) == 1


def test_merge_plugin_hooks_pre_tool_use():
    manager = HookManager()
    hooks_json = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "aider_shell_tool",
                    "hooks": [{"type": "command", "command": "echo pre", "timeout": 10}],
                }
            ],
        }
    }
    merge_plugin_hooks(manager, hooks_json, plugin_root="/opt")
    assert len(manager.pre_tool_use) == 1


def test_merge_plugin_hooks_skips_non_command():
    """Only 'command' type hooks are processed; 'prompt' type is skipped."""
    manager = HookManager()
    hooks_json = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "prompt", "prompt": "Check safety"}]}
            ],
        }
    }
    merge_plugin_hooks(manager, hooks_json, plugin_root="/opt")
    assert len(manager.on_session_start) == 0


def test_merge_plugin_hooks_skips_unknown_events():
    """Unknown CC hook events are silently skipped."""
    manager = HookManager()
    hooks_json = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
            "SubagentStop": [{"hooks": [{"type": "command", "command": "echo sub"}]}],
        }
    }
    merge_plugin_hooks(manager, hooks_json, plugin_root="/opt")
    # Neither Stop nor SubagentStop are in our mapping
    assert len(manager.pre_tool_use) == 0
    assert len(manager.on_session_start) == 0


def test_merge_plugin_hooks_variable_substitution():
    """${CLAUDE_PLUGIN_ROOT} should be replaced in command strings."""
    manager = HookManager()
    hooks_json = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh"}
                    ]
                }
            ],
        }
    }
    plugin_root = "/home/user/.meeseeks/plugins/cache/mp/plug/1.0"
    merge_plugin_hooks(manager, hooks_json, plugin_root=plugin_root)
    assert len(manager.on_session_start) == 1


def test_merge_plugin_hooks_multiple():
    """Multiple hooks in one event group."""
    manager = HookManager()
    hooks_json = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo first"},
                        {"type": "command", "command": "echo second"},
                    ],
                }
            ],
        }
    }
    merge_plugin_hooks(manager, hooks_json, plugin_root="/opt")
    assert len(manager.pre_tool_use) == 2


def test_merge_plugin_hooks_empty():
    """Empty hooks dict should be a no-op."""
    manager = HookManager()
    merge_plugin_hooks(manager, {}, plugin_root="/opt")
    merge_plugin_hooks(manager, {"hooks": {}}, plugin_root="/opt")
    assert len(manager.pre_tool_use) == 0
    assert len(manager.on_session_start) == 0
