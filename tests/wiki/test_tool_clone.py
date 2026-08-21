"""Tests for WikiCloneRepoTool — TDD: write tests first, then implement."""
from __future__ import annotations

import asyncio
import shutil
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
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-cs", "git.home/org/repo"))
    store.attach_job_session("job-cs", "sess-cs")
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.home/org/repo"),
        RepoCredential(kind="token", value="ghp_store", username=None),
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
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-leak", "git.home/org/repo"))
    store.attach_job_session("job-leak", "sess-leak")
    SECRET = "ghp_durableLEAK999"
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.home/org/repo"),
        RepoCredential(kind="token", value=SECRET, username=None),
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
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-ssh", "git.home/org/repo"))
    store.attach_job_session("job-ssh", "sess-ssh")
    CredentialStore.save(
        store, CredentialScope.from_slug("git.home/org/repo"),
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


# ── Test 9: rejected stored token, ambient credential rescues ─────────────────


def test_stored_token_rejected_falls_back_to_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoked stored token no longer wedges the clone: the chain advances to the
    ambient git credential, a warning names the rejected scope, and the argv/env
    carry the helper-disable + prompt-off hardening. This is the root-cause fix."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-rescue", "git.home/org/repo"))
    store.attach_job_session("job-rescue", "sess-rescue")

    REJECTED = "ghp_rejected000"
    AMBIENT = "ghp_ambientOK111"
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.home/org/repo"),
        RepoCredential(kind="token", value=REJECTED),
    )
    # Override the conftest autouse (ambient→None): a valid ambient credential.
    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: RepoCredential(kind="token", value=AMBIENT),
    )

    clone_dir = tmp_path / "clones" / "job-rescue"
    clone_calls: list = []
    clone_envs: list = []

    def _run(cmd, **kwargs):
        if "clone" in cmd:
            clone_calls.append(list(cmd))
            clone_envs.append(dict(kwargs.get("env") or {}))
            url_arg = cmd[-2]
            if REJECTED in url_arg:
                return subprocess.CompletedProcess(
                    cmd, 128, b"",
                    (
                        f"fatal: unable to access "
                        f"'https://x-access-token:{REJECTED}@git.home/org/repo/': "
                        "The requested URL returned error: 403"
                    ).encode(),
                )
            # The ambient-authed URL succeeds.
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "README.md").write_text("hi")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"abc1234\n", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-rescue")
    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_run):
        result = asyncio.run(
            tool.handle(_make_action_step({"url": "https://git.home/org/repo"}))
        )

    # Rescued: not an error, and the queued wire event fired.
    assert "repo_access" not in result.content
    events = store.load_job_events("job-rescue")
    assert any(e["type"] == "queued" for e in events)

    # Exactly two clone attempts: rejected store token, then the ambient rescue.
    assert len(clone_calls) == 2
    assert REJECTED in clone_calls[0][-2]
    assert AMBIENT in clone_calls[1][-2]

    # Every attempt disables the git credential helper (argv) and forces
    # GIT_TERMINAL_PROMPT=0 (env) so git never touches the mounted cred file.
    for call in clone_calls:
        assert "credential.helper=" in call
    for env in clone_envs:
        assert env.get("GIT_TERMINAL_PROMPT") == "0"

    # A WARNING log names the rejected STORE scope (the Settings-UI story).
    warnings = [
        e for e in events if e["type"] == "log" and e.get("level") == "warn"
    ]
    assert any(
        "git.home/org/repo" in w["text"] and "rejected" in w["text"].lower()
        for w in warnings
    )

    # Neither candidate secret leaks into any event or persisted file.
    assert REJECTED not in str(events) and AMBIENT not in str(events)
    for f in tmp_path.rglob("*"):
        if f.is_file() and "credentials" not in f.parts:
            text = f.read_text(errors="replace")
            assert REJECTED not in text and AMBIENT not in text


# ── Test 10: a non-auth (network) failure aborts without burning candidates ────


def test_non_auth_failure_does_not_iterate_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network error (not an auth rejection) aborts the chain immediately — a
    valid ambient candidate is NEVER tried, and the error surfaces as repo_access."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    store.create_job(_job("job-net", "git.home/org/repo"))
    store.attach_job_session("job-net", "sess-net")
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.home/org/repo"),
        RepoCredential(kind="token", value="ghp_first"),
    )
    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: RepoCredential(kind="token", value="ghp_ambient"),
    )

    clone_calls: list = []

    def _run(cmd, **kwargs):
        if "clone" in cmd:
            clone_calls.append(list(cmd))
            return subprocess.CompletedProcess(
                cmd, 128, b"",
                b"fatal: unable to access '...': Could not resolve host: git.home",
            )
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-net")
    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime), \
         patch("subprocess.run", side_effect=_run):
        result = asyncio.run(
            tool.handle(_make_action_step({"url": "https://git.home/org/repo"}))
        )

    assert "repo_access" in result.content
    # Aborted after the FIRST attempt — the ambient candidate was never reached.
    assert len(clone_calls) == 1
    errors = [e for e in store.load_job_events("job-net") if e["type"] == "error"]
    assert len(errors) == 1
    assert "resolve host" in errors[0]["error"]["message"]


# ---------------------------------------------------------------------------
# clone_working_checkout — the PERSISTENT checkout, against a REAL local git
# ---------------------------------------------------------------------------
#
# Driven against a real bare origin rather than a patched ``subprocess.run``,
# because all three properties under test are properties of what git DID: that a
# non-empty target does not defeat the clone, that history and every branch
# survive, and that the token the executor injected is gone from the resulting
# ``.git/config``. A stubbed subprocess can only re-assert the argv this module
# already builds, which is the shape of test that would still pass if the
# behaviour regressed.


