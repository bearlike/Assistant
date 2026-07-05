"""Tests for RemoteBranchLister + the POST /v1/wiki/branches endpoint.

Stubs ONLY the subprocess boundary (``git ls-remote``) — never hits the network.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

# A realistic ``git ls-remote --symref <url> HEAD refs/heads/*`` sample: a symref
# line naming the default branch, then several branch-head lines (one out of
# alpha order to prove the lister sorts).
_LS_REMOTE_STDOUT = (
    b"ref: refs/heads/main\tHEAD\n"
    b"a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4\tHEAD\n"
    b"a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4\trefs/heads/main\n"
    b"00112233445566778899aabbccddeeff00112233\trefs/heads/develop\n"
    b"ffeeddccbbaa99887766554433221100ffeeddcc\trefs/heads/feature/login\n"
)


# ── RemoteBranchLister ─────────────────────────────────────────────────────────


def test_list_heads_parses_symref_and_branches() -> None:
    """A realistic --symref sample → sorted branches + default_branch from HEAD."""
    from mewbo_graph.plugins.wiki.branches import RemoteBranches, RemoteBranchLister

    proc = subprocess.CompletedProcess(
        ["git", "ls-remote"], returncode=0, stdout=_LS_REMOTE_STDOUT, stderr=b""
    )
    with patch("subprocess.run", return_value=proc):
        result = RemoteBranchLister(url="https://github.com/org/repo").list_heads()

    assert isinstance(result, RemoteBranches)
    assert result.default_branch == "main"
    assert result.branches == ["develop", "feature/login", "main"]


def test_list_heads_no_symref_leaves_default_none() -> None:
    """No ``ref: ...HEAD`` line → default_branch is None, branches still parsed."""
    from mewbo_graph.plugins.wiki.branches import RemoteBranchLister

    stdout = (
        b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\trefs/heads/main\n"
        b"cafebabecafebabecafebabecafebabecafebabe\trefs/heads/release\n"
    )
    proc = subprocess.CompletedProcess(
        ["git", "ls-remote"], returncode=0, stdout=stdout, stderr=b""
    )
    with patch("subprocess.run", return_value=proc):
        result = RemoteBranchLister(url="https://github.com/org/repo").list_heads()

    assert result.default_branch is None
    assert result.branches == ["main", "release"]


def test_list_heads_failure_scrubs_token() -> None:
    """Non-zero exit → BranchListError with the injected token redacted."""
    from mewbo_graph.plugins.wiki.branches import BranchListError, RemoteBranchLister

    SECRET = "ghp_supersecrettoken123"
    stderr = (
        f"fatal: unable to access 'https://x-access-token:{SECRET}@git.home/org/repo/': "
        "The requested URL returned error: 403"
    ).encode()
    proc = subprocess.CompletedProcess(
        ["git", "ls-remote"], returncode=128, stdout=b"", stderr=stderr
    )
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(BranchListError) as excinfo:
            RemoteBranchLister(url="https://git.home/org/repo", token=SECRET).list_heads()

    msg = str(excinfo.value)
    assert SECRET not in msg
    assert "<redacted>" in msg


def test_list_heads_token_injected_into_url() -> None:
    """A token is injected into the ls-remote URL (x-access-token), not persisted."""
    from mewbo_graph.plugins.wiki.branches import RemoteBranchLister

    SECRET = "ghp_token456"
    captured: list = []

    def _capture(cmd, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=_LS_REMOTE_STDOUT, stderr=b""
        )

    with patch("subprocess.run", side_effect=_capture):
        RemoteBranchLister(url="https://github.com/org/repo", token=SECRET).list_heads()

    assert any("x-access-token" in arg and SECRET in arg for arg in captured[0])


def test_list_heads_private_host_skips_tls() -> None:
    """A private-TLD host inserts ``-c http.sslVerify=false`` (mirrors clone)."""
    from mewbo_graph.plugins.wiki.branches import RemoteBranchLister

    captured: list = []

    def _capture(cmd, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=_LS_REMOTE_STDOUT, stderr=b""
        )

    with patch("subprocess.run", side_effect=_capture):
        RemoteBranchLister(url="https://git.home/org/repo").list_heads()

    cmd = captured[0]
    assert "-c" in cmd and "http.sslVerify=false" in cmd


# ── build_clone_command (shared clone argv) ─────────────────────────────────────


def test_build_clone_command_pins_ref() -> None:
    """A non-null ref → ``--branch <ref> --single-branch`` in the argv."""
    from mewbo_graph.plugins.wiki.clone import build_clone_command

    cmd = build_clone_command(
        "https://github.com/org/repo", "/tmp/clone", ref="release-1", private_host=False
    )
    assert "--branch" in cmd
    assert cmd[cmd.index("--branch") + 1] == "release-1"
    assert "--single-branch" in cmd
    assert cmd[-1] == "/tmp/clone"


def test_build_clone_command_no_ref_no_branch_flag() -> None:
    """A null ref → no ``--branch`` (default-branch clone), no TLS skip on public."""
    from mewbo_graph.plugins.wiki.clone import build_clone_command

    cmd = build_clone_command(
        "https://github.com/org/repo", "/tmp/clone", ref=None, private_host=False
    )
    assert "--branch" not in cmd
    assert "http.sslVerify=false" not in cmd
