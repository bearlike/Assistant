"""Tests for local file tools (read_file, list_dir)."""

from __future__ import annotations

from truss_core.classes import ActionStep
from truss_tools.integration.aider_file_tools import AiderListDirTool, ReadFileTool


def test_read_file_reads(tmp_path):
    """Read a file using the read tool."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\n", encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "hello.txt", "root": str(tmp_path)},
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "file"
    assert payload.get("path") == "hello.txt"
    assert "1\thello" in payload.get("text", "")
    assert payload.get("total_lines") == 1


def test_aider_list_dir_tool_lists(tmp_path):
    """List a directory using the Aider list tool."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "file.txt").write_text("data", encoding="utf-8")

    tool = AiderListDirTool()
    step = ActionStep(
        tool_id="aider_list_dir_tool",
        operation="get",
        tool_input={"path": "a", "root": str(tmp_path)},
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
    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "../oops.txt", "root": str(tmp_path)},
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "resolves outside all allowed project roots" in result.content


def test_aider_read_file_truncates(tmp_path):
    """Truncate file contents when max_bytes is set."""
    target = tmp_path / "long.txt"
    target.write_text("hello world\n", encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "long.txt", "root": str(tmp_path), "max_bytes": "5"},
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("text").endswith("... (truncated)")


def test_aider_read_file_invalid_argument_type():
    """Reject missing path payloads."""
    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": ""},
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "path is required" in result.content


def test_aider_read_file_rejects_non_string_payload():
    """Reject invalid tool input types."""
    tool = ReadFileTool()
    step = ActionStep.model_construct(
        tool_id="read_file",
        operation="get",
        tool_input=123,
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "Tool input must be a string path" in result.content


def test_aider_list_dir_limits_entries(tmp_path):
    """Stop listing when max_entries is reached."""
    (tmp_path / "a").mkdir()
    for name in ["one.txt", "two.txt"]:
        (tmp_path / "a" / name).write_text("data", encoding="utf-8")

    tool = AiderListDirTool()
    step = ActionStep(
        tool_id="aider_list_dir_tool",
        operation="get",
        tool_input={"path": "a", "root": str(tmp_path), "max_entries": 1},
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
        tool_id="aider_list_dir_tool",
        operation="get",
        tool_input={"path": "", "root": str(tmp_path), "max_entries": 10},
    )
    result = tool.get_state(step)
    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "dir"


def test_aider_list_dir_rejects_invalid_payload_type():
    """Reject invalid tool input types."""
    tool = AiderListDirTool()
    step = ActionStep.model_construct(
        tool_id="aider_list_dir_tool",
        operation="get",
        tool_input=123,
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "Tool input must be a string path" in result.content


def test_read_file_offset_and_limit(tmp_path):
    """Read a specific range of lines."""
    target = tmp_path / "multi.txt"
    target.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "multi.txt", "root": str(tmp_path), "offset": 1, "limit": 2},
    )
    result = tool.get_state(step)
    payload = result.content
    assert isinstance(payload, dict)
    text = payload.get("text", "")
    assert "2\tline2" in text
    assert "3\tline3" in text
    assert "1\tline1" not in text  # skipped by offset
    assert "4\tline4" not in text  # cut by limit
    assert payload.get("total_lines") == 5


def test_read_file_default_line_numbers(tmp_path):
    """Output includes line numbers by default."""
    target = tmp_path / "numbered.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "numbered.txt", "root": str(tmp_path)},
    )
    result = tool.get_state(step)
    text = result.content.get("text", "")
    assert "1\talpha" in text
    assert "2\tbeta" in text
    assert "3\tgamma" in text


def test_read_file_truncation_message(tmp_path):
    """Large files show truncation hint."""
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"line{i}" for i in range(3000)), encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "big.txt", "root": str(tmp_path)},
    )
    result = tool.get_state(step)
    payload = result.content
    assert payload.get("total_lines") == 3000
    assert "truncated" in payload.get("text", "")
