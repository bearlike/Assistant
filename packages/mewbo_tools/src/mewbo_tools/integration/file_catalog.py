#!/usr/bin/env python3
"""Project file catalog — git-index first, worktree-safe.

One atomic class that answers "what files belong to this project?" for three
callers that must agree on the same answer:

- the ``@<ref>`` expander's scoping gate (only project files are expandable),
- the API's file-list endpoint (client `@`-autocomplete),
- the CLI's completer (`@<file>` suggestions).

Git is the source of truth: ``git ls-files --cached --others --exclude-standard``
lists tracked files **and** new files that are not ``.gitignore``d — so a
freshly-created working file is referenceable while secrets / build artifacts
under ``.gitignore`` are excluded by construction. ``-C <cwd>`` makes it
worktree-safe (a linked worktree lists its own files). Non-git directories fall
back to a bounded filesystem walk that skips the usual heavy/noise directories.
"""

from __future__ import annotations

import os
import subprocess

from mewbo_core.common import get_logger

logging = get_logger(name="tools.file_catalog")

# Directories never worth walking for a non-git fallback listing.
_WALK_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
        ".next", ".turbo", "target", ".idea", ".vscode",
    }
)


class FileCatalog:
    """List a project's files (git-index first) for scoping + autocomplete.

    Holds the resolved ``cwd`` and lazily-computed, cached results. Construct one
    per request/expansion; it never raises — a missing repo, a missing ``git``
    binary, or an unreadable tree degrades to an empty/fallback listing.
    """

    _MAX_FALLBACK_FILES = 5000

    def __init__(self, cwd: str | None) -> None:
        """Bind the project directory (``None`` → an empty, inert catalog)."""
        self._cwd = os.path.realpath(cwd) if cwd else None
        self._is_repo: bool | None = None
        self._tracked: frozenset[str] | None = None

    # -- git probing ---------------------------------------------------

    def is_git_repo(self) -> bool:
        """True if ``cwd`` is inside a git work tree (cached)."""
        if self._is_repo is not None:
            return self._is_repo
        self._is_repo = False
        if self._cwd:
            try:
                proc = subprocess.run(
                    ["git", "-C", self._cwd, "rev-parse", "--is-inside-work-tree"],
                    capture_output=True,
                    text=True,
                )
                self._is_repo = proc.returncode == 0 and proc.stdout.strip() == "true"
            except OSError as exc:  # git not on PATH
                logging.debug("git probe failed for %s: %s", self._cwd, exc)
        return self._is_repo

    def tracked_files(self) -> frozenset[str]:
        """The git-index file set (relative POSIX paths), cached."""
        if self._tracked is not None:
            return self._tracked
        files: set[str] = set()
        if self._cwd and self.is_git_repo():
            try:
                proc = subprocess.run(
                    [
                        "git", "-C", self._cwd, "ls-files",
                        "--cached", "--others", "--exclude-standard",
                    ],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode == 0:
                    files = {
                        line for line in proc.stdout.splitlines() if line.strip()
                    }
            except OSError as exc:
                logging.debug("git ls-files failed for %s: %s", self._cwd, exc)
        self._tracked = frozenset(files)
        return self._tracked

    # -- public API ----------------------------------------------------

    def contains(self, rel_path: str) -> bool:
        """True if ``rel_path`` (relative to cwd) is an allowed project file.

        Git repo → the path must be in the git index. Non-repo → the path must
        resolve to an existing file confined under ``cwd``. Used as the scoping
        gate by the expander.
        """
        if not self._cwd:
            return False
        norm = rel_path.replace(os.sep, "/").lstrip("./")
        if self.is_git_repo():
            return norm in self.tracked_files()
        # Non-git fallback: confined existing path.
        candidate = os.path.realpath(os.path.join(self._cwd, rel_path))
        if candidate != self._cwd and not candidate.startswith(self._cwd + os.sep):
            return False
        return os.path.isfile(candidate)

    def list_files(self, limit: int = 2000) -> list[str]:
        """Sorted relative POSIX paths for autocomplete / the file endpoint.

        Git repo → the git index; otherwise a bounded, noise-filtered walk.
        Capped at ``limit`` entries.
        """
        if not self._cwd:
            return []
        if self.is_git_repo():
            return sorted(self.tracked_files())[:limit]
        return self._walk_fallback(limit)

    def _walk_fallback(self, limit: int) -> list[str]:
        if not self._cwd:
            return []
        out: list[str] = []
        seen = 0
        for root, dirs, names in os.walk(self._cwd):
            dirs[:] = [d for d in dirs if d not in _WALK_SKIP_DIRS and not d.startswith(".")]
            for name in names:
                if name.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(root, name), self._cwd)
                out.append(rel.replace(os.sep, "/"))
                seen += 1
                if seen >= self._MAX_FALLBACK_FILES:
                    return sorted(out)[:limit]
        return sorted(out)[:limit]
