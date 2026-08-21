"""Tests for local file tools (read_file, list_dir)."""

from __future__ import annotations

import json

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.config import reset_config, set_config_override
from mewbo_core.tooling.tool_registry import _default_registry
from mewbo_tools.integration.aider_file_tools import AiderListDirTool, ReadFileTool


@pytest.fixture(autouse=True)
def _unpinned_path_scope():
    """Pin the path-scope axis OFF: these exercise read/list SEMANTICS.

    What this module is about is line offsets, byte truncation, ``max_entries``
    and the tool envelopes — against an explicit caller-supplied ``root``, which
    every case here spells as a bare ``tmp_path``. ``path_scope_to_active_project``
    ships ON and refuses a ``root`` argument that widens beyond the session's
    scope, so leaving it at its default would fail all of these for a reason none
    of them is testing. That axis has its own coverage in
    ``tests/test_path_guard_scope_parity.py``.

    It also keeps ``test_aider_read_file_blocks_escape`` HONEST. With the root
    dropped, ``../oops.txt`` is refused because no root was admitted at all —
    the assertion passes without the traversal check ever running.

    Resets afterwards: ``set_config_override`` is process-global and nothing in
    ``conftest.py`` clears it, so an unreset override leaks into every later
    module.
    """
    set_config_override({"agent": {"path_scope_to_active_project": False}})
    yield
    reset_config()


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


def test_read_file_names_the_cause_of_a_failed_read(tmp_path):
    """A failed read must say WHICH failure it was, not just that it failed.

    The three causes warrant different responses — correct the path, read a
    file inside the directory, or give up — so collapsing them into one
    message leaves the caller unable to tell its own bad guess from a
    repository it genuinely cannot read. Measured consequence: an indexing run
    asked for a directory and for a README that lives one level down, got the
    same opaque string twice, read it as fatal and stopped with the repository
    cloned and its graph fully built.

    Each case asserts the DISTINCTION, not merely that some string came back:
    a test accepting any message passes just as well against the single
    collapsed one this exists to prevent.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "real.txt").write_text("data\n", encoding="utf-8")

    tool = ReadFileTool()

    def read(path: str) -> str:
        result = tool.get_state(
            ActionStep(
                tool_id="read_file",
                operation="get",
                tool_input={"path": path, "root": str(tmp_path)},
            )
        )
        assert isinstance(result.content, str), "a failed read returns a message, not a payload"
        return result.content

    directory = read("src")
    assert "is a directory" in directory
    assert "not found" not in directory

    missing = read("README.md")
    assert "not found" in missing
    assert "is a directory" not in missing

    # The positive case is what keeps the two negatives non-vacuous: the same
    # tool on the same root still returns a payload rather than a message.
    ok = tool.get_state(
        ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": "real.txt", "root": str(tmp_path)},
        )
    )
    assert isinstance(ok.content, dict)
    assert ok.content.get("kind") == "file"


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


def test_read_file_tool_spec_declares_a_cap_wide_enough_for_a_realistic_file(tmp_path):
    """``read_file``'s registered ``max_result_chars`` must not undercut its own line cap.

    Left undeclared, a ``ToolSpec`` falls back to the registry's 2000-CHARACTER
    class default (`tool_registry.py`'s ``ToolSpec.max_result_chars``) — two
    orders of magnitude tighter than this tool's own advertised 2000-LINE
    window, so a multi-hundred-line source file (well under the line cap)
    would get silently re-truncated by the LOOP after the tool already
    returned it whole. Pin the relationship directly: build a realistic
    500-line file, read it through the real tool, serialize the payload the
    same way ``ToolUseLoop`` does (``json.dumps`` of the dict content), and
    assert the serialized size fits inside the REGISTERED spec's declared cap
    — not a hardcoded number — so a regression back to the 2000-char default
    fails this test rather than silently reappearing in the loop.
    """
    target = tmp_path / "module.py"
    # ~40 chars/line before line-numbering — representative of real source,
    # not a pathological one-word-per-line fixture.
    target.write_text(
        "\n".join(f"def handler_{i}(request, response):  # noqa: PLR0913" for i in range(500)),
        encoding="utf-8",
    )

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={"path": "module.py", "root": str(tmp_path)},
    )
    result = tool.get_state(step)
    payload = result.content
    assert payload.get("total_lines") == 500
    assert "truncated" not in payload.get("text", "")

    serialized = json.dumps(payload, ensure_ascii=False, default=str)

    spec = _default_registry().get_spec("read_file")
    assert spec is not None
    assert spec.max_result_chars > 0, "read_file must declare a nonzero cap"
    assert len(serialized) <= spec.max_result_chars, (
        "the registered max_result_chars is tighter than a realistic file's "
        "serialized payload — the loop would re-truncate a whole read"
    )
