#!/usr/bin/env python3
"""Aider-style search/replace block application tool."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from meeseeks_core.classes import AbstractTool, ActionStep
from meeseeks_core.common import MockSpeaker, get_mock_speaker
from meeseeks_core.errors import ToolInputError

from meeseeks_tools.aider_bridge import (
    EditBlockApplyError,
    EditBlockParseError,
    apply_search_replace_blocks,
    parse_search_replace_blocks,
)
from meeseeks_tools.core import resolve_safe_path
from meeseeks_tools.integration.edit_common import (
    build_unified_diff,
    format_diff_result,
    read_file_contents,
)


@dataclass(frozen=True)
class EditBlockRequest:
    """Parsed request payload for edit-block application."""

    content: str
    root: str
    files: list[str] | None


class AiderEditBlockTool(AbstractTool):
    """Apply Aider SEARCH/REPLACE blocks to local files."""

    def __init__(self) -> None:
        """Initialize the edit block tool."""
        super().__init__(
            name="Aider Edit Blocks",
            description="Apply Aider-style SEARCH/REPLACE blocks to files.",
            use_llm=False,
        )

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Apply search/replace blocks to files."""
        try:
            request = _parse_request(action_step)
            target_paths = _collect_target_paths(request)
            before_map = read_file_contents(target_paths)
            results = apply_search_replace_blocks(
                request.content,
                root=request.root,
                valid_fnames=request.files,
                write=True,
            )
            if not results:
                raise ToolInputError(_format_tool_input_error("No SEARCH/REPLACE blocks found."))
            diff_text = build_unified_diff(before_map, target_paths)
            if diff_text:
                message: object = format_diff_result(
                    diff_text, "Aider Edit Blocks", list(target_paths.keys())
                )
            else:
                message = _format_summary(results, dry_run=False)
            MockSpeaker = get_mock_speaker()
            return MockSpeaker(content=message)
        except EditBlockApplyError as exc:
            raise ToolInputError(_format_tool_input_error(str(exc))) from exc

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Validate search/replace blocks without writing changes."""
        try:
            request = _parse_request(action_step)
            results = apply_search_replace_blocks(
                request.content,
                root=request.root,
                valid_fnames=request.files,
                write=False,
            )
            if not results:
                raise ToolInputError(_format_tool_input_error("No SEARCH/REPLACE blocks found."))
            message = _format_summary(results, dry_run=True)
            MockSpeaker = get_mock_speaker()
            return MockSpeaker(content=message)
        except EditBlockApplyError as exc:
            raise ToolInputError(_format_tool_input_error(str(exc))) from exc


def _parse_request(action_step: ActionStep | None) -> EditBlockRequest:
    if action_step is None:
        raise EditBlockApplyError("Action step is required for edit block operations.")

    argument = action_step.tool_input
    if isinstance(argument, str):
        return EditBlockRequest(content=argument, root=os.getcwd(), files=None)

    if isinstance(argument, dict):
        content = argument.get("content") or argument.get("blocks")
        if not isinstance(content, str) or not content.strip():
            raise EditBlockApplyError("Edit block content is required.")
        root = argument.get("root") or os.getcwd()
        if not isinstance(root, str) or not root.strip():
            raise EditBlockApplyError("Root path must be a non-empty string.")
        files = argument.get("files")
        if files is not None:
            if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
                raise EditBlockApplyError("files must be a list of strings.")
        return EditBlockRequest(content=content, root=root, files=files)

    raise EditBlockApplyError("Tool input must be a string or object payload.")


def _collect_target_paths(request: EditBlockRequest) -> dict[str, Path]:
    try:
        edits, shell_blocks = parse_search_replace_blocks(
            request.content,
            valid_fnames=request.files,
        )
    except EditBlockParseError as exc:
        raise EditBlockApplyError(str(exc)) from exc
    if shell_blocks:
        raise EditBlockApplyError("Shell command blocks are not supported by this tool.")
    root_path = Path(request.root).resolve()
    targets: dict[str, Path] = {}
    for edit in edits:
        try:
            targets[edit.path] = resolve_safe_path(edit.path, root=str(root_path))
        except ValueError as exc:
            raise EditBlockApplyError(str(exc)) from exc
    return targets


def _format_summary(results, *, dry_run: bool) -> str:
    if not results:
        return "No SEARCH/REPLACE blocks found."

    created = [result.path for result in results if result.created]
    applied = [result.path for result in results]

    mode = "Validated" if dry_run else "Applied"
    summary = f"{mode} {len(applied)} SEARCH/REPLACE block(s) across {len(set(applied))} file(s)."
    if created:
        summary += f" Created {len(created)} file(s)."
    return summary


def _format_tool_input_error(message: str) -> str:
    guidance = (
        "Expected format:\n"
        "<path>\n"
        "```text\n"
        "<<<<<<< SEARCH\n"
        "<exact text to match>\n"
        "=======\n"
        "<replacement text>\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        "Rules: filename line immediately before the fence; SEARCH must match exactly; "
        "use a line with `...` in both SEARCH and REPLACE to skip unchanged sections; "
        "do not use shell code blocks."
    )
    if not message:
        return guidance
    return f"{message}\n\n{guidance}"


__all__ = ["AiderEditBlockTool", "EditBlockRequest"]
