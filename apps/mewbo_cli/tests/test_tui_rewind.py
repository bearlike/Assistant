#!/usr/bin/env python3
"""Tests for the per-turn workspace checkpointer.

Drives a real temp git repo (filesystem is the only boundary) so the git
plumbing — non-destructive capture, safe reversible restore, graceful
degradation outside a repo — is exercised end-to-end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mewbo_cli.tui.session.rewind import RewindCheckpointer


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.test")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "file.txt").write_text("original\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_outside_git_repo_degrades(tmp_path: Path) -> None:
    cp = RewindCheckpointer(cwd=str(tmp_path))
    assert cp.is_git_repo() is False
    assert cp.checkpoint(label="x") is None
    assert cp.checkpoints() == []
    # restore of a fabricated checkpoint is a no-op False, never raises
    from mewbo_cli.tui.session.rewind import Checkpoint

    assert cp.restore(Checkpoint(0, "deadbeef", "x")) is False


def test_checkpoint_on_clean_tree_uses_head(repo: Path) -> None:
    cp = RewindCheckpointer(cwd=str(repo))
    checkpoint = cp.checkpoint(label="turn1", after_ts="2026-01-01T00:00:00Z")
    assert checkpoint is not None
    assert checkpoint.commit_ref  # a ref was captured (HEAD on a clean tree)
    assert checkpoint.after_ts == "2026-01-01T00:00:00Z"
    assert cp.latest() is checkpoint


def test_capture_is_non_destructive(repo: Path) -> None:
    # Dirty the tree; a checkpoint must NOT touch the working file.
    (repo / "file.txt").write_text("dirty\n")
    cp = RewindCheckpointer(cwd=str(repo))
    cp.checkpoint(label="turn")
    assert (repo / "file.txt").read_text() == "dirty\n"


def test_restore_reverts_tracked_and_removes_untracked(repo: Path) -> None:
    cp = RewindCheckpointer(cwd=str(repo))
    # Checkpoint the clean original state.
    checkpoint = cp.checkpoint(label="before")
    assert checkpoint is not None
    # A "turn" edits a tracked file AND creates a brand-new untracked file.
    (repo / "file.txt").write_text("changed by turn\n")
    turn_file = repo / "created_by_turn.txt"
    turn_file.write_text("scaffolded\n")
    # Rewind: tracked edit reverted AND the turn-created file removed (complete).
    assert cp.restore(checkpoint) is True
    assert (repo / "file.txt").read_text() == "original\n"
    assert not turn_file.exists()


def test_safety_snapshot_recovers_removed_untracked(repo: Path) -> None:
    cp = RewindCheckpointer(cwd=str(repo))
    checkpoint = cp.checkpoint(label="before")
    assert checkpoint is not None
    turn_file = repo / "created_by_turn.txt"
    turn_file.write_text("precious work\n")
    before_count = len(cp.checkpoints())
    assert cp.restore(checkpoint) is True
    assert not turn_file.exists()  # cleaned
    # The safety snapshot captured tracked+untracked → it is recoverable.
    safety = cp.checkpoints()[-1]
    assert len(cp.checkpoints()) == before_count + 1
    assert "safety" in safety.label
    # The removed untracked file lives in the safety commit's tree.
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", safety.commit_ref],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "created_by_turn.txt" in listing
    # And its content is recoverable verbatim.
    content = subprocess.run(
        ["git", "-C", str(repo), "show", f"{safety.commit_ref}:created_by_turn.txt"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert content == "precious work\n"


def test_restore_captures_safety_checkpoint(repo: Path) -> None:
    cp = RewindCheckpointer(cwd=str(repo))
    first = cp.checkpoint(label="before")
    assert first is not None
    (repo / "file.txt").write_text("work in progress\n")
    before_count = len(cp.checkpoints())
    cp.restore(first)
    # A safety snapshot of the in-progress work was captured before reverting.
    assert len(cp.checkpoints()) == before_count + 1
    assert "safety" in cp.checkpoints()[-1].label
