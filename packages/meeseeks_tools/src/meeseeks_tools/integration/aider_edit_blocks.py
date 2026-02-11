#!/usr/bin/env python3
"""Aider-style search/replace block application tool."""

from __future__ import annotations

import difflib
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
            before_map = _read_targets(target_paths)
            results = apply_search_replace_blocks(
                request.content,
                root=request.root,
                valid_fnames=request.files,
                write=True,
            )
            if not results:
                raise ToolInputError(_format_tool_input_error("No SEARCH/REPLACE blocks found."))
            diff_text = _build_diff(before_map, target_paths)
            if diff_text:
                message: object = {
                    "kind": "diff",
                    "title": "Aider Edit Blocks",
                    "text": diff_text,
                }
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
        targets[edit.path] = _resolve_path(root_path, edit.path)
    return targets


def _resolve_path(root_path: Path, rel_path: str) -> Path:
    candidate = Path(rel_path)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise EditBlockApplyError(
            f"Edit path '{rel_path}' resolves outside the project root."
        ) from exc
    return resolved


def _read_targets(targets: dict[str, Path]) -> dict[str, str]:
    before: dict[str, str] = {}
    for rel_path, abs_path in targets.items():
        if abs_path.exists():
            before[rel_path] = abs_path.read_text(encoding="utf-8")
        else:
            before[rel_path] = ""
    return before


def _build_diff(before_map: dict[str, str], targets: dict[str, Path]) -> str:
    chunks: list[str] = []
    for rel_path, abs_path in targets.items():
        before = before_map.get(rel_path, "")
        after = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=rel_path,
            tofile=rel_path,
        )
        diff_text = "".join(diff)
        if diff_text:
            chunks.append(diff_text)
    return "".join(chunks)


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
