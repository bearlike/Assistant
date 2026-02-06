"""Tests for Aider-based local file tools."""

from __future__ import annotations

from meeseeks_core.classes import ActionStep
from meeseeks_tools.integration.aider_file_tools import AiderListDirTool, AiderReadFileTool


def test_aider_read_file_tool_reads(tmp_path):
    """Read a file using the Aider read tool."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\n", encoding="utf-8")

    tool = AiderReadFileTool()
    step = ActionStep(
        action_consumer="aider_read_file_tool",
        action_type="get",
        action_argument={"path": "hello.txt", "root": str(tmp_path)},
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "file"
    assert payload.get("path") == "hello.txt"
    assert payload.get("text") == "hello\n"


def test_aider_list_dir_tool_lists(tmp_path):
    """List a directory using the Aider list tool."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "file.txt").write_text("data", encoding="utf-8")

    tool = AiderListDirTool()
    step = ActionStep(
        action_consumer="aider_list_dir_tool",
        action_type="get",
        action_argument={"path": "a", "root": str(tmp_path)},
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "dir"
    assert payload.get("path") == "a"
    entries = payload.get("entries")
    assert isinstance(entries, list)
    assert "a/file.txt" in entries


def test_aider_read_file_blocks_escape(tmp_path):
    """Reject path traversal attempts."""
    tool = AiderReadFileTool()
    step = ActionStep(
        action_consumer="aider_read_file_tool",
        action_type="get",
        action_argument={"path": "../oops.txt", "root": str(tmp_path)},
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "outside the project root" in result.content


def test_aider_read_file_truncates(tmp_path):
    """Truncate file contents when max_bytes is set."""
    target = tmp_path / "long.txt"
    target.write_text("hello world\n", encoding="utf-8")

    tool = AiderReadFileTool()
    step = ActionStep(
        action_consumer="aider_read_file_tool",
        action_type="get",
        action_argument={"path": "long.txt", "root": str(tmp_path), "max_bytes": "5"},
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("text").endswith("... (truncated)")


def test_aider_read_file_invalid_argument_type():
    """Reject missing path payloads."""
    tool = AiderReadFileTool()
    step = ActionStep(
        action_consumer="aider_read_file_tool",
        action_type="get",
        action_argument={"path": ""},
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "path is required" in result.content


def test_aider_list_dir_limits_entries(tmp_path):
    """Stop listing when max_entries is reached."""
    (tmp_path / "a").mkdir()
    for name in ["one.txt", "two.txt"]:
        (tmp_path / "a" / name).write_text("data", encoding="utf-8")

    tool = AiderListDirTool()
    step = ActionStep(
        action_consumer="aider_list_dir_tool",
        action_type="get",
        action_argument={"path": "a", "root": str(tmp_path), "max_entries": 1},
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict)
    assert len(payload.get("entries", [])) == 1


def test_aider_list_dir_defaults_to_root(tmp_path):
    """Use root listing when path is empty."""
    (tmp_path / "root.txt").write_text("data", encoding="utf-8")
    tool = AiderListDirTool()
    step = ActionStep(
        action_consumer="aider_list_dir_tool",
        action_type="get",
        action_argument={"path": "", "root": str(tmp_path), "max_entries": 10},
    )
    result = tool.get_state(step)
    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "dir"
