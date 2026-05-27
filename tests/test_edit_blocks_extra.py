"""Extra tests for edit-block and file-edit paths.

Targets:
- mewbo_tools/aider_bridge/edit_blocks.py  (85.1% → +)
- mewbo_tools/integration/file_edit_tool.py (70.1% → +)
- mewbo_tools/integration/aider_file_tools.py (84.7% → +)

Covers real edit application, malformed-block handling, the {"kind":"diff",...}
contract, replace_all semantics, error paths, and parser edge-cases — all using
tmp_path (no network, no subprocesses).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.errors import ToolInputError
from mewbo_tools.aider_bridge.edit_blocks import (
    EditBlockApplyError,
    _compute_replacement,
    apply_search_replace_blocks,
    find_filename,
    find_similar_lines,
    match_but_for_leading_whitespace,
    parse_search_replace_blocks,
    perfect_replace,
    prep,
    replace_most_similar_chunk,
    replace_part_with_missing_leading_whitespace,
    strip_filename,
    strip_quoted_wrapping,
    try_dotdotdots,
)
from mewbo_tools.integration.aider_edit_blocks import AiderEditBlockTool, _format_tool_input_error
from mewbo_tools.integration.aider_file_tools import (
    AiderListDirTool,
    ReadFileTool,
    _parse_list_request,
    _parse_read_request,
)
from mewbo_tools.integration.file_edit_tool import FileEditTool, _apply_edit, _parse_request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _action_step(tool_input: Any) -> ActionStep:
    return ActionStep(tool_id="test", operation="set", tool_input=tool_input)


def _srblock(path: str, search: str, replace: str) -> str:
    """Build a minimal SEARCH/REPLACE block string."""
    return f"{path}\n```text\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n```\n"


# ===========================================================================
# aider_bridge/edit_blocks.py  -- uncovered lines
# ===========================================================================


class TestPrepFunction:
    def test_adds_trailing_newline_when_missing(self):
        content, lines = prep("hello")
        assert content.endswith("\n")
        assert lines[-1] == "hello\n"

    def test_no_change_when_already_ends_with_newline(self):
        content, lines = prep("hello\n")
        assert content == "hello\n"
        assert len(lines) == 1


class TestPerfectReplace:
    def test_exact_match(self):
        whole = ["a\n", "b\n", "c\n"]
        part = ["b\n"]
        replace = ["X\n"]
        result = perfect_replace(whole, part, replace)
        assert result == "a\nX\nc\n"

    def test_no_match_returns_none(self):
        whole = ["a\n", "b\n"]
        part = ["z\n"]
        replace = ["X\n"]
        assert perfect_replace(whole, part, replace) is None

    def test_replaces_first_occurrence(self):
        whole = ["a\n", "b\n", "a\n"]
        part = ["a\n"]
        replace = ["Z\n"]
        result = perfect_replace(whole, part, replace)
        assert result == "Z\nb\na\n"


class TestReplaceMostSimilarChunk:
    def test_exact_replacement(self):
        result = replace_most_similar_chunk("alpha\nbeta\n", "beta\n", "gamma\n")
        assert result == "alpha\ngamma\n"

    def test_no_match_returns_none(self):
        result = replace_most_similar_chunk("hello\n", "world\n", "there\n")
        assert result is None

    def test_leading_blank_line_skip(self):
        """A leading blank line in the search block does not match — returns None.

        replace_most_similar_chunk strips the leading blank line and tries
        perfect_or_whitespace, but 'def foo():\n' alone does not cover the
        full original block 'def foo():\n    return 1\n', so None is returned.
        """
        whole = "def foo():\n    return 1\n"
        part = "\ndef foo():\n"
        replace = "def bar():\n"
        result = replace_most_similar_chunk(whole, part, replace)
        assert result is None


class TestTryDotdotdots:
    def test_no_dots_returns_none(self):
        result = try_dotdotdots("whole\n", "part\n", "replace\n")
        assert result is None

    def test_unpaired_dots_raises(self):
        with pytest.raises(ValueError, match="Unpaired"):
            try_dotdotdots("a\nb\nc\n", "a\n...\n", "x\n")

    def test_unmatched_dots_raises(self):
        # Different literal dot lines (one indented, one not) trigger "Unmatched"
        with pytest.raises(ValueError, match="Unmatched"):
            try_dotdotdots("a\nb\nc\n", "a\n...\nb\n", "x\n  ...\ny\n")

    def test_successful_dotdotdots_replacement(self):
        whole = "alpha\nbeta\ngamma\ndelta\n"
        part = "alpha\n...\ndelta\n"
        replace = "alpha\n...\nepsilon\n"
        result = try_dotdotdots(whole, part, replace)
        assert result is not None
        assert "epsilon" in result

    def test_empty_part_piece_with_replace_appends(self):
        """Empty part piece with non-empty replace → append to whole."""
        whole = "line1\n"
        part = "line1\n...\n"
        replace = "line1\n...\nappended\n"
        result = try_dotdotdots(whole, part, replace)
        # The dots expansion should append 'appended'
        assert result is not None
        assert "appended" in result


class TestReplacePartWithMissingLeadingWhitespace:
    def test_indented_match(self):
        whole_lines = ["    if True:\n", "        x = 1\n", "    return x\n"]
        part_lines = ["if True:\n", "    x = 1\n"]
        replace_lines = ["if False:\n", "    x = 2\n"]
        result = replace_part_with_missing_leading_whitespace(
            whole_lines, part_lines, replace_lines
        )
        assert result is not None
        assert "    if False:\n" in result

    def test_no_match_returns_none(self):
        whole_lines = ["x = 1\n"]
        part_lines = ["y = 2\n"]
        replace_lines = ["y = 3\n"]
        result = replace_part_with_missing_leading_whitespace(
            whole_lines, part_lines, replace_lines
        )
        assert result is None


class TestMatchButForLeadingWhitespace:
    def test_matches_with_leading_whitespace_difference(self):
        whole = ["    foo()\n", "    bar()\n"]
        part = ["foo()\n", "bar()\n"]
        result = match_but_for_leading_whitespace(whole, part)
        assert result == "    "

    def test_inconsistent_indentation_returns_none(self):
        whole = ["  foo()\n", "    bar()\n"]
        part = ["foo()\n", "bar()\n"]
        result = match_but_for_leading_whitespace(whole, part)
        assert result is None

    def test_different_content_returns_none(self):
        whole = ["foo()\n"]
        part = ["bar()\n"]
        result = match_but_for_leading_whitespace(whole, part)
        assert result is None


class TestStripQuotedWrapping:
    def test_strips_fence_lines(self):
        text = "```\nsome code\n```"
        result = strip_quoted_wrapping(text)
        assert result == "some code\n"

    def test_empty_string_returned_unchanged(self):
        assert strip_quoted_wrapping("") == ""

    def test_no_fence_returned_unchanged(self):
        text = "no fence here"
        result = strip_quoted_wrapping(text)
        assert "no fence here" in result


class TestStripFilename:
    def test_strips_backtick_filename_with_dot(self):
        result = strip_filename("`foo.py`", ("```", "```"))
        assert result == "foo.py"

    def test_returns_none_for_dots(self):
        result = strip_filename("...", ("```", "```"))
        assert result is None

    def test_strips_fence_prefix_with_valid_path(self):
        result = strip_filename("```foo/bar.py", ("```", "```"))
        assert result == "foo/bar.py"

    def test_strips_colon_and_hash(self):
        result = strip_filename("## foo.py:", ("```", "```"))
        assert result == "foo.py"

    def test_plain_filename_returned(self):
        result = strip_filename("myfile.py", ("```", "```"))
        assert result == "myfile.py"


class TestFindFilename:
    def test_finds_filename_in_valid_fnames(self):
        lines = ["foo.py\n", "```text\n"]
        result = find_filename(lines, ("```", "```"), ["foo.py", "bar.py"])
        assert result == "foo.py"

    def test_finds_by_basename(self):
        lines = ["foo.py\n"]
        result = find_filename(lines, ("```", "```"), ["src/foo.py"])
        assert result == "src/foo.py"

    def test_returns_none_when_no_candidates(self):
        lines = []
        result = find_filename(lines, ("```", "```"), ["foo.py"])
        assert result is None

    def test_close_match_is_found(self):
        """find_filename returns the close match 'foo.py' for the near-match 'fooo.py'."""
        lines = ["fooo.py\n"]
        result = find_filename(lines, ("```", "```"), ["foo.py"])
        assert result == "foo.py"

    def test_filename_with_dot_returned_without_valid_fnames(self):
        lines = ["mymodule.py\n"]
        result = find_filename(lines, ("```", "```"), [])
        assert result == "mymodule.py"


class TestFindSimilarLines:
    def test_returns_empty_when_no_match(self):
        result = find_similar_lines("zzz", "aaa\nbbb\n")
        assert result == ""

    def test_returns_match_above_threshold(self):
        content = "alpha\nbeta\ngamma\ndelta\n"
        search = "alpha\nbeta\ngamma\ndelta\n"
        result = find_similar_lines(search, content)
        assert result  # non-empty

    def test_best_match_starts_and_ends_same(self):
        content = "def foo():\n    return 1\ndef bar():\n    return 2\n"
        search = "def foo():\n    return 1\n"
        result = find_similar_lines(search, content)
        assert "def foo" in result


class TestComputeReplacement:
    def test_empty_search_on_new_file(self):
        result = _compute_replacement(
            content="", before_text="", after_text="new content\n", file_exists=False
        )
        assert result == "new content\n"

    def test_empty_search_appends_to_existing(self):
        result = _compute_replacement(
            content="existing\n", before_text="", after_text="appended\n", file_exists=True
        )
        assert result == "existing\nappended\n"

    def test_exact_replacement(self):
        result = _compute_replacement(
            content="hello world\n",
            before_text="hello world\n",
            after_text="goodbye world\n",
            file_exists=True,
        )
        assert result is not None
        assert "goodbye" in result

    def test_no_match_returns_none(self):
        result = _compute_replacement(
            content="hello\n",
            before_text="nonexistent\n",
            after_text="something\n",
            file_exists=True,
        )
        assert result is None


class TestApplySearchReplaceBlocks:
    def test_write_false_does_not_modify_file(self, tmp_path: Path):
        target = tmp_path / "test.py"
        target.write_text("original\n", encoding="utf-8")
        content = _srblock("test.py", "original\n", "changed\n")
        apply_search_replace_blocks(content, root=str(tmp_path), write=False)
        # File must remain unchanged
        assert target.read_text(encoding="utf-8") == "original\n"

    def test_multiple_blocks_same_file(self, tmp_path: Path):
        target = tmp_path / "multi.txt"
        target.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
        content = _srblock("multi.txt", "aaa\n", "AAA\n") + _srblock("multi.txt", "bbb\n", "BBB\n")
        apply_search_replace_blocks(content, root=str(tmp_path), write=True)
        text = target.read_text(encoding="utf-8")
        assert "AAA" in text
        assert "BBB" in text

    def test_creates_parent_directories(self, tmp_path: Path):
        content = _srblock("deep/nested/new.txt", "", "content\n")
        apply_search_replace_blocks(content, root=str(tmp_path), write=True)
        assert (tmp_path / "deep" / "nested" / "new.txt").exists()

    def test_empty_edits_returns_empty_list(self, tmp_path: Path):
        results = apply_search_replace_blocks("no blocks here", root=str(tmp_path), write=False)
        assert results == []


class TestParseSearchReplaceBlocks:
    def test_shell_blocks_are_collected(self):
        content = "```bash\necho hello\n```\n"
        edits, shell_blocks = parse_search_replace_blocks(content)
        assert len(shell_blocks) > 0

    def test_edit_block_parsed_correctly(self):
        content = _srblock("foo.py", "old\n", "new\n")
        edits, shell_blocks = parse_search_replace_blocks(content)
        assert len(edits) == 1
        assert edits[0].path == "foo.py"
        assert edits[0].search == "old\n"
        assert edits[0].replace == "new\n"


# ===========================================================================
# integration/file_edit_tool.py — uncovered lines
# ===========================================================================


class TestFileEditToolParseRequest:
    def test_none_action_step_raises(self):
        with pytest.raises(ToolInputError):
            _parse_request(None)

    def test_non_dict_raises(self):
        with pytest.raises(ToolInputError, match="object"):
            _parse_request(_action_step("just a string"))

    def test_missing_file_path_raises(self):
        with pytest.raises(ToolInputError, match="file_path"):
            _parse_request(_action_step({"old_string": "x", "new_string": "y"}))

    def test_empty_file_path_raises(self):
        with pytest.raises(ToolInputError, match="file_path"):
            _parse_request(_action_step({"file_path": "  ", "old_string": "x", "new_string": "y"}))

    def test_missing_old_string_raises(self):
        with pytest.raises(ToolInputError, match="old_string"):
            _parse_request(_action_step({"file_path": "f.txt", "new_string": "y"}))

    def test_missing_new_string_raises(self):
        with pytest.raises(ToolInputError, match="new_string"):
            _parse_request(_action_step({"file_path": "f.txt", "old_string": "x"}))

    def test_replace_all_defaults_to_false(self):
        req = _parse_request(
            _action_step({"file_path": "f.txt", "old_string": "x", "new_string": "y"})
        )
        assert req.replace_all is False

    def test_replace_all_can_be_set(self):
        req = _parse_request(
            _action_step(
                {
                    "file_path": "f.txt",
                    "old_string": "x",
                    "new_string": "y",
                    "replace_all": True,
                }
            )
        )
        assert req.replace_all is True

    def test_root_defaults_to_cwd(self):
        req = _parse_request(
            _action_step({"file_path": "f.txt", "old_string": "x", "new_string": "y"})
        )
        assert req.root == os.getcwd()


class TestApplyEdit:
    def _req(self, file_path: str, old: str, new: str, replace_all: bool = False):
        from mewbo_tools.integration.file_edit_tool import FileEditRequest

        return FileEditRequest(
            file_path=file_path,
            old_string=old,
            new_string=new,
            replace_all=replace_all,
            root="/tmp",
        )

    def test_empty_old_string_creates_content(self):
        req = self._req("f.txt", "", "hello\n")
        result = _apply_edit("", req)
        assert result == "hello\n"

    def test_empty_old_string_appends_to_existing(self):
        req = self._req("f.txt", "", "more\n")
        result = _apply_edit("existing\n", req)
        assert result == "existing\nmore\n"

    def test_old_string_not_found_raises(self):
        req = self._req("f.txt", "missing", "x")
        with pytest.raises(ToolInputError, match="not found"):
            _apply_edit("other content\n", req)

    def test_multiple_occurrences_without_replace_all_raises(self):
        req = self._req("f.txt", "x", "y", replace_all=False)
        with pytest.raises(ToolInputError, match="occurrences"):
            _apply_edit("x x x\n", req)

    def test_replace_all_replaces_every_occurrence(self):
        req = self._req("f.txt", "x", "z", replace_all=True)
        result = _apply_edit("x y x y x\n", req)
        assert result == "z y z y z\n"

    def test_single_replace_without_replace_all(self):
        req = self._req("f.txt", "hello", "world")
        result = _apply_edit("hello there\n", req)
        assert result == "world there\n"


class TestFileEditToolSetState:
    def test_set_state_writes_file_and_returns_diff(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "edit_me.txt"
        target.write_text("old line\n", encoding="utf-8")
        tool = FileEditTool()
        step = _action_step(
            {
                "file_path": str(target),
                "old_string": "old line",
                "new_string": "new line",
                "root": str(tmp_path),
            }
        )
        result = tool.set_state(step)
        # Content must be {"kind":"diff",...}
        assert isinstance(result.content, dict)
        assert result.content["kind"] == "diff"
        assert "new line" in result.content["text"]

    def test_set_state_no_diff_fallback_message(self, tmp_path: Path, monkeypatch):
        """When old == new, no diff is produced and a fallback message is returned."""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "same.txt"
        target.write_text("content\n", encoding="utf-8")
        tool = FileEditTool()
        step = _action_step(
            {
                "file_path": str(target),
                "old_string": "content",
                "new_string": "content",
                "root": str(tmp_path),
            }
        )
        result = tool.set_state(step)
        # No visible diff → fallback string message
        assert "Applied edit" in result.content or "no visible diff" in result.content

    def test_get_state_validates_without_writing(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "check_me.txt"
        target.write_text("hello world\n", encoding="utf-8")
        tool = FileEditTool()
        step = _action_step(
            {
                "file_path": str(target),
                "old_string": "hello",
                "new_string": "goodbye",
                "root": str(tmp_path),
            }
        )
        result = tool.get_state(step)
        assert "Validated" in result.content
        # File must not be modified
        assert target.read_text(encoding="utf-8") == "hello world\n"

    def test_get_state_on_nonexistent_file_validates_empty(self, tmp_path: Path, monkeypatch):
        """get_state on a nonexistent file with empty old_string succeeds."""
        monkeypatch.chdir(tmp_path)
        tool = FileEditTool()
        step = _action_step(
            {
                "file_path": str(tmp_path / "new_file.txt"),
                "old_string": "",
                "new_string": "initial content\n",
                "root": str(tmp_path),
            }
        )
        result = tool.get_state(step)
        assert "Validated" in result.content


# ===========================================================================
# integration/aider_file_tools.py — uncovered lines
# ===========================================================================


class TestParseReadRequest:
    def test_none_action_step_raises(self):
        with pytest.raises(ValueError, match="required"):
            _parse_read_request(None)

    def test_string_input_parses_path(self):
        req = _parse_read_request(_action_step("/tmp/foo.py"))
        assert req.path == "/tmp/foo.py"
        assert req.offset == 0
        assert req.limit is None

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Path is required"):
            _parse_read_request(_action_step("   "))

    def test_dict_input_with_all_fields(self):
        req = _parse_read_request(
            _action_step(
                {
                    "path": "foo.py",
                    "root": "/tmp",
                    "offset": "5",
                    "limit": "100",
                    "max_bytes": "1000",
                }
            )
        )
        assert req.offset == 5
        assert req.limit == 100
        assert req.max_bytes == 1000

    def test_dict_invalid_offset_defaults_to_zero(self):
        req = _parse_read_request(_action_step({"path": "foo.py", "offset": "not-a-number"}))
        assert req.offset == 0

    def test_dict_invalid_limit_defaults_to_none(self):
        req = _parse_read_request(_action_step({"path": "foo.py", "limit": "bad"}))
        assert req.limit is None

    def test_non_string_non_dict_raises(self):
        # Use a namespace to bypass ActionStep validation for testing the parser directly
        import types as _types

        step = _types.SimpleNamespace(tool_input=42)
        with pytest.raises(ValueError, match="string path or an object"):
            _parse_read_request(step)

    def test_dict_missing_path_raises(self):
        with pytest.raises(ValueError, match="path is required"):
            _parse_read_request(_action_step({"other": "value"}))


class TestParseListRequest:
    def test_none_action_step_raises(self):
        with pytest.raises(ValueError, match="required"):
            _parse_list_request(None)

    def test_string_input(self):
        req = _parse_list_request(_action_step("/tmp"))
        assert req.path == "/tmp"

    def test_empty_string_defaults_to_dot(self):
        req = _parse_list_request(_action_step(""))
        assert req.path == "."

    def test_dict_with_max_entries(self):
        req = _parse_list_request(_action_step({"path": "/tmp", "max_entries": "10"}))
        assert req.max_entries == 10

    def test_dict_invalid_max_entries_defaults_to_none(self):
        req = _parse_list_request(_action_step({"path": "/tmp", "max_entries": "bad"}))
        assert req.max_entries is None

    def test_non_string_non_dict_raises(self):
        import types as _types

        step = _types.SimpleNamespace(tool_input=42)
        with pytest.raises(ValueError, match="string path or an object"):
            _parse_list_request(step)


class TestReadFileTool:
    def test_reads_existing_file(self, tmp_path: Path):
        target = tmp_path / "data.txt"
        target.write_text("line1\nline2\nline3\n", encoding="utf-8")
        tool = ReadFileTool()
        step = _action_step({"path": "data.txt", "root": str(tmp_path)})
        result = tool.get_state(step)
        assert isinstance(result.content, dict)
        assert result.content["kind"] == "file"
        assert "line1" in result.content["text"]
        assert result.content["total_lines"] == 3

    def test_returns_error_for_missing_file(self, tmp_path: Path):
        tool = ReadFileTool()
        step = _action_step({"path": "nonexistent.txt", "root": str(tmp_path)})
        result = tool.get_state(step)
        assert "unable to read" in result.content or isinstance(result.content, str)

    def test_offset_slices_lines(self, tmp_path: Path):
        target = tmp_path / "big.txt"
        target.write_text("".join(f"line{i}\n" for i in range(10)), encoding="utf-8")
        tool = ReadFileTool()
        step = _action_step({"path": "big.txt", "root": str(tmp_path), "offset": 5})
        result = tool.get_state(step)
        assert isinstance(result.content, dict)
        # Line numbers should start at 6 (offset=5, 1-based)
        assert "6\t" in result.content["text"]
        assert "1\t" not in result.content["text"]

    def test_limit_truncates_lines(self, tmp_path: Path):
        target = tmp_path / "trunc.txt"
        target.write_text("".join(f"line{i}\n" for i in range(100)), encoding="utf-8")
        tool = ReadFileTool()
        step = _action_step({"path": "trunc.txt", "root": str(tmp_path), "limit": 5})
        result = tool.get_state(step)
        assert isinstance(result.content, dict)
        assert "truncated" in result.content["text"]

    def test_max_bytes_truncates(self, tmp_path: Path):
        target = tmp_path / "bytes.txt"
        target.write_text("A" * 1000 + "\n", encoding="utf-8")
        tool = ReadFileTool()
        step = _action_step({"path": "bytes.txt", "root": str(tmp_path), "max_bytes": 50})
        result = tool.get_state(step)
        assert isinstance(result.content, dict)
        assert "truncated" in result.content["text"]

    def test_path_traversal_blocked(self, tmp_path: Path):
        tool = ReadFileTool()
        step = _action_step({"path": "../../../etc/passwd", "root": str(tmp_path)})
        result = tool.get_state(step)
        # Should return an error string, not file contents
        assert isinstance(result.content, str)
        assert "resolves outside" in result.content or "not in" in result.content.lower()


class TestAiderListDirTool:
    def test_lists_directory_entries(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.txt").write_text("b", encoding="utf-8")
        tool = AiderListDirTool()
        step = _action_step({"path": ".", "root": str(tmp_path)})
        result = tool.get_state(step)
        assert isinstance(result.content, dict)
        assert result.content["kind"] == "dir"
        entries = result.content["entries"]
        names = [Path(e).name for e in entries]
        assert "a.txt" in names
        assert "b.txt" in names

    def test_max_entries_limits_output(self, tmp_path: Path):
        for i in range(10):
            (tmp_path / f"file{i}.txt").write_text("x", encoding="utf-8")
        tool = AiderListDirTool()
        step = _action_step({"path": ".", "root": str(tmp_path), "max_entries": 3})
        result = tool.get_state(step)
        assert isinstance(result.content, dict)
        assert len(result.content["entries"]) <= 3

    def test_path_traversal_blocked(self, tmp_path: Path):
        tool = AiderListDirTool()
        step = _action_step({"path": "../../etc", "root": str(tmp_path)})
        result = tool.get_state(step)
        # Returns string error message, not a dict
        assert isinstance(result.content, str)


class TestAiderEditBlockTool:
    def test_set_state_applies_and_returns_diff_dict(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "source.py"
        target.write_text("def foo():\n    return 1\n", encoding="utf-8")
        tool = AiderEditBlockTool()
        content = _srblock("source.py", "return 1\n", "return 2\n")
        step = _action_step({"content": content, "root": str(tmp_path)})
        result = tool.set_state(step)
        payload = result.content
        assert isinstance(payload, dict)
        assert payload["kind"] == "diff"
        assert "return 2" in payload["text"]

    def test_set_state_raises_on_no_blocks(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tool = AiderEditBlockTool()
        step = _action_step({"content": "no blocks here", "root": str(tmp_path)})
        with pytest.raises(ToolInputError, match="SEARCH/REPLACE"):
            tool.set_state(step)

    def test_get_state_validates_without_writing(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "validate_me.py"
        target.write_text("original content\n", encoding="utf-8")
        tool = AiderEditBlockTool()
        content = _srblock("validate_me.py", "original content\n", "new content\n")
        step = _action_step({"content": content, "root": str(tmp_path)})
        result = tool.get_state(step)
        assert "Validated" in result.content or "block" in result.content.lower()
        # File must be unchanged
        assert target.read_text(encoding="utf-8") == "original content\n"

    def test_set_state_string_input_uses_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "cwd_target.txt"
        target.write_text("hello\n", encoding="utf-8")
        tool = AiderEditBlockTool()
        content = _srblock("cwd_target.txt", "hello\n", "world\n")
        step = _action_step(content)  # string input
        result = tool.set_state(step)
        payload = result.content
        assert isinstance(payload, dict)
        assert payload["kind"] == "diff"

    def test_parse_request_none_raises(self):
        from mewbo_tools.integration.aider_edit_blocks import _parse_request

        with pytest.raises(EditBlockApplyError, match="required"):
            _parse_request(None)

    def test_parse_request_non_str_non_dict_raises(self):
        import types as _types

        from mewbo_tools.integration.aider_edit_blocks import _parse_request

        step = _types.SimpleNamespace(tool_input=12345)
        with pytest.raises(EditBlockApplyError, match="string or object"):
            _parse_request(step)

    def test_parse_request_dict_missing_content_raises(self):
        from mewbo_tools.integration.aider_edit_blocks import _parse_request

        with pytest.raises(EditBlockApplyError, match="content is required"):
            _parse_request(_action_step({"content": "", "root": "/tmp"}))

    def test_parse_request_dict_bad_root_raises(self):
        """root explicitly set to whitespace-only string triggers the guard."""
        from mewbo_tools.integration.aider_edit_blocks import _parse_request

        # Whitespace-only root: truthy (not caught by `or os.getcwd()`),
        # but root.strip() is empty → raises EditBlockApplyError
        step = _action_step({"content": "some content\n", "root": "   "})
        with pytest.raises(EditBlockApplyError, match="Root path"):
            _parse_request(step)

    def test_parse_request_dict_bad_files_raises(self):
        from mewbo_tools.integration.aider_edit_blocks import _parse_request

        with pytest.raises(EditBlockApplyError, match="list of strings"):
            _parse_request(
                _action_step({"content": "some content\n", "root": "/tmp", "files": "foo.py"})
            )

    def test_format_tool_input_error_with_message(self):
        result = _format_tool_input_error("Something went wrong")
        assert "Something went wrong" in result
        assert "SEARCH" in result

    def test_format_tool_input_error_empty_returns_guidance(self):
        result = _format_tool_input_error("")
        assert "SEARCH" in result
        assert "REPLACE" in result
