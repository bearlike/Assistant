"""``RemoteBranchLister`` — list a remote repo's branches without cloning.

Atomic class: it holds the URL + the credentials a single ``git ls-remote`` needs
(dependency injection — it touches NO store/cache) and exposes one public method
:meth:`list_heads`. It REUSES the clone tool's module-level credential helpers
(``_inject_token`` / ``_ssh_env_for`` / ``_is_private_host``) so the token →
URL injection, the SSH-key temp-file env, and the private-host TLS carve-out are
identical to the actual clone — never duplicated.

The wizard's branch-picker calls this BEFORE onboarding so the user can choose a
ref; the resulting ``ref`` rides :class:`~mewbo_graph.wiki.types.WizardSubmission`.
"""
from __future__ import annotations

import subprocess

from mewbo_core.common import get_logger
from pydantic import BaseModel

from mewbo_graph.plugins.wiki.clone import (
    _inject_token,
    _is_private_host,
    _redact,
    _ssh_env_for,
    build_ls_remote_command,
    hardened_git_env,
)

logging = get_logger(name="mewbo_graph.plugins.wiki.branches")


class RemoteBranches(BaseModel):
    """The branch listing for a remote repo."""

    branches: list[str]
    default_branch: str | None


class BranchListError(Exception):
    """A ``git ls-remote`` failure carrying a secret-scrubbed message."""


class RemoteBranchLister:
    """List a remote repo's branch heads via ``git ls-remote`` (no clone).

    Construct with the URL + the credential a private repo needs (token OR an
    SSH key), then call :meth:`list_heads`. All state is injected; no store.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        ssh_key: str | None = None,
        username: str | None = None,
    ) -> None:
        """Bind the repo URL and the optional credential for this listing.

        *username* accompanies a token credential (a stored credential may carry
        its own — e.g. a GitLab ``oauth2`` deploy token); it threads into
        ``_inject_token`` exactly like the clone/freshness/validate paths, so a
        username-bearing credential authenticates the branch-list step too rather
        than always injecting ``x-access-token`` and 401-ing on those platforms.
        """
        self.url = url
        self.token = token
        self.ssh_key = ssh_key
        self.username = username

    def list_heads(self) -> RemoteBranches:
        """Return the remote's branch heads + default branch (from ``HEAD``'s symref).

        Uses the SAME hardened builders as the clone/freshness paths
        (:func:`build_ls_remote_command` — helper-disable + private-host TLS
        carve-out + ``--symref``; :func:`hardened_git_env` — ``GIT_TERMINAL_PROMPT=0``
        over the SSH/inherited env). Without that hardening this jobless endpoint
        was the one ls-remote path where git could still reach the read-only
        mounted credential helper and wedge on EBUSY, masking the real auth error.
        The temp SSH key is deleted in a ``finally``; the stderr is secret-scrubbed
        via the shared :func:`_redact`. Raises :class:`BranchListError` on timeout
        or a non-zero exit.
        """
        clone_url = _inject_token(self.url, self.token, self.username)
        cmd = build_ls_remote_command(
            clone_url, "HEAD", "refs/heads/*",
            private_host=_is_private_host(self.url), symref=True,
        )

        run_env, key_path = _ssh_env_for(self.ssh_key)
        env = hardened_git_env(run_env)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30, env=env)
        except subprocess.TimeoutExpired as exc:
            raise BranchListError("git ls-remote timed out after 30s") from exc
        finally:
            if key_path is not None:
                key_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            err_msg = (proc.stderr or b"").decode(errors="ignore").strip() or "git ls-remote failed"
            err_msg = _redact(err_msg, [self.token or "", self.ssh_key or ""])
            raise BranchListError(err_msg)

        return self._parse(proc.stdout or b"")

    @staticmethod
    def _parse(stdout: bytes) -> RemoteBranches:
        r"""Parse ``git ls-remote --symref`` output into a :class:`RemoteBranches`.

        A ``ref: refs/heads/<name>\tHEAD`` line names the default branch; each
        ``<sha>\trefs/heads/<name>`` line is a branch head. Branches are returned
        sorted + de-duplicated; ``default_branch`` is ``None`` when no symref line
        was present (the caller labels the first option generically).
        """
        default_branch: str | None = None
        branches: set[str] = set()
        for raw in stdout.decode(errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("ref:") and line.endswith("\tHEAD"):
                target = line[len("ref:"):].rsplit("\t", 1)[0].strip()
                if target.startswith("refs/heads/"):
                    default_branch = target[len("refs/heads/"):]
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                branches.add(parts[1][len("refs/heads/"):])
        return RemoteBranches(branches=sorted(branches), default_branch=default_branch)


__all__ = ["RemoteBranches", "BranchListError", "RemoteBranchLister"]
