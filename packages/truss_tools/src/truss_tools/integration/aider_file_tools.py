#!/usr/bin/env python3
"""Local file helpers adapted from Aider."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from truss_core.classes import AbstractTool, ActionStep
from truss_core.common import MockSpeaker, get_mock_speaker

from truss_tools.core import resolve_safe_path
from truss_tools.vendor.aider.file_ops import expand_subdir
from truss_tools.vendor.aider.io import InputOutput


@dataclass(frozen=True)
class ReadFileRequest:
    path: str
    root: str
    offset: int = 0  # 0-based start line
    limit: int | None = None  # max lines to read (None → default 2000)
    max_bytes: int | None = None  # byte truncation (backwards compat)


@dataclass(frozen=True)
class ListDirRequest:
    path: str
    root: str
    max_entries: int | None


def _parse_read_request(action_step: ActionStep | None) -> ReadFileRequest:
    if action_step is None:
        raise ValueError("Action step is required.")
    argument = action_step.tool_input
    if isinstance(argument, str):
        path = argument.strip()
        if not path:
            raise ValueError("Path is required.")
        return ReadFileRequest(path=path, root=os.getcwd(), offset=0, limit=None, max_bytes=None)
    if isinstance(argument, dict):
        path = str(argument.get("path", "")).strip()
        if not path:
            raise ValueError("path is required.")
        root = str(argument.get("root") or os.getcwd())
        offset = argument.get("offset", 0)
        if offset is not None:
            try:
                offset = int(offset)
            except (TypeError, ValueError):
                offset = 0
        limit = argument.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None
        max_bytes = argument.get("max_bytes")
        if max_bytes is not None:
            try:
                max_bytes = int(max_bytes)
            except (TypeError, ValueError):
                max_bytes = None
        return ReadFileRequest(
            path=path,
            root=root,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
        )
    raise ValueError("Tool input must be a string path or an object payload.")


def _parse_list_request(action_step: ActionStep | None) -> ListDirRequest:
    if action_step is None:
        raise ValueError("Action step is required.")
    argument = action_step.tool_input
    if isinstance(argument, str):
        path = argument.strip() or "."
        return ListDirRequest(path=path, root=os.getcwd(), max_entries=None)
    if isinstance(argument, dict):
        path = str(argument.get("path") or ".").strip()
        root = str(argument.get("root") or os.getcwd())
        max_entries = argument.get("max_entries")
        if max_entries is not None:
            try:
                max_entries = int(max_entries)
            except (TypeError, ValueError):
                max_entries = None
        return ListDirRequest(path=path, root=root, max_entries=max_entries)
    raise ValueError("Tool input must be a string path or an object payload.")


class ReadFileTool(AbstractTool):
    """Read a local file."""

    def __init__(self) -> None:
        """Initialize the read-file tool."""
        super().__init__(
            name="Read File",
            description="Read local files.",
            use_llm=False,
        )
        self._io = InputOutput(pretty=False, fancy_input=False)

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Return the contents of a file or a readable error message."""
        try:
            request = _parse_read_request(action_step)
            target = resolve_safe_path(request.path, root=request.root)
        except ValueError as exc:
            MockSpeaker = get_mock_speaker()
            return MockSpeaker(content=str(exc))
        text = self._io.read_text(target, silent=True)
        if text is None:
            message = f"{request.path}: unable to read"
            MockSpeaker = get_mock_speaker()
            return MockSpeaker(content=message)

        DEFAULT_LINE_LIMIT = 2000

        # Line-based windowing with line numbers
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        # Apply offset
        if request.offset > 0:
            lines = lines[request.offset :]

        # Apply limit (default 2000)
        effective_limit = request.limit if request.limit is not None else DEFAULT_LINE_LIMIT
        truncated = len(lines) > effective_limit
        if effective_limit > 0:
            lines = lines[:effective_limit]

        # Add line numbers (1-based, tab-separated, like cat -n)
        numbered_lines = []
        for i, line in enumerate(lines):
            line_num = request.offset + i + 1
            # Strip trailing newline before adding number, then re-add
            stripped = line.rstrip("\n").rstrip("\r")
            numbered_lines.append(f"{line_num}\t{stripped}")
        text = "\n".join(numbered_lines)

        if truncated:
            text += "\n... (truncated — use offset/limit to read more)"

        # Apply max_bytes truncation after line windowing for backwards compat
        if request.max_bytes is not None and request.max_bytes > 0:
            if len(text) > request.max_bytes:
                text = text[: request.max_bytes] + "\n... (truncated)"

        payload: dict[str, object] = {
            "kind": "file",
            "path": request.path,
            "text": text,
            "total_lines": total_lines,
        }
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=payload)


class AiderListDirTool(AbstractTool):
    """List files under a local directory using Aider helpers."""

    def __init__(self) -> None:
        """Initialize the Aider list-directory tool."""
        super().__init__(
            name="Aider List Directory",
            description="List files under a directory using Aider helpers.",
            use_llm=False,
        )

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Return directory entries or a readable error message."""
        try:
            request = _parse_list_request(action_step)
            target = resolve_safe_path(request.path, root=request.root)
        except ValueError as exc:
            MockSpeaker = get_mock_speaker()
            return MockSpeaker(content=str(exc))
        root_path = Path(request.root).resolve()
        entries: list[str] = []
        for file_path in expand_subdir(target):
            try:
                rel_path = file_path.resolve().relative_to(root_path)
            except ValueError:
                continue
            entries.append(str(rel_path))
            if request.max_entries and len(entries) >= request.max_entries:
                break
        payload: dict[str, object] = {
            "kind": "dir",
            "path": request.path,
            "entries": entries,
        }
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=payload)


# Backward compatibility alias
AiderReadFileTool = ReadFileTool

__all__ = ["ReadFileTool", "AiderReadFileTool", "AiderListDirTool"]
