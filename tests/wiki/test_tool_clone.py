"""Tests for WikiCloneRepoTool — TDD: write tests first, then implement."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

# ── Helpers ────────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-clone", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="queued",
        scanned_count=0,
        total_count=0,
        current_file=None,
    )


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _git_success_side_effect(clone_dir: Path):
    """Return a side_effect function for subprocess.run that creates a fake repo."""

    def _run(cmd, **kwargs):
        # First call is git clone — create some fake files in clone_dir
        if "clone" in cmd:
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "README.md").write_text("hello")
            (clone_dir / "src").mkdir(exist_ok=True)
            (clone_dir / "src" / "main.py").write_text("# main")
            (clone_dir / ".git").mkdir(exist_ok=True)
            (clone_dir / ".git" / "config").write_text("[core]")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")
        # Second call is git rev-parse HEAD
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=b"abc1234\n", stderr=b""
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    return _run


# ── Test 1: successful clone emits queued event ────────────────────────────────


def test_clone_success_emits_queued_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful clone: queued event emitted, totalCount set on job + event."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))

    store = _store(tmp_path)
    job = _job("job-s1", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-s1", "sess-s1")

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-s1")

    clone_dir = tmp_path / "clones" / "job-s1"

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_git_success_side_effect(clone_dir)):
        result = asyncio.run(tool.handle(_make_action_step({"url": "https://github.com/org/repo"})))

    # Result should not be an error
    assert "repo_access" not in result.content

    # Verify the canonical wire-shape event landed. Phase/log telemetry
    # events are emitted alongside it now — those are timeline-only and
    # not asserted here.
    events = [e for e in store.load_job_events("job-s1") if e["type"] not in ("phase", "log")]
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "queued"
    assert ev["jobId"] == "job-s1"
    assert ev["slug"] == "org/repo"
    assert ev["totalCount"] > 0

    # Verify job record updated
    updated = store.get_job("job-s1")
    assert updated is not None
    assert updated.total_count == ev["totalCount"]
    assert updated.total_count > 0


# ── Test 2: clone failure emits error event ────────────────────────────────────


def test_clone_failure_emits_error_event(tmp_path: Path) -> None:
    """Non-zero git exit → error event with code=repo_access + stderr in message."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool

    store = _store(tmp_path)
    job = _job("job-f1", "org/bad-repo")
    store.create_job(job)
    store.attach_job_session("job-f1", "sess-f1")

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-f1")

    failed_proc = subprocess.CompletedProcess(
        ["git", "clone", "..."], returncode=128,
        stdout=b"", stderr=b"fatal: repository 'https://github.com/org/bad-repo' not found"
    )

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", return_value=failed_proc):
        result = asyncio.run(tool.handle(_make_action_step({"url": "https://github.com/org/bad-repo"})))

    assert "repo_access" in result.content

    # Phase + log events are emitted alongside; isolate the canonical
    # error wire event for the assertion.
    events = [e for e in store.load_job_events("job-f1") if e["type"] not in ("phase", "log")]
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "error"
    assert ev["error"]["code"] == "repo_access"
    assert "fatal" in ev["error"]["message"] or "not found" in ev["error"]["message"]


# ── Test 3: token rewriting + never persisted ──────────────────────────────────


def test_token_rewrites_url_and_is_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token is injected into clone URL but never written to any file."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))

    store = _store(tmp_path)
    job = _job("job-t1", "org/private-repo")
    store.create_job(job)
    store.attach_job_session("job-t1", "sess-t1")

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-t1")

    clone_dir = tmp_path / "clones" / "job-t1"
    captured_calls: list = []

    def _capturing_run(cmd, **kwargs):
        captured_calls.append(list(cmd))
        return _git_success_side_effect(clone_dir)(cmd, **kwargs)

    SECRET_TOKEN = "ghp_supersecrettoken123"

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_capturing_run):
        asyncio.run(tool.handle(_make_action_step({
            "url": "https://github.com/org/private-repo",
            "token": SECRET_TOKEN,
        })))

    # Clone call should contain x-access-token in the URL
    clone_call = next((c for c in captured_calls if "clone" in c), None)
    assert clone_call is not None
    clone_url = next(arg for arg in clone_call if "x-access-token" in arg or "github.com" in arg)
    assert "x-access-token" in clone_url
    assert SECRET_TOKEN in clone_url  # token IS in the in-process URL passed to git

    # But token must NOT appear in any persisted file under tmp_path
    for f in tmp_path.rglob("*"):
        if f.is_file():
            text = f.read_text(errors="replace")
            assert SECRET_TOKEN not in text, f"Token found in persisted file {f}"