def _git_local(repo: Path, *args: str) -> None:
    """Run a local git command in *repo*, failing the test on a non-zero exit."""
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def bare_origin(tmp_path: Path) -> Path:
    """A bare repo with two commits on ``main`` and a second branch."""
    if shutil.which("git") is None:  # pragma: no cover — CI always has git
        pytest.skip("git not installed")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git_local(seed, "init", "-b", "main")
    _git_local(seed, "config", "user.email", "t@e.com")
    _git_local(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("first\n")
    _git_local(seed, "add", "-A")
    _git_local(seed, "commit", "-m", "first")
    (seed / "README.md").write_text("second\n")
    _git_local(seed, "add", "-A")
    _git_local(seed, "commit", "-m", "second")
    _git_local(seed, "branch", "release")

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(origin)],
        capture_output=True, text=True, check=True,
    )
    return origin


def test_working_checkout_clones_over_the_seeded_project_folder(
    tmp_path: Path, bare_origin: Path
) -> None:
    """A target holding a CLAUDE.md still clones — and the file does not survive.

    This is the exact shape ``create_project`` hands the checkout route: the
    folder exists and already contains a Mewbo ``CLAUDE.md``, which plain
    ``git clone`` refuses outright. The executor's ``reset_dir`` empties it
    first, which is what makes cloning straight into the allocated path viable.
    """
    from mewbo_graph.plugins.wiki.clone import clone_working_checkout

    target = tmp_path / "project"
    target.mkdir()
    (target / "CLAUDE.md").write_text("# seeded by create_project\n")

    outcome = clone_working_checkout(
        str(bare_origin), target, store=None, slug="git.example.com/acme/beacon", timeout=60
    )

    assert outcome.ok, outcome.stderr
    assert (target / ".git").is_dir()
    assert (target / "README.md").read_text() == "second\n"
    # The seeded placeholder is gone: the checkout IS the repository, and this
    # origin ships no CLAUDE.md of its own.
    assert not (target / "CLAUDE.md").exists()


def test_working_checkout_keeps_a_repository_s_own_claude_md(
    tmp_path: Path, bare_origin: Path
) -> None:
    """A CLAUDE.md tracked BY the repository arrives with the clone and wins."""
    from mewbo_graph.plugins.wiki.clone import clone_working_checkout

    # Add a tracked CLAUDE.md to the origin.
    contributor = tmp_path / "contributor"
    subprocess.run(
        ["git", "clone", str(bare_origin), str(contributor)],
        capture_output=True, text=True, check=True,
    )
    _git_local(contributor, "config", "user.email", "t@e.com")
    _git_local(contributor, "config", "user.name", "t")
    (contributor / "CLAUDE.md").write_text("# the repository's own guide\n")
    _git_local(contributor, "add", "-A")
    _git_local(contributor, "commit", "-m", "add guide")
    _git_local(contributor, "push", "origin", "main")

    target = tmp_path / "project"
    target.mkdir()
    (target / "CLAUDE.md").write_text("# seeded by create_project\n")

    outcome = clone_working_checkout(
        str(bare_origin), target, store=None, slug="git.example.com/acme/beacon", timeout=60
    )

    assert outcome.ok, outcome.stderr
    assert (target / "CLAUDE.md").read_text() == "# the repository's own guide\n"


def test_working_checkout_keeps_history_and_every_branch(
    tmp_path: Path, bare_origin: Path
) -> None:
    """Not shallow and not single-branch — a task diffs and branches off main."""
    from mewbo_graph.plugins.wiki.clone import clone_working_checkout

    target = tmp_path / "project"
    outcome = clone_working_checkout(
        str(bare_origin), target, store=None, slug="git.example.com/acme/beacon", timeout=60
    )

    assert outcome.ok, outcome.stderr
    log = subprocess.run(
        ["git", "-C", str(target), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(log) == 2, "a --depth=1 clone would leave exactly one commit"
    assert not (target / ".git" / "shallow").exists()

    remote_branches = subprocess.run(
        ["git", "-C", str(target), "branch", "-r"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "origin/release" in remote_branches, "--single-branch would have dropped it"


def test_working_checkout_scrubs_the_credential_out_of_the_origin_url(
    tmp_path: Path, bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The winning token must not persist in .git/config for an agent to read.

    The executor authenticates by injecting the credential into the URL, and
    ``git clone`` records exactly that string as ``remote.origin.url``. An
    ephemeral index clone is deleted with the token still in it; this checkout
    is handed to an agent, so the token would otherwise ride into a transcript
    via ``git remote -v``.
    """
    from mewbo_graph.plugins.wiki import clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import clone_working_checkout
    from mewbo_graph.wiki.credentials import CredentialCandidate, CredentialSource, RepoCredential

    secret = "ghp_never_in_git_config"
    url = f"file://{bare_origin}"

    def _chain(store, slug, *, arg_token=None):
        yield CredentialCandidate(
            source=CredentialSource.STORE_REPO,
            credential=RepoCredential(kind="token", value=secret),
        )

    monkeypatch.setattr(clone_mod, "resolve_chain", _chain)

    target = tmp_path / "project"
    outcome = clone_working_checkout(
        url, target, store=None, slug="git.example.com/acme/beacon", timeout=60
    )

    assert outcome.ok, outcome.stderr
    config = (target / ".git" / "config").read_text()
    assert secret not in config
    remotes = subprocess.run(
        ["git", "-C", str(target), "remote", "-v"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert secret not in remotes
    assert url in remotes
