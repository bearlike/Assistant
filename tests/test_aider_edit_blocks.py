"""Tests for Aider-style SEARCH/REPLACE block handling."""

from __future__ import annotations

import textwrap

import pytest
from meeseeks_core.classes import ActionStep
from meeseeks_core.errors import ToolInputError
from meeseeks_tools.aider_bridge import (
    EditBlockApplyError,
    apply_search_replace_blocks,
    parse_search_replace_blocks,
)
from meeseeks_tools.integration.aider_edit_blocks import (
    AiderEditBlockTool,
    _format_tool_input_error,
)


def _block(path: str, search: str, replace: str) -> str:
    return f"{path}\n```text\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n```\n"


def test_apply_search_replace_block(tmp_path):
    """Apply a simple SEARCH/REPLACE block to an existing file."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    content = _block("hello.txt", "world\n", "there\n")
    results = apply_search_replace_blocks(content, root=str(tmp_path), write=True)

    assert target.read_text(encoding="utf-8") == "hello\nthere\n"
    assert results[0].applied is True


def test_create_new_file(tmp_path):
    """Create a new file from an empty SEARCH block."""
    content = _block("new.txt", "", "first line\n")
    results = apply_search_replace_blocks(content, root=str(tmp_path), write=True)

    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "first line\n"
    assert results[0].created is True


def test_shell_blocks_are_rejected(tmp_path):
    """Reject shell command blocks in edit content."""
    content = textwrap.dedent(
        """
        ```bash
        echo "hello"
        ```
        """
    ).lstrip()
    with pytest.raises(EditBlockApplyError):
        apply_search_replace_blocks(content, root=str(tmp_path), write=False)


def test_path_traversal_is_blocked(tmp_path):
    """Block edits that escape the project root."""
    content = _block("../oops.txt", "", "nope\n")
    with pytest.raises(EditBlockApplyError):
        apply_search_replace_blocks(content, root=str(tmp_path), write=False)


def test_append_when_search_empty(tmp_path):
    """Append content when SEARCH block is empty and file exists."""
    target = tmp_path / "append.txt"
    target.write_text("hello\n", encoding="utf-8")

    content = _block("append.txt", "", "world\n")
    results = apply_search_replace_blocks(content, root=str(tmp_path), write=True)

    assert target.read_text(encoding="utf-8") == "hello\nworld\n"
    assert results[0].applied is True


def test_search_miss_includes_hint(tmp_path):
    """Include a hint when SEARCH block fails to match."""
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")

    content = _block("hello.txt", "alpha\nbeta\ngamaa\ndelta\n", "there\n")
    with pytest.raises(EditBlockApplyError) as exc:
        apply_search_replace_blocks(content, root=str(tmp_path), write=False)

    assert "Did you mean" in str(exc.value)


@pytest.mark.parametrize(
    ("operation", "content", "files", "expected"),
    [
        ("set", "no edits here", None, "SEARCH/REPLACE blocks"),
        ("get", "no edits here", None, "SEARCH/REPLACE blocks"),
        ("set", "```bash\necho 'no'\n```\n", None, "Shell command blocks"),
        ("get", "```bash\necho 'no'\n```\n", None, "Shell command blocks"),
        ("set", _block("hello.txt", "world\n", "there\n"), "hello.txt", "files must be a list"),
    ],
)
def test_edit_block_tool_rejects_invalid_inputs(tmp_path, operation, content, files, expected):
    """Reject invalid tool inputs with guidance."""
    tool = AiderEditBlockTool()
    argument = {"content": content, "root": str(tmp_path)}
    if files is not None:
        argument["files"] = files
    step = ActionStep(
        tool_id="aider_edit_block_tool",
        operation=operation,
        tool_input=argument,
    )
    with pytest.raises(ToolInputError) as exc:
        tool.run(step)
    assert expected in str(exc.value)


def test_edit_block_tool_rejects_invalid_payload_type(tmp_path):
    """Reject non-string/non-object payloads."""
    tool = AiderEditBlockTool()
    step = ActionStep.construct(
        tool_id="aider_edit_block_tool",
        operation="set",
        tool_input=123,
    )
    with pytest.raises(ToolInputError) as exc:
        tool.run(step)
    assert "Tool input must be a string or object payload" in str(exc.value)


def test_edit_block_tool_wraps_apply_errors(tmp_path):
    """Wrap apply errors with format guidance."""
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = AiderEditBlockTool()
    step = ActionStep(
        tool_id="aider_edit_block_tool",
        operation="set",
        tool_input={
            "content": _block("hello.txt", "alpha\nbeta\ngamaa\ndelta\n", "there\n"),
            "root": str(tmp_path),
        },
    )
    with pytest.raises(ToolInputError) as exc:
        tool.set_state(step)
    message = str(exc.value)
    assert "Did you mean" in message
    assert "Expected format" in message


def test_edit_block_tool_set_state_returns_diff(tmp_path):
    """Return a diff payload when changes are applied."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    tool = AiderEditBlockTool()
    step = ActionStep(
        tool_id="aider_edit_block_tool",
        operation="set",
        tool_input={
            "content": _block("hello.txt", "world\n", "there\n"),
            "root": str(tmp_path),
        },
    )
    result = tool.set_state(step)
    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "diff"
    assert "-world" in payload.get("text", "")
    assert "+there" in payload.get("text", "")


