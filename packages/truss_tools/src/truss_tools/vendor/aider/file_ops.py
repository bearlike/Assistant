"""File utilities adapted from Aider commands."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator


def expand_subdir(file_path: Path) -> Iterator[Path]:
    """Yield files under file_path (recursive if directory)."""
    if file_path.is_file():
        yield file_path
        return

    if file_path.is_dir():
        for file in file_path.rglob("*"):
            if file.is_file():
                yield file
