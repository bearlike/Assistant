#!/usr/bin/env python3
"""Per-turn workspace checkpoints for ``/rewind``.

``/rewind`` undoes a conversation turn AND the code it changed, together. The
hard, valuable half is the *workspace* checkpoint: a git ref captured BEFORE a
turn mutates files, so reverting the turn can also revert the files.

:class:`RewindCheckpointer` is an atomic class (state attrs + methods + DI) over
``git``:

* **Capture is non-destructive.** ``git stash create`` builds a commit object
  that snapshots the working tree + index WITHOUT touching either or pushing
  onto the stash stack. On a clean tree it returns nothing, so we fall back to
  ``HEAD`` — the tree already equals HEAD. The user's uncommitted work is never
  disturbed by taking a checkpoint.
* **Restore is safe.** Before reverting the tree we capture the CURRENT state as
  one more (safety) snapshot, so a restore can itself be undone and uncommitted
  work is never silently destroyed. We then reset tracked files to the
  checkpoint's tree via ``git restore --source=<ref> --staged --worktree``.
* **Degrades gracefully outside git.** Every method no-ops (returns ``None`` /
  ``[]`` / ``False``) when the cwd is not a git work tree, so the App can wire
  the hooks unconditionally.

Conversation truncation is the caller's job (it owns the ``SessionRuntime`` +
``session_id``): :meth:`restore` reverts ONLY the workspace and returns the
session event timestamp the caller truncates after. Pairing the two halves
stays in the command handler so this class has one responsibility.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from mewbo_core.common import get_logger

logger = get_logger(name="mewbo.cli.rewind")


@dataclass(frozen=True)
class Checkpoint:
    """One captured workspace state paired with a conversation turn.

    ``commit_ref`` is a git commit object (a ``stash create`` snapshot, or
    ``HEAD`` when the tree was clean) whose tree is the pre-turn workspace.
    ``after_ts`` is the session event timestamp the conversation should be
    truncated after to undo the turn (the event just BEFORE the turn's user
    message); ``None`` means "truncate the whole turn" / unknown.
    """

    turn_index: int
    commit_ref: str
    label: str
    after_ts: str | None = None
    head_ref: str | None = None


@dataclass
class RewindCheckpointer:
    """Capture + restore per-turn git workspace checkpoints (DI on ``cwd``).

    Construct with the working directory the turns mutate (the CLI's cwd). Call
    :meth:`checkpoint` before each turn; :meth:`restore` to revert the tree to a
    prior checkpoint. Stateful only in its in-memory checkpoint list — the
    captured commit objects live in the git object store, reachable while the
    process holds the ref (kept alive in :attr:`_checkpoints`).
    """

    cwd: str
    _checkpoints: list[Checkpoint] = field(default_factory=list)

    # -- git plumbing -----------------------------------------------------

    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """Run ``git -C <cwd> <args>`` capturing output (never shells out text)."""
        return subprocess.run(
            ["git", "-C", self.cwd, *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def is_git_repo(self) -> bool:
        """Return ``True`` when ``cwd`` is inside a git work tree."""
        try:
            result = self._git("rev-parse", "--is-inside-work-tree")
        except (OSError, FileNotFoundError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _head_ref(self) -> str | None:
        result = self._git("rev-parse", "HEAD")
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    # -- capture ----------------------------------------------------------

    def checkpoint(self, *, label: str, after_ts: str | None = None) -> Checkpoint | None:
        """Capture the current workspace as a checkpoint (non-destructive).

        Returns the recorded :class:`Checkpoint`, or ``None`` outside a git repo
        (or on any git error) — the caller wires this before every turn and
        treats ``None`` as "rewind unavailable for this turn".
        """
        if not self.is_git_repo():
            return None
        head = self._head_ref()
        # ``stash create`` snapshots tracked changes + index into a commit
        # object without touching the work tree or the stash stack. Empty
        # stdout ⇒ nothing to stash (clean tree) ⇒ the tree already == HEAD.
        created = self._git("stash", "create", label)
        ref = created.stdout.strip()
        if not ref:
            # No stash object. The expected reason is a clean tree, in which case
            # HEAD already captures the state. A non-zero return code means git
            # actually errored (dirty work that did NOT get snapshotted) — warn
            # so a silent HEAD fallback can't quietly drop tracked changes.
            if created.returncode != 0:
                logger.warning(
                    "git stash create failed ({}); falling back to HEAD: {}",
                    created.returncode,
                    created.stderr.strip(),
                )
            if head is None:
                return None
            ref = head
        checkpoint = Checkpoint(
            turn_index=len(self._checkpoints),
            commit_ref=ref,
            label=label,
            after_ts=after_ts,
            head_ref=head,
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    # -- inspect ----------------------------------------------------------

    def checkpoints(self) -> list[Checkpoint]:
        """Return the captured checkpoints, oldest first (a defensive copy)."""
        return list(self._checkpoints)

    def latest(self) -> Checkpoint | None:
        """Return the most recent checkpoint, or ``None`` when none captured."""
        return self._checkpoints[-1] if self._checkpoints else None

    # -- restore ----------------------------------------------------------

    def _safety_snapshot(self, label: str) -> str | None:
        """Capture the CURRENT state — tracked AND untracked — into a commit ref.

        ``git stash create`` omits untracked files, so a plain checkpoint can't
        recover turn-created files we are about to ``git clean``. We build the
        snapshot in a TEMPORARY index (``GIT_INDEX_FILE``) so the real index and
        work tree are never touched: ``add -A`` into the temp index →
        ``write-tree`` → ``commit-tree`` parented on HEAD. The returned commit is
        reachable (recorded as a :class:`Checkpoint`) so the rewind is fully
        reversible. Returns the commit sha, or ``None`` on any git error.
        """
        head = self._head_ref()
        tmp = tempfile.NamedTemporaryFile(prefix="mewbo-rewind-idx-", delete=False)
        tmp.close()
        env = {**os.environ, "GIT_INDEX_FILE": tmp.name}
        try:
            # Seed the temp index from HEAD so add -A produces a full tree.
            if head is not None:
                seed = subprocess.run(
                    ["git", "-C", self.cwd, "read-tree", "HEAD"],
                    capture_output=True, text=True, env=env,
                )
                if seed.returncode != 0:
                    return None
            add = subprocess.run(
                ["git", "-C", self.cwd, "add", "-A"],
                capture_output=True, text=True, env=env,
            )
            if add.returncode != 0:
                return None
            tree = subprocess.run(
                ["git", "-C", self.cwd, "write-tree"],
                capture_output=True, text=True, env=env,
            )
            tree_sha = tree.stdout.strip()
            if tree.returncode != 0 or not tree_sha:
                return None
            commit_args = ["git", "-C", self.cwd, "commit-tree", tree_sha, "-m", label]
            if head is not None:
                commit_args[5:5] = ["-p", head]
            commit = subprocess.run(commit_args, capture_output=True, text=True, env=env)
            sha = commit.stdout.strip()
            if commit.returncode != 0 or not sha:
                return None
            self._checkpoints.append(
                Checkpoint(
                    turn_index=len(self._checkpoints),
                    commit_ref=sha,
                    label=label,
                    head_ref=head,
                )
            )
            return sha
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:  # pragma: no cover - temp cleanup edge
                pass

    def restore(self, checkpoint: Checkpoint) -> bool:
        """Revert the workspace to ``checkpoint`` — tracked AND untracked (safe).

        Reverting "code + conversation together" must be COMPLETE: a turn that
        created new files has those removed too, not just tracked edits undone.
        To stay safe, we FIRST capture the current state — including untracked
        files — into a recoverable commit (:meth:`_safety_snapshot`), so nothing
        is destroyed irrecoverably. Then we:

        1. ``git restore --source <ref> --staged --worktree`` — revert tracked
           files (work tree + index) to the checkpoint tree;
        2. ``git clean -fd`` — remove turn-created untracked files/dirs. This is
           safe ONLY because the safety snapshot just recorded them; they are
           recoverable via the returned checkpoint.

        Returns ``True`` on success, ``False`` outside a git repo or on a git
        error (the tree is left as-is on a restore failure). Conversation
        truncation is the caller's job (it owns the session store).
        """
        if not self.is_git_repo():
            return False
        # Safety net: snapshot tracked+untracked so the rewind is reversible AND
        # the subsequent ``git clean`` can never lose unrecoverable work.
        if self._safety_snapshot(f"pre-rewind safety ({checkpoint.label})") is None:
            logger.warning("Could not capture safety snapshot; aborting rewind.")
            return False
        result = self._git(
            "restore",
            "--source",
            checkpoint.commit_ref,
            "--staged",
            "--worktree",
            "--",
            ".",
        )
        if result.returncode != 0:
            logger.warning(
                "git restore to {} failed: {}",
                checkpoint.commit_ref[:8],
                result.stderr.strip(),
            )
            return False
        # Remove turn-created untracked files/dirs (recoverable from the safety
        # snapshot above). -f required; -d also clears new directories.
        clean = self._git("clean", "-fd")
        if clean.returncode != 0:
            # Tracked files are already reverted; surface the clean failure but
            # do not treat it as a total failure — the safety snapshot stands.
            logger.warning("git clean after rewind failed: {}", clean.stderr.strip())
        return True


__all__ = ["Checkpoint", "RewindCheckpointer"]