def test_edit_block_tool_set_state_summary_when_no_diff(tmp_path):
    """Return a summary when the diff is empty."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    tool = AiderEditBlockTool()
    step = ActionStep(
        tool_id="aider_edit_block_tool",
        operation="set",
        tool_input={
            "content": _block("hello.txt", "world\n", "world\n"),
            "root": str(tmp_path),
        },
    )
    result = tool.set_state(step)
    assert isinstance(result.content, str)
    assert "Applied" in result.content


def test_edit_block_tool_get_state_summary(tmp_path):
    """Return a summary when validating without writes."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    tool = AiderEditBlockTool()
    step = ActionStep(
        tool_id="aider_edit_block_tool",
        operation="get",
        tool_input={
            "content": _block("hello.txt", "world\n", "there\n"),
            "root": str(tmp_path),
        },
    )
    result = tool.get_state(step)
    assert isinstance(result.content, str)
    assert "Validated" in result.content


def test_edit_block_tool_uses_cwd_for_string_argument(monkeypatch, tmp_path):
    """Default to the current working directory for string arguments."""
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    tool = AiderEditBlockTool()
    step = ActionStep(
        tool_id="aider_edit_block_tool",
        operation="set",
        tool_input=_block("hello.txt", "world\n", "there\n"),
    )
    tool.set_state(step)
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"


def test_edit_block_tool_input_error_guidance_when_blank():
    """Return guidance when no message is provided."""
    message = _format_tool_input_error("")
    assert "Expected format" in message


def test_apply_search_replace_block_with_ellipses(tmp_path):
    """Apply a SEARCH/REPLACE block that uses ellipses to skip unchanged lines."""
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    content = _block("hello.txt", "alpha\n...\ngamma\n", "alpha\n...\ndelta\n")
    apply_search_replace_blocks(content, root=str(tmp_path), write=True)
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ndelta\n"


def test_apply_search_replace_block_with_unpaired_ellipses(tmp_path):
    """Raise when ellipses are not paired between SEARCH and REPLACE."""
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    content = _block("hello.txt", "alpha\n...\ngamma\n", "alpha\ndelta\n")
    with pytest.raises(EditBlockApplyError):
        apply_search_replace_blocks(content, root=str(tmp_path), write=False)


def test_apply_search_replace_block_missing_leading_whitespace(tmp_path):
    """Match a block when the SEARCH omits leading whitespace."""
    target = tmp_path / "indent.txt"
    target.write_text("    one\n    two\n", encoding="utf-8")
    content = _block("indent.txt", "one\ntwo\n", "one\nthree\n")
    apply_search_replace_blocks(content, root=str(tmp_path), write=True)
    assert target.read_text(encoding="utf-8") == "    one\n    three\n"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("```foo.py", "foo.py"),
        ("# bar.py:", "bar.py"),
    ],
)
def test_parse_search_replace_blocks_filename_variants(header, expected):
    """Normalize filename markers before SEARCH blocks."""
    content = f"{header}\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
    edits, shell_blocks = parse_search_replace_blocks(content, valid_fnames=None)
    assert not shell_blocks
    assert edits[0].path == expected


def test_parse_search_replace_blocks_prefers_valid_fnames():
    """Prefer close matches from valid filename list."""
    content = "file.txt\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
    edits, _ = parse_search_replace_blocks(content, valid_fnames=["a/file.txt"])
    assert edits[0].path == "a/file.txt"
