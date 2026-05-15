"""Tests for WikiCloneRepoTool — TDD: write tests first, then implement."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import IndexingJob

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
    import mewbo_core.builtin_plugins.wiki.clone as clone_mod
    from mewbo_core.builtin_plugins.wiki.clone import WikiCloneRepoTool

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
    import mewbo_core.builtin_plugins.wiki.clone as clone_mod
    from mewbo_core.builtin_plugins.wiki.clone import WikiCloneRepoTool

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
    import mewbo_core.builtin_plugins.wiki.clone as clone_mod
    from mewbo_core.builtin_plugins.wiki.clone import WikiCloneRepoTool

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
    import mewbo_core.builtin_plugins.wiki.clone as clone_mod
    from mewbo_core.builtin_plugins.wiki.clone import WikiCloneRepoTool

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
    import mewbo_core.builtin_plugins.wiki.clone as clone_mod
    from mewbo_core.builtin_plugins.wiki.clone import WikiCloneRepoTool

    store = _store(tmp_path)
    runtime = _fake_runtime(store)
    tool = WikiCloneRepoTool(session_id="sess-nobody")

    with patch.object(clone_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(tool.handle(_make_action_step({"url": "https://github.com/org/repo"})))

    assert "internal" in result.content
