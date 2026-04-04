"""Per-file exact string replacement tool.

Provides a simple ``file_path`` / ``old_string`` / ``new_string`` interface
for LLMs that work better with structured per-file edits rather than the
Aider-style SEARCH/REPLACE block format.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from meeseeks_core.classes import AbstractTool, ActionStep
from meeseeks_core.common import get_mock_speaker
from meeseeks_core.errors import ToolInputError

from meeseeks_tools.integration.edit_common import (
    build_unified_diff,
    format_diff_result,
    read_file_contents,
    resolve_and_validate_path,
)


@dataclass(frozen=True)
class FileEditRequest:
    """Parsed request payload for structured file edits."""

    file_path: str
    old_string: str
    new_string: str
    replace_all: bool
    root: str


class FileEditTool(AbstractTool):
    """Apply exact string replacement to a single file."""

    def __init__(self) -> None:
        """Initialize the file edit tool."""
        super().__init__(
            name="File Edit",
            description="Apply exact string replacement to a file.",
            use_llm=False,
        )

    def set_state(self, action_step: ActionStep | None = None):
        """Apply the replacement and return a diff."""
        request = _parse_request(action_step)
        abs_path = resolve_and_validate_path(request.file_path, request.root)
        targets = {request.file_path: abs_path}
        before_map = read_file_contents(targets)
        new_content = _apply_edit(before_map[request.file_path], request)

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(new_content, encoding="utf-8")

        diff_text = build_unified_diff(before_map, targets)
        MockSpeaker = get_mock_speaker()
        if diff_text:
            return MockSpeaker(
                content=format_diff_result(diff_text, "File Edit", [request.file_path])
            )
        return MockSpeaker(content=f"Applied edit to {request.file_path} (no visible diff).")

    def get_state(self, action_step: ActionStep | None = None):
        """Validate the edit without writing to disk."""
        request = _parse_request(action_step)
        abs_path = resolve_and_validate_path(request.file_path, request.root)
        content = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
        _apply_edit(content, request)  # validates; raises on error
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=f"Validated edit for {request.file_path}.")


# ── Private helpers ──────────────────────────────────────────────────


def _parse_request(action_step: ActionStep | None) -> FileEditRequest:
    if action_step is None:
        raise ToolInputError("Action step is required.")
    argument = action_step.tool_input
    if not isinstance(argument, dict):
        raise ToolInputError(
            "Tool input must be an object with file_path, old_string, and new_string."
        )
    file_path = argument.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise ToolInputError("file_path is required and must be a non-empty string.")
    old_string = argument.get("old_string")
    if not isinstance(old_string, str):
        raise ToolInputError("old_string is required and must be a string.")
    new_string = argument.get("new_string")
    if not isinstance(new_string, str):
        raise ToolInputError("new_string is required and must be a string.")
    replace_all = bool(argument.get("replace_all", False))
    root = argument.get("root") or os.getcwd()
    return FileEditRequest(
        file_path=file_path.strip(),
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        root=str(root),
    )


def _apply_edit(content: str, request: FileEditRequest) -> str:
    """Compute edited content.  Raises :class:`ToolInputError` on failure."""
    # Create / append when old_string is empty
    if not request.old_string:
        return content + request.new_string if content else request.new_string

    count = content.count(request.old_string)
    if count == 0:
        raise ToolInputError(
            f"old_string not found in {request.file_path}. "
            "Ensure the string matches exactly (including whitespace and newlines)."
        )
    if count > 1 and not request.replace_all:
        raise ToolInputError(
            f"Found {count} occurrences of old_string in {request.file_path}. "
            "Set replace_all=true to replace all, or provide a more specific match."
        )

    if request.replace_all:
        return content.replace(request.old_string, request.new_string)
    return content.replace(request.old_string, request.new_string, 1)


__all__ = ["FileEditTool", "FileEditRequest"]
