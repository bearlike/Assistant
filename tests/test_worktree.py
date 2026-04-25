"""Tests for git worktree management and project_store worktree integration.

These tests use real git subprocess calls against a temporary repository.
They are skipped when git is not available on PATH.
"""
# ruff: noqa: E402

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("git") is None:  # pragma: no cover - CI environments without git
    pytest.skip("git not installed", allow_module_level=True)

from mewbo_core.config import reset_config, set_config_override
from mewbo_core.project_store import (
    JsonProjectStore,
    VirtualProject,
    worktree_project_id,
)
from mewbo_core.worktree import (
    WORKTREES_DIR,
    WorktreeManager,
    slugify_branch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: str, *args: str) -> str:
    """Run a git command and return stdout (raises on non-zero)."""
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    """Initialize a tiny repo with one commit on `main` and a `feature` branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "config", "user.email", "test@example.com")
    _git(str(repo), "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-m", "init")
    _git(str(repo), "branch", "feature/auth")
    return repo


@pytest.fixture
def store(tmp_path: Path):
    """A JsonProjectStore configured against tmp_path with a clean override."""
    set_config_override(
        {
            "runtime": {
                "config_dir": str(tmp_path / "config"),
                "projects_home": str(tmp_path / "projects"),
            }
        }
    )
    yield JsonProjectStore()
    reset_config()


@pytest.fixture
def repo_with_parent(tmp_path: Path, store: JsonProjectStore) -> tuple[Path, VirtualProject]:
    """A real git repo plus a managed project pointing at it."""
    repo = _make_repo(tmp_path)
    parent = store.create_project(name="myrepo", description="", path=str(repo))
    return repo, parent


# ---------------------------------------------------------------------------
# slugify_branch
# ---------------------------------------------------------------------------


def test_slugify_branch_replaces_slashes() -> None:
    assert slugify_branch("feature/auth") == "feature-auth"


def test_slugify_branch_keeps_safe_chars() -> None:
    assert slugify_branch("v1.2.3-rc1") == "v1.2.3-rc1"


def test_slugify_branch_collapses_runs() -> None:
    assert slugify_branch("foo//bar  baz") == "foo-bar-baz"


def test_slugify_branch_strips_leading_separators() -> None:
    assert slugify_branch("/feature-x/") == "feature-x"


def test_slugify_branch_empty_raises() -> None:
    with pytest.raises(ValueError):
        slugify_branch("///")


# ---------------------------------------------------------------------------
# list_branches
# ---------------------------------------------------------------------------


def test_list_branches_returns_local(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    branches = WorktreeManager.list_branches(str(repo))
    assert "main" in branches
    assert "feature/auth" in branches


def test_list_branches_handles_invalid_repo(tmp_path: Path) -> None:
    bad = tmp_path / "no-repo"
    bad.mkdir()
    assert WorktreeManager.list_branches(str(bad)) == []


# ---------------------------------------------------------------------------
# create / list_worktrees / is_clean / remove
# ---------------------------------------------------------------------------


def test_create_worktree_under_managed_dir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    expected = repo / WORKTREES_DIR / "feature-auth"
    assert Path(path) == expected.resolve() or Path(path) == expected
    assert expected.exists()
    assert (expected / "README.md").exists()


def test_create_worktree_appends_gitignore(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    WorktreeManager.create(str(repo), "feature/auth")
    gi = (repo / ".gitignore").read_text()
    assert f"{WORKTREES_DIR}/" in gi


def test_create_worktree_idempotent_gitignore(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text(f"{WORKTREES_DIR}/\n")
    WorktreeManager.create(str(repo), "feature/auth")
    # Should not duplicate the entry.
    text = (repo / ".gitignore").read_text()
    assert text.count(f"{WORKTREES_DIR}/") == 1


def test_create_rejects_nongit_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        WorktreeManager.create(str(plain), "main")


def test_create_rejects_existing_worktree_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    WorktreeManager.create(str(repo), "feature/auth")
    with pytest.raises(FileExistsError):
        WorktreeManager.create(str(repo), "feature/auth")


def test_list_worktrees_includes_main_and_created(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    WorktreeManager.create(str(repo), "feature/auth")
    out = WorktreeManager.list_worktrees(str(repo))
    paths = [w.get("path", "") for w in out]
    assert any(str(repo) == p for p in paths)
    assert any("feature-auth" in p for p in paths)


def test_is_clean_true_for_fresh_worktree(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    assert WorktreeManager.is_clean(path) is True


def test_is_clean_false_when_dirty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    (Path(path) / "scratch.txt").write_text("dirty\n")
    assert WorktreeManager.is_clean(path) is False


def test_is_clean_false_when_ahead_of_upstream(tmp_path: Path) -> None:
    """A worktree with commits ahead of upstream is not clean."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    # Set up upstream pointing at the current ref, then add a commit.
    _git(path, "config", "user.email", "t@e.com")
    _git(path, "config", "user.name", "t")
    _git(path, "branch", "--set-upstream-to=main", "feature/auth")
    (Path(path) / "new.txt").write_text("ahead\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "ahead")
    assert WorktreeManager.is_clean(path) is False


def test_remove_clean_worktree(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    WorktreeManager.remove(path)
    assert not Path(path).exists()


def test_remove_refuses_dirty_without_force(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    (Path(path) / "scratch.txt").write_text("dirty\n")
    with pytest.raises(RuntimeError):
        WorktreeManager.remove(path)
    assert Path(path).exists()


def test_remove_force_dirty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    (Path(path) / "scratch.txt").write_text("dirty\n")
    WorktreeManager.remove(path, force=True)
    assert not Path(path).exists()


def test_remove_missing_path_is_noop(tmp_path: Path) -> None:
    WorktreeManager.remove(str(tmp_path / "does-not-exist"))


# ---------------------------------------------------------------------------
# project_store integration
# ---------------------------------------------------------------------------


def test_create_worktree_via_store(
    store: JsonProjectStore,
    repo_with_parent: tuple[Path, VirtualProject],
) -> None:
    _, parent = repo_with_parent
    wt: VirtualProject = store.create_worktree(parent.project_id, "feature/auth")

    assert wt.is_worktree is True
    assert wt.parent_project_id == parent.project_id
    assert wt.branch == "feature/auth"
    assert wt.project_id == worktree_project_id(parent.project_id, "feature/auth")
    assert Path(wt.path).exists()


def test_create_worktree_idempotent(
    store: JsonProjectStore,
    repo_with_parent: tuple[Path, VirtualProject],
) -> None:
    _, parent = repo_with_parent
    wt1 = store.create_worktree(parent.project_id, "feature/auth")
    wt2 = store.create_worktree(parent.project_id, "feature/auth")
    assert wt1.project_id == wt2.project_id
    assert wt1.path == wt2.path


def test_list_worktrees_filters_by_parent(
    store: JsonProjectStore,
    repo_with_parent: tuple[Path, VirtualProject],
) -> None:
    _, parent = repo_with_parent
    store.create_worktree(parent.project_id, "feature/auth")

    children = store.list_worktrees(parent.project_id)
    assert len(children) == 1
    assert children[0].branch == "feature/auth"
    assert store.list_worktrees("unknown-id") == []


def test_delete_worktree_removes_record_and_path(
    store: JsonProjectStore,
    repo_with_parent: tuple[Path, VirtualProject],
) -> None:
    _, parent = repo_with_parent
    wt = store.create_worktree(parent.project_id, "feature/auth")

    store.delete_worktree(wt.project_id)

    assert store.get_project(wt.project_id) is None
    assert not Path(wt.path).exists()


def test_delete_worktree_refuses_dirty(
    store: JsonProjectStore,
    repo_with_parent: tuple[Path, VirtualProject],
) -> None:
    _, parent = repo_with_parent
    wt = store.create_worktree(parent.project_id, "feature/auth")
    (Path(wt.path) / "dirty.txt").write_text("changes\n")

    with pytest.raises(RuntimeError):
        store.delete_worktree(wt.project_id)
    assert Path(wt.path).exists()
    assert store.get_project(wt.project_id) is not None


def test_delete_worktree_force_dirty(
    store: JsonProjectStore,
    repo_with_parent: tuple[Path, VirtualProject],
) -> None:
    _, parent = repo_with_parent
    wt = store.create_worktree(parent.project_id, "feature/auth")
    (Path(wt.path) / "dirty.txt").write_text("changes\n")

    store.delete_worktree(wt.project_id, force=True)
    assert store.get_project(wt.project_id) is None
    assert not Path(wt.path).exists()


def test_create_worktree_unknown_parent(store: JsonProjectStore) -> None:
    with pytest.raises(KeyError):
        store.create_worktree("not-a-real-id", "main")


def test_regular_project_unaffected_by_worktree_fields(store: JsonProjectStore) -> None:
    """Verify backwards compatibility: existing projects load with default flags."""
    proj = store.create_project(name="x", description="", path=None)
    fetched = store.get_project(proj.project_id)
    assert fetched is not None
    assert fetched.is_worktree is False
    assert fetched.parent_project_id is None
    assert fetched.branch is None
