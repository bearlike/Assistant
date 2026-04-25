#!/usr/bin/env python3
"""Git worktree management.

Thin subprocess wrapper around ``git worktree`` for creating, listing,
inspecting, and removing branch-bound worktrees rooted under
``<repo>/.mewbo/worktrees/<slug>/``.

Design rules (KISS):

* One worktree per branch. Directory name is the slugified branch name.
  No separate ``name`` field, no rename, no collision handling beyond what
  git itself enforces.
* All operations are git subprocess calls — no extra dependencies.
* "Clean" means ``git status --porcelain`` is empty AND there are no commits
  ahead of upstream (matches Claude Code's auto-cleanup default).
* The caller (``project_store``) decides whether to persist a record;
  this module deals with the filesystem and git only.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from mewbo_core.common import get_logger

logger = get_logger(name="core.worktree")


WORKTREES_DIR = ".mewbo/worktrees"
"""Subdirectory of the parent repo that holds all managed worktrees."""

_INVALID_SLUG_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_branch(branch: str) -> str:
    """Convert a branch name to a filesystem-safe directory name.

    Replaces any sequence of non-alphanumeric/non-``._-`` characters with a
    single ``-``. Trims leading/trailing separators.
    """
    s = _INVALID_SLUG_CHARS.sub("-", branch.strip()).strip("-._")
    if not s:
        raise ValueError(f"Branch name '{branch}' has no slug-safe characters")
    return s


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <repo> <args>`` and return the completed process."""
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _ensure_gitignore_entry(repo_path: Path, entry: str) -> None:
    """Append ``entry`` to ``.gitignore`` if not already present.

    Idempotent. Best-effort — failures are logged, not raised.
    """
    gi = repo_path / ".gitignore"
    try:
        existing = gi.read_text() if gi.exists() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if entry in lines or f"/{entry}" in lines:
            return
        sep = "" if existing.endswith("\n") or not existing else "\n"
        gi.write_text(f"{existing}{sep}{entry}\n")
    except Exception:  # pragma: no cover - I/O edge
        logger.warning("Could not update .gitignore in %s", repo_path, exc_info=True)


