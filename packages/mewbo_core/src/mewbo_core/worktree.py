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
import secrets
import subprocess
from pathlib import Path

from mewbo_core.common import get_logger

logger = get_logger(name="core.worktree")


WORKTREES_DIR = ".mewbo/worktrees"
"""Subdirectory of the parent repo that holds all managed worktrees."""

MEWBO_BRANCH_PREFIX = "mewbo/"
"""Prefix for branches auto-created by Mewbo for managed worktrees.

Branches with this prefix are owned by Mewbo and may be deleted automatically
when their backing worktree is removed. Anything outside this prefix is
treated as user-owned and left alone.
"""

_INVALID_SLUG_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class WorktreeBranchInUseError(RuntimeError):
    """Raised when ``git worktree add`` rejects a branch already checked out.

    Surfaces a more actionable message than the raw git stderr, but remains
    a ``RuntimeError`` subclass so existing ``except RuntimeError`` callers
    keep working unchanged (LSP-friendly, KISS).
    """

    def __init__(self, branch: str, existing_path: str | None = None) -> None:  # noqa: D107
        loc = f" at '{existing_path}'" if existing_path else ""
        super().__init__(
            f"Branch '{branch}' is already checked out{loc}. "
            "Pick a different branch, or check out another branch in the parent repo first."
        )
        self.branch = branch
        self.existing_path = existing_path


_BRANCH_IN_USE_RE = re.compile(
    r"'([^']+)' is already checked out at '([^']+)'", re.IGNORECASE
)


def slugify_branch(branch: str) -> str:
    """Convert a branch name to a filesystem-safe directory name.

    Replaces any sequence of non-alphanumeric/non-``._-`` characters with a
    single ``-``. Trims leading/trailing separators.
    """
    s = _INVALID_SLUG_CHARS.sub("-", branch.strip()).strip("-._")
    if not s:
        raise ValueError(f"Branch name '{branch}' has no slug-safe characters")
    return s


def is_mewbo_branch(branch: str) -> bool:
    """Return True iff *branch* was auto-created by Mewbo.

    Used by cleanup logic to decide whether a branch is safe to ``-D`` after
    its worktree is removed. Anything outside :data:`MEWBO_BRANCH_PREFIX` is
    considered user-owned.
    """
    return branch.startswith(MEWBO_BRANCH_PREFIX)


def generate_worktree_branch_name(base: str) -> str:
    """Return a fresh ``mewbo/<base-slug>-<short-id>`` branch name.

    The 6-char hex suffix makes the name unique without a git roundtrip.
    The base is slugified so e.g. ``feature/auth`` yields
    ``mewbo/feature-auth-ab12cd``.
    """
    suffix = secrets.token_hex(3)  # 6 hex chars, ~16M space — plenty for UI defaults.
    base_slug = slugify_branch(base) if base else "branch"
    return f"{MEWBO_BRANCH_PREFIX}{base_slug}-{suffix}"


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
    def create(repo_path: str, branch: str, *, base: str | None = None) -> str:
        """Create a worktree for *branch* under ``<repo>/.mewbo/worktrees/<slug>``.

        Two modes:

        * ``base is None`` — *branch* must already exist (locally or as a
          remote-tracking ref). Runs ``git worktree add <path> <branch>``.
        * ``base`` provided — *branch* must NOT exist yet. Runs
          ``git worktree add -b <branch> <path> <base>`` which creates the
          new branch from *base* and checks it out into the worktree
          atomically.

        Returns the absolute path of the new worktree. Raises
        :class:`WorktreeBranchInUseError` when *branch* is already checked
        out somewhere; raises :class:`FileExistsError` when the target
        directory already exists.
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

        if base:
            git_args = ["worktree", "add", "-b", branch, str(target), base]
        else:
            git_args = ["worktree", "add", str(target), branch]

        try:
            _git(str(repo), *git_args)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            match = _BRANCH_IN_USE_RE.search(stderr)
            if match:
                raise WorktreeBranchInUseError(
                    branch=match.group(1),
                    existing_path=match.group(2),
                ) from exc
            raise RuntimeError(
                f"git worktree add failed for branch '{branch}': {stderr}"
            ) from exc
        logger.info(
            "Created worktree at %s for branch %s%s",
            target,
            branch,
            f" (from {base})" if base else "",
        )
        return str(target)

    @staticmethod
    def branches_in_use(repo_path: str) -> set[str]:
        """Return the set of branch names that cannot back a new worktree.

        A branch is "in use" when it is the active checkout of the parent repo
        or any existing worktree. ``git worktree add`` will refuse such names
        unless ``--force`` is passed (which we don't).

        Detached HEADs and worktrees with no branch contribute nothing.
        """
        in_use: set[str] = set()
        current = WorktreeManager.current_branch(repo_path)
        if current:
            in_use.add(current)
        for wt in WorktreeManager.list_worktrees(repo_path):
            br = wt.get("branch", "")
            if br:
                in_use.add(br)
        return in_use

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

    @staticmethod
    def delete_branch(repo_path: str, branch: str) -> bool:
        """Best-effort delete of a local branch with ``git branch -D``.

        Returns ``True`` on success, ``False`` if git refused (e.g. branch
        still checked out somewhere, or doesn't exist). Never raises — branch
        cleanup is auxiliary and should not fail worktree removal.
        """
        result = _git(repo_path, "branch", "-D", branch, check=False)
        if result.returncode == 0:
            logger.info("Deleted branch %s in %s", branch, repo_path)
            return True
        logger.debug(
            "git branch -D %s failed in %s: %s",
            branch,
            repo_path,
            (result.stderr or "").strip(),
        )
        return False
