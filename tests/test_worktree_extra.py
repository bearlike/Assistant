"""Extra tests for mewbo_core/worktree.py — covers the missing lines
identified in the coverage gap analysis.

Lines targeted:
  153, 154, 156 — current_branch: FileNotFoundError (no git) + returncode != 0
  185, 188-190 — list_branches: CalledProcessError + remote-only dedup path
  218, 219 — list_worktrees: parsing without trailing blank line
  237 — list_worktrees: CalledProcessError
  303-307, 311 — create: CalledProcessError with / without _BRANCH_IN_USE_RE match
  355-359 — is_clean: CalledProcessError on status + ValueError on ahead-count
  374-375 — remove: non-existent path returns early
  398-399 — remove: _git(rev-parse) CalledProcessError → fallback repo path
  407-416 — remove: force=True CalledProcessError; retry-with-force paths
  426-427 — prune: CalledProcessError is non-fatal

Uses real git subprocess calls against a temporary repository (skipped when
git is absent on PATH).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("git") is None:  # pragma: no cover
    pytest.skip("git not installed", allow_module_level=True)

from mewbo_core.worktree import (
    WORKTREES_DIR,
    WorktreeBranchInUseError,
    WorktreeManager,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(str(repo), "init", "-b", "main")
    _run(str(repo), "config", "user.email", "test@example.com")
    _run(str(repo), "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _run(str(repo), "add", "-A")
    _run(str(repo), "commit", "-m", "init")
    _run(str(repo), "branch", "feature/auth")
    return repo


# ---------------------------------------------------------------------------
# current_branch — error paths (lines 153-158)
# ---------------------------------------------------------------------------


def test_current_branch_returns_none_when_git_missing(monkeypatch) -> None:
    """FileNotFoundError from subprocess → None (git not on PATH)."""
    import mewbo_core.worktree as wt_mod

    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(
        wt_mod, "_git", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("no git"))
    )
    # Use a direct test rather than monkeypatching the private module function:
    # simulate the exact path by providing a non-git dir and patching subprocess.
    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if cmd[:2] == ["git", "-C"] and "symbolic-ref" in cmd:
            raise FileNotFoundError("git not found")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    result = WorktreeManager.current_branch("/any/path")
    assert result is None


def test_current_branch_returns_none_for_detached_head(tmp_path: Path) -> None:
    """Detached HEAD → symbolic-ref returns non-zero → None."""
    repo = _make_repo(tmp_path)
    # Detach HEAD at the current commit
    sha = _run(str(repo), "rev-parse", "HEAD").strip()
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--detach", sha],
        check=True,
        capture_output=True,
    )
    result = WorktreeManager.current_branch(str(repo))
    assert result is None


def test_current_branch_returns_none_for_empty_stdout(monkeypatch) -> None:
    """Empty stdout from symbolic-ref → None."""
    from types import SimpleNamespace

    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if "symbolic-ref" in cmd:
            return SimpleNamespace(returncode=0, stdout="  \n", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    result = WorktreeManager.current_branch("/any")
    assert result is None


# ---------------------------------------------------------------------------
# list_branches — CalledProcessError + remote dedup (lines 185-203)
# ---------------------------------------------------------------------------


def test_list_branches_returns_empty_on_subprocess_error(monkeypatch) -> None:
    """CalledProcessError from git for-each-ref → empty list."""
    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if "for-each-ref" in cmd:
            raise subprocess.CalledProcessError(128, cmd, stderr="not a repo")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    result = WorktreeManager.list_branches("/not/a/repo")
    assert result == []


def test_list_branches_deduplicates_remote_against_local(tmp_path: Path) -> None:
    """Remote branch names that duplicate local names are not included twice."""
    repo = _make_repo(tmp_path)
    # Clone the repo so 'clone' has origin/main → remote entry
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(repo), str(clone)], check=True, capture_output=True)
    branches = WorktreeManager.list_branches(str(clone))
    # 'main' should appear only once despite being in both local and remote refs
    assert branches.count("main") == 1


def test_list_branches_skips_head_pointers(tmp_path: Path) -> None:
    """HEAD ref entries from for-each-ref are excluded."""
    repo = _make_repo(tmp_path)
    branches = WorktreeManager.list_branches(str(repo))
    assert all("HEAD" not in b for b in branches)


# ---------------------------------------------------------------------------
# list_worktrees — parsing edge cases (lines 218-237)
# ---------------------------------------------------------------------------


def test_list_worktrees_returns_empty_on_error(monkeypatch) -> None:
    """CalledProcessError from git worktree list → empty list."""
    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if "worktree" in cmd and "list" in cmd:
            raise subprocess.CalledProcessError(128, cmd)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    result = WorktreeManager.list_worktrees("/any")
    assert result == []


def test_list_worktrees_handles_no_trailing_blank_line(monkeypatch) -> None:
    """Porcelain output without a final blank line still yields all entries."""
    from types import SimpleNamespace

    # Simulate two worktrees with no trailing newline
    porcelain = (
        "worktree /repo\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/.mewbo/worktrees/feature-auth\n"
        "HEAD def456\n"
        "branch refs/heads/feature/auth"
    )
    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if "worktree" in cmd and "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=porcelain, stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    result = WorktreeManager.list_worktrees("/repo")
    assert len(result) == 2
    assert result[0]["branch"] == "main"
    assert result[1]["branch"] == "feature/auth"


# ---------------------------------------------------------------------------
# create — CalledProcessError with and without branch-in-use match (lines 303-311)
# ---------------------------------------------------------------------------


def test_create_calledprocesserror_with_branch_in_use_re(tmp_path: Path, monkeypatch) -> None:
    """CalledProcessError matching _BRANCH_IN_USE_RE raises WorktreeBranchInUseError."""
    import mewbo_core.worktree as wt_mod

    repo = _make_repo(tmp_path)

    call_count = [0]
    original_git = wt_mod._git

    def _patched_git(repo_str, *args, **kwargs):
        # Allow prune, symbolic-ref, worktree list, worktree prune, gitignore ops
        # Fail only the actual "worktree add" of feature/auth
        if "worktree" in args and "add" in args:
            call_count[0] += 1
            if call_count[0] >= 2:
                err = subprocess.CalledProcessError(
                    128,
                    list(args),
                    stderr="fatal: 'feature/auth' is already checked out at '/other'",
                )
                raise err
        return original_git(repo_str, *args, **kwargs)

    monkeypatch.setattr(wt_mod, "_git", _patched_git)

    # feature/auth is free, so we need a different scenario.
    # Simpler: patch subprocess directly to fail with the right message.

    original_run = subprocess.run

    def _fail_with_in_use(cmd, **kwargs):
        if "worktree" in cmd and "add" in cmd and "feature" in " ".join(str(c) for c in cmd):
            raise subprocess.CalledProcessError(
                128, cmd, stderr="fatal: 'feature/auth' is already checked out at '/other'"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _fail_with_in_use)

    with pytest.raises(WorktreeBranchInUseError) as exc_info:
        WorktreeManager.create(str(repo), "feature/auth")
    assert exc_info.value.branch == "feature/auth"


def test_create_calledprocesserror_without_branch_in_use_re(tmp_path: Path, monkeypatch) -> None:
    """A CalledProcessError that doesn't match _BRANCH_IN_USE_RE raises plain RuntimeError."""
    repo = _make_repo(tmp_path)
    original_run = subprocess.run

    def _fail_generic(cmd, **kwargs):
        if "worktree" in cmd and "add" in cmd and "new-branch" in " ".join(str(c) for c in cmd):
            raise subprocess.CalledProcessError(128, cmd, stderr="some other git error")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _fail_generic)
    # Create new-branch first so it exists
    _run(str(repo), "branch", "new-branch")

    with pytest.raises(RuntimeError, match="git worktree add failed"):
        WorktreeManager.create(str(repo), "new-branch")


