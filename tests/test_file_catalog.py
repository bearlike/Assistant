#!/usr/bin/env python3
"""Tests for FileCatalog — git-index file listing + scoping gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mewbo_tools.integration.file_catalog import FileCatalog


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def test_none_cwd_is_inert() -> None:
    cat = FileCatalog(None)
    assert cat.is_git_repo() is False
    assert cat.list_files() == []
    assert cat.contains("anything") is False


def test_git_repo_lists_tracked_and_new_not_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("b\n")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("secret\n")

    cat = FileCatalog(str(tmp_path))
    files = cat.list_files()
    assert "a.py" in files
    assert "sub/b.py" in files
    assert ".gitignore" in files
    assert "ignored.txt" not in files  # excluded by --exclude-standard


def test_contains_gate_git(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / ".gitignore").write_text("secret.txt\n")
    (tmp_path / "secret.txt").write_text("nope\n")
    cat = FileCatalog(str(tmp_path))
    assert cat.contains("a.py") is True
    assert cat.contains("secret.txt") is False  # gitignored
    assert cat.contains("does_not_exist.py") is False


def test_non_git_fallback_walk_and_contains(tmp_path: Path) -> None:
    # No git init → bounded walk + cwd-confined contains.
    (tmp_path / "main.py").write_text("x\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("junk\n")
    cat = FileCatalog(str(tmp_path))
    assert cat.is_git_repo() is False
    files = cat.list_files()
    assert "main.py" in files
    assert not any("node_modules" in f for f in files)  # skipped
    assert cat.contains("main.py") is True
    assert cat.contains("../escape") is False


def test_list_files_capped(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x\n")
    cat = FileCatalog(str(tmp_path))
    assert len(cat.list_files(limit=3)) == 3