class WorktreeManager:
    """Stateless wrapper around ``git worktree``.

    All methods are static — there is nothing to remember between calls.
    Uses the parent repo path supplied by the caller.
    """

    @staticmethod
    def current_branch(repo_path: str) -> str | None:
        """Return the parent repo's current branch (HEAD), or ``None``.

        Returns ``None`` when HEAD is detached, the path is not a git
        repository, or git itself errors out. Callers should treat ``None``
        as "no default branch" and fall back to letting the user pick.
        """
        try:
            result = _git(repo_path, "symbolic-ref", "--short", "HEAD", check=False)
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name or None

    @staticmethod
    def list_branches(repo_path: str) -> list[str]:
        """List branches that can back a new worktree.

        Returns local branches plus remote-tracked branch short-names that
        do not duplicate a local branch. Excludes ``HEAD`` pointers.
        """
        try:
            result = _git(
                repo_path,
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
                "refs/remotes",
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to list branches in %s: %s", repo_path, exc.stderr)
            return []

        local: list[str] = []
        remote: list[str] = []
        seen_local: set[str] = set()
        for raw in result.stdout.splitlines():
            ref = raw.strip()
            if not ref or "/HEAD" in ref:
                continue
            if "/" in ref and ref.split("/", 1)[0] in {"origin", "upstream"}:
                # Remote branch — strip remote prefix for selection display.
                short = ref.split("/", 1)[1]
                if short and short != "HEAD":
                    remote.append(short)
            else:
                local.append(ref)
                seen_local.add(ref)

        # Dedupe remote against local; preserve order; final dedup pass.
        merged: list[str] = []
        seen: set[str] = set()
        for b in local + [r for r in remote if r not in seen_local]:
            if b not in seen:
                merged.append(b)
                seen.add(b)
        return merged

    @staticmethod
    def worktree_path(repo_path: str, branch: str) -> Path:
        """Return the canonical worktree directory for *branch* under *repo_path*."""
        return Path(repo_path) / WORKTREES_DIR / slugify_branch(branch)

    @staticmethod
    def list_worktrees(repo_path: str) -> list[dict[str, str]]:
        """Return ``git worktree list --porcelain`` parsed into dicts.

        Each entry: ``{"path", "branch", "head"}``. Missing fields are
        empty strings.
        """
        try:
            result = _git(repo_path, "worktree", "list", "--porcelain")
        except subprocess.CalledProcessError:
            return []
        out: list[dict[str, str]] = []
        cur: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line:
                if cur:
                    out.append(cur)
                    cur = {}
                continue
            if line.startswith("worktree "):
                cur["path"] = line[len("worktree ") :]
            elif line.startswith("HEAD "):
                cur["head"] = line[len("HEAD ") :]
            elif line.startswith("branch "):
                # "branch refs/heads/foo" -> "foo"
                ref = line[len("branch ") :]
                cur["branch"] = ref.replace("refs/heads/", "", 1)
        if cur:
            out.append(cur)
        return out

    @staticmethod
    def create(repo_path: str, branch: str) -> str:
        """Create a worktree for *branch* under ``<repo>/.mewbo/worktrees/<slug>``.

        The branch must already exist (locally or as a remote-tracking ref).
        Idempotent in the sense that re-creating an existing worktree raises;
        callers should check ``worktree_path().exists()`` first if they need
        soft handling.

        Returns the absolute path of the new worktree.
        """
        repo = Path(repo_path).resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"Not a git repository: {repo_path}")

        WorktreeManager.prune(str(repo))  # clear stale entries first
        target = WorktreeManager.worktree_path(str(repo), branch)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            raise FileExistsError(
                f"Worktree path already exists: {target}. "
                "Remove it first or use a different branch."
            )

        _ensure_gitignore_entry(repo, f"{WORKTREES_DIR}/")

        try:
            _git(str(repo), "worktree", "add", str(target), branch)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"git worktree add failed for branch '{branch}': {exc.stderr.strip()}"
            ) from exc
        logger.info("Created worktree at %s for branch %s", target, branch)
        return str(target)

    @staticmethod
    def is_clean(worktree_path: str) -> bool:
        """Return True iff the worktree is clean.

        Clean = ``git status --porcelain`` is empty AND there are no commits
        ahead of upstream.

        A worktree with no upstream configured is treated as "ahead" only if
        it has unpushed commits; absent any upstream, only working-tree state
        matters (best-effort; matches CC behavior of "if anything could be
        lost, keep it").
        """
        if not Path(worktree_path).exists():
            return False
        try:
            status = _git(worktree_path, "status", "--porcelain")
        except subprocess.CalledProcessError:
            return False
        if status.stdout.strip():
            return False

        # Check for commits ahead of upstream, if upstream is set.
        ahead = _git(
            worktree_path,
            "rev-list",
            "--count",
            "@{u}..HEAD",
            check=False,
        )
        if ahead.returncode == 0:
            try:
                return int(ahead.stdout.strip() or "0") == 0
            except ValueError:
                return False
        # No upstream configured — fall back to working-tree-only check.
        return True

    @staticmethod
    def remove(worktree_path: str, *, force: bool = False) -> None:
        """Remove a worktree.

        Refuses if not clean unless ``force=True``. Always invokes
        ``git worktree remove``; falls back to forcing on the second attempt
        if the worktree directory is in an inconsistent state.
        """
        wt = Path(worktree_path)
        if not wt.exists():
            return
        if not force and not WorktreeManager.is_clean(worktree_path):
            raise RuntimeError(
                f"Worktree {worktree_path} has uncommitted changes or unpushed commits"
            )

        # Locate parent repo from the worktree itself.
        try:
            common = _git(worktree_path, "rev-parse", "--git-common-dir").stdout.strip()
        except subprocess.CalledProcessError:
            common = ""
        repo = str(Path(common).parent.resolve()) if common else str(wt.parent.parent.parent)

        args = ["worktree", "remove", str(wt)]
        if force:
            args.append("--force")
        try:
            _git(repo, *args)
        except subprocess.CalledProcessError as exc:
            if force:
                raise RuntimeError(
                    f"git worktree remove failed: {exc.stderr.strip()}"
                ) from exc
            # Retry once with --force to recover from transient inconsistency.
            try:
                _git(repo, "worktree", "remove", "--force", str(wt))
            except subprocess.CalledProcessError as exc2:
                raise RuntimeError(
                    f"git worktree remove failed: {exc2.stderr.strip()}"
                ) from exc2
        logger.info("Removed worktree at %s", worktree_path)

    @staticmethod
    def prune(repo_path: str) -> None:
        """Run ``git worktree prune`` to clean up stale administrative records."""
        try:
            _git(repo_path, "worktree", "prune")
        except subprocess.CalledProcessError:
            logger.debug("worktree prune failed (non-fatal) in %s", repo_path)