# ---------------------------------------------------------------------------
# is_clean — error paths (lines 355-375)
# ---------------------------------------------------------------------------


def test_is_clean_false_for_nonexistent_path() -> None:
    """Non-existent worktree path → False without subprocess call."""
    result = WorktreeManager.is_clean("/totally/missing/path")
    assert result is False


def test_is_clean_false_when_status_fails(tmp_path: Path, monkeypatch) -> None:
    """CalledProcessError on git status → False."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    original_run = subprocess.run

    def _fail_status(cmd, **kwargs):
        if "status" in cmd and "--porcelain" in cmd:
            raise subprocess.CalledProcessError(128, cmd)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _fail_status)
    result = WorktreeManager.is_clean(path)
    assert result is False


def test_is_clean_false_when_ahead_count_not_integer(tmp_path: Path, monkeypatch) -> None:
    """Non-integer stdout from rev-list --count → False."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if "rev-list" in cmd and "--count" in cmd:
            from types import SimpleNamespace

            return SimpleNamespace(returncode=0, stdout="not-a-number\n", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    result = WorktreeManager.is_clean(path)
    assert result is False


def test_is_clean_true_when_no_upstream(tmp_path: Path) -> None:
    """When rev-list @{u}..HEAD fails (no upstream), falls back to True if status is clean."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    # No upstream configured → ahead check returns non-zero → fallback True
    result = WorktreeManager.is_clean(path)
    assert result is True


# ---------------------------------------------------------------------------
# remove — early return + rev-parse failure + force error paths (lines 398-416)
# ---------------------------------------------------------------------------


def test_remove_noop_for_nonexistent_path(tmp_path: Path) -> None:
    """Removing a path that does not exist is a no-op (no exception)."""
    WorktreeManager.remove(str(tmp_path / "ghost"))
    # No exception is the assertion.


def test_remove_uses_fallback_repo_path_when_rev_parse_fails(tmp_path: Path, monkeypatch) -> None:
    """When git rev-parse --git-common-dir fails, repo is inferred from wt path structure."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    original_run = subprocess.run

    def _patched_run(cmd, **kwargs):
        if "rev-parse" in cmd and "--git-common-dir" in cmd:
            raise subprocess.CalledProcessError(128, cmd)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    # Should still succeed — uses wt.parent.parent.parent fallback
    WorktreeManager.remove(path)
    assert not Path(path).exists()


def test_remove_force_raises_on_repeated_failure(tmp_path: Path, monkeypatch) -> None:
    """When force=True and both git worktree remove attempts fail, RuntimeError is raised."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    original_run = subprocess.run

    def _fail_remove(cmd, **kwargs):
        if "worktree" in cmd and "remove" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="locked worktree")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _fail_remove)

    with pytest.raises(RuntimeError, match="git worktree remove failed"):
        WorktreeManager.remove(path, force=True)


def test_remove_retries_with_force_on_first_failure(tmp_path: Path, monkeypatch) -> None:
    """Non-force remove failing triggers a retry with --force; success is accepted."""
    repo = _make_repo(tmp_path)
    path = WorktreeManager.create(str(repo), "feature/auth")
    wt_path = Path(path)

    original_run = subprocess.run
    attempts: list[list] = []

    def _patched_run(cmd, **kwargs):
        if "worktree" in cmd and "remove" in cmd and "--force" not in cmd:
            attempts.append(list(cmd))
            raise subprocess.CalledProcessError(1, cmd, stderr="inconsistent")
        if "worktree" in cmd and "remove" in cmd and "--force" in cmd:
            attempts.append(list(cmd))
            # Actually do the removal
            import shutil as _shutil

            if wt_path.exists():
                _shutil.rmtree(wt_path)
            return original_run(["git", "-C", str(repo), "worktree", "prune"], **kwargs)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    # force=False so we take the retry path
    WorktreeManager.remove(path, force=False)
    # Two remove attempts: one without --force, one with --force
    remove_attempts = [a for a in attempts if "remove" in a]
    assert len(remove_attempts) == 2


# ---------------------------------------------------------------------------
# prune — CalledProcessError is non-fatal (lines 426-427)
# ---------------------------------------------------------------------------


def test_prune_non_fatal_on_failure(monkeypatch) -> None:
    """CalledProcessError from git worktree prune is logged and swallowed."""
    original_run = subprocess.run

    def _fail_prune(cmd, **kwargs):
        if "worktree" in cmd and "prune" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _fail_prune)
    # Must not raise
    WorktreeManager.prune("/any/repo")


# ---------------------------------------------------------------------------
# worktree_path (line 207)
# ---------------------------------------------------------------------------


def test_worktree_path_uses_slugified_branch(tmp_path: Path) -> None:
    """worktree_path converts branch slashes to dashes."""
    repo = tmp_path / "repo"
    path = WorktreeManager.worktree_path(str(repo), "feature/auth")
    assert path == repo / WORKTREES_DIR / "feature-auth"


# ---------------------------------------------------------------------------
# branches_in_use — current_branch returns None edge case
# ---------------------------------------------------------------------------


def test_branches_in_use_handles_no_current_branch(tmp_path: Path, monkeypatch) -> None:
    """When current_branch returns None (detached HEAD), it doesn't add None to in-use set."""
    repo = _make_repo(tmp_path)
    # Detach HEAD so symbolic-ref returns non-zero (returncode != 0 → None)
    original_run = subprocess.run
    from types import SimpleNamespace

    def _patched_run(cmd, **kwargs):
        if "symbolic-ref" in cmd:
            # Return non-zero without raising (check=False path)
            return SimpleNamespace(returncode=128, stdout="", stderr="HEAD is detached")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("mewbo_core.worktree.subprocess.run", _patched_run)
    in_use = WorktreeManager.branches_in_use(str(repo))
    # current_branch returns None → not added to in_use; no exception
    assert isinstance(in_use, set)
    assert None not in in_use