# ── Test 4: clone_dir is used as target ───────────────────────────────────────


def test_clone_dir_is_used_as_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess.run is called with ctx.clone_dir as the final argument."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))

    store = _store(tmp_path)
    job = _job("job-d1", "org/repo")
    store.create_job(job)
    store.attach_job_session("job-d1", "sess-d1")

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-d1")

    expected_clone_dir = tmp_path / "clones" / "job-d1"
    captured_calls: list = []

    def _capturing_run(cmd, **kwargs):
        captured_calls.append(list(cmd))
        return _git_success_side_effect(expected_clone_dir)(cmd, **kwargs)

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_capturing_run):
        asyncio.run(tool.handle(_make_action_step({"url": "https://github.com/org/repo"})))

    clone_call = next(c for c in captured_calls if "clone" in c)
    # Last argument to git clone is the target directory
    assert clone_call[-1] == str(expected_clone_dir)
    # clone_dir parent must have been created
    assert expected_clone_dir.parent.exists()


# ── Test 5: unknown session → internal error ───────────────────────────────────


def test_unknown_session_returns_internal_error(tmp_path: Path) -> None:
    """If no wiki job is associated with the session, return WikiError code=internal."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool

    store = _store(tmp_path)
    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-nobody")

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"url": "https://github.com/org/repo"})))

    assert "internal" in result.content


# ── Test 6: token resolved from the durable CredentialStore ────────────────────


def test_token_resolved_from_credential_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No arg token, cold CloneTokenCache → clone reads the durable credential."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
    from mewbo_graph.wiki.credentials import CredentialStore
    from mewbo_graph.wiki.tokens import CloneTokenCache
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-cs", "git.home/org/repo"))
    store.attach_job_session("job-cs", "sess-cs")
    CloneTokenCache.forget("job-cs")
    CredentialStore.save(
        store, "git.home/org/repo", RepoCredential(kind="token", value="ghp_store", username=None)
    )

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-cs")
    clone_dir = tmp_path / "clones" / "job-cs"
    calls: list = []

    def _capturing_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _git_success_side_effect(clone_dir)(cmd, **kwargs)

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_capturing_run):
        asyncio.run(tool.handle(_make_action_step({"url": "https://git.home/org/repo"})))

    clone_call = next(c for c in calls if "clone" in c)
    assert any("x-access-token" in arg and "ghp_store" in arg for arg in clone_call)


# ── Test 6b: durable-credential token is scrubbed from clone error output ──────


def test_durable_token_scrubbed_from_clone_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token resolved from the durable CredentialStore (args.token is None)
    must be scrubbed from BOTH the persisted error event AND the returned tool
    result when git fails and echoes the auth'd URL into stderr."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
    from mewbo_graph.wiki.credentials import CredentialStore
    from mewbo_graph.wiki.tokens import CloneTokenCache
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-leak", "git.home/org/repo"))
    store.attach_job_session("job-leak", "sess-leak")
    CloneTokenCache.forget("job-leak")
    SECRET = "ghp_durableLEAK999"
    CredentialStore.save(
        store, "git.home/org/repo", RepoCredential(kind="token", value=SECRET, username=None)
    )

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-leak")

    # git echoes the *authenticated* URL (with the injected token) into stderr.
    stderr_text = (
        f"fatal: unable to access 'https://x-access-token:{SECRET}@git.home/org/repo/': "
        "The requested URL returned error: 403"
    )
    failed_proc = subprocess.CompletedProcess(
        ["git", "clone", "..."], returncode=128, stdout=b"",
        stderr=stderr_text.encode(),
    )

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", return_value=failed_proc):
        result = asyncio.run(
            tool.handle(_make_action_step({"url": "https://git.home/org/repo"}))
        )

    # The returned tool result must NOT carry the secret, and must redact it.
    assert SECRET not in result.content
    assert "<redacted>" in result.content

    # The persisted error event must NOT carry the secret either.
    events = [e for e in store.load_job_events("job-leak") if e["type"] == "error"]
    assert len(events) == 1
    msg = events[0]["error"]["message"]
    assert SECRET not in msg
    assert "<redacted>" in msg


# ── Test 7: SSH-key credential sets GIT_SSH_COMMAND, temp key cleaned up ────────


def test_ssh_key_credential_sets_git_ssh_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ssh_key credential drives clone via GIT_SSH_COMMAND, not URL injection,
    and the temp key file is removed afterward."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
    from mewbo_graph.wiki.credentials import CredentialStore
    from mewbo_graph.wiki.tokens import CloneTokenCache
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-ssh", "git.home/org/repo"))
    store.attach_job_session("job-ssh", "sess-ssh")
    CloneTokenCache.forget("job-ssh")
    CredentialStore.save(
        store, "git.home/org/repo",
        RepoCredential(kind="ssh_key", value="PRIVATEKEYDATA", username="git"),
    )

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-ssh")
    clone_dir = tmp_path / "clones" / "job-ssh"
    seen_env: dict = {}
    seen_keyfile: list[str] = []

    def _capturing_run(cmd, **kwargs):
        env = kwargs.get("env") or {}
        if "clone" in cmd and "GIT_SSH_COMMAND" in env:
            seen_env["GIT_SSH_COMMAND"] = env["GIT_SSH_COMMAND"]
            # capture the -i <path> the command references so we can assert cleanup
            parts = env["GIT_SSH_COMMAND"].split()
            if "-i" in parts:
                seen_keyfile.append(parts[parts.index("-i") + 1])
        return _git_success_side_effect(clone_dir)(cmd, **kwargs)

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_capturing_run):
        asyncio.run(tool.handle(_make_action_step({"url": "ssh://git@git.home/org/repo.git"})))

    assert "GIT_SSH_COMMAND" in seen_env
    assert "StrictHostKeyChecking=accept-new" in seen_env["GIT_SSH_COMMAND"]
    # The temp key file is cleaned up in the finally block.
    assert seen_keyfile and not Path(seen_keyfile[0]).exists()
    # SSH path must NOT leak the key into any persisted file.
    for f in tmp_path.rglob("*"):
        if f.is_file() and "credentials" not in f.parts:
            assert "PRIVATEKEYDATA" not in f.read_text(errors="replace")


# ── Test 8: GIT_SSH_COMMAND key path is shell-quoted (TMPDIR with a space) ──────


def test_ssh_command_quotes_key_path_with_spaces(tmp_path: Path) -> None:
    """When the temp key path contains a space the GIT_SSH_COMMAND key path
    must be shell-quoted so it isn't split mid-path."""
    import os
    import shlex

    from mewbo_graph.plugins.wiki.clone import _ssh_env_for

    spaced_tmp = tmp_path / "dir with space"
    spaced_tmp.mkdir()
    real_mkstemp = __import__("tempfile").mkstemp

    def _mkstemp_in_spaced(*args, **kwargs):
        kwargs.setdefault("dir", str(spaced_tmp))
        return real_mkstemp(*args, **kwargs)

    with patch("tempfile.mkstemp", side_effect=_mkstemp_in_spaced):
        env, key_path = _ssh_env_for("PRIVATEKEYDATA")
    try:
        assert env is not None and key_path is not None
        assert " " in str(key_path)  # sanity: the space really is in the path
        cmd = env["GIT_SSH_COMMAND"]
        # The quoted path round-trips through shlex back to the real path —
        # an unquoted f"ssh -i {path}" would split into two tokens here.
        tokens = shlex.split(cmd)
        assert tokens[tokens.index("-i") + 1] == str(key_path)
        assert shlex.quote(str(key_path)) in cmd
        assert os.access(key_path, os.R_OK)
    finally:
        if key_path is not None:
            key_path.unlink(missing_ok=True)
