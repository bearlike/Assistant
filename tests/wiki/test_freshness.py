"""Tests for RepoFreshness — ls-remote + platform compare, I/O stubbed only."""
from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
from mewbo_graph.plugins.wiki.freshness import RepoFreshness

_URL = "https://git.home/o/r"
_SLUG = "git.home/o/r"
_INDEXED = "a" * 40
_REMOTE = "b" * 40


class _FakeResp:
    """Minimal context-manager stand-in for ``urllib.request.urlopen``."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _ls_remote_run(sha: str, *, rc: int = 0, stderr: bytes = b""):
    """subprocess.run side-effect returning *sha* for the ls-remote call."""

    def _run(cmd, **kwargs):
        if "ls-remote" in cmd:
            out = f"{sha}\tHEAD\n".encode() if rc == 0 else b""
            return subprocess.CompletedProcess(cmd, rc, out, stderr)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    return _run


# ── same sha → up to date ─────────────────────────────────────────────────────


def test_same_sha_is_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("subprocess.run", _ls_remote_run(_INDEXED))
    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", slug=_SLUG)
    assert result.remote_sha == _INDEXED
    assert result.behind_by == 0
    assert result.up_to_date is True
    assert result.error is None


# ── differing sha → gitea compare reports how far behind ──────────────────────


def test_gitea_compare_reports_behind_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("subprocess.run", _ls_remote_run(_REMOTE))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, context=None: _FakeResp({"total_commits": 3}),
    )
    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", slug=_SLUG)
    assert result.remote_sha == _REMOTE
    assert result.behind_by == 3
    assert result.up_to_date is False
    assert result.error is None


# ── differing sha but compare API fails → unknown count, not up to date ───────


def test_compare_failure_yields_unknown_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("subprocess.run", _ls_remote_run(_REMOTE))

    def _boom(req, timeout=None, context=None):
        raise urllib.error.URLError("compare unavailable")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", slug=_SLUG)
    assert result.remote_sha == _REMOTE
    assert result.behind_by is None
    assert result.up_to_date is False
    assert result.error is None


# ── unreachable remote → could-not-check (up_to_date None, error set) ─────────


def test_unreachable_remote_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        _ls_remote_run("", rc=128, stderr=b"fatal: Could not resolve host: git.home"),
    )
    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", slug=_SLUG)
    assert result.remote_sha is None
    assert result.behind_by is None
    assert result.up_to_date is None
    assert result.error is not None


# ── ls-remote is hardened (helper-disable in argv, prompt-off env, TLS carve) ─


def test_compare_uses_winning_credential_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1/E2: the compare API is authenticated with the token that WON the
    ls-remote (threaded from the executor's winner), never a re-resolved first."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.store import JsonWikiStore
    from mewbo_graph.wiki.types import RepoCredential

    store = JsonWikiStore(root_dir=tmp_path)
    CredentialStore.save(
        store,
        CredentialScope.from_slug(_SLUG),
        RepoCredential(kind="token", value="WINTOKEN"),
    )

    def _run(cmd, **kwargs):
        if "ls-remote" in cmd:
            url_arg = cmd[-2]  # the ls-remote URL (ref is the final arg)
            if "WINTOKEN" in url_arg:
                return subprocess.CompletedProcess(cmd, 0, f"{_REMOTE}\tHEAD\n".encode(), b"")
            return subprocess.CompletedProcess(
                cmd, 128, b"", b"remote: HTTP Basic: Access denied"
            )
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("subprocess.run", _run)

    captured: dict = {}

    def _urlopen(req, timeout=None, context=None):
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp({"total_commits": 2})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", store=store, slug=_SLUG)
    assert result.behind_by == 2
    assert result.up_to_date is False
    # gitea auth header shape → "token <value>", carrying the WINNING token.
    assert captured["auth"] == "token WINTOKEN"


def test_compare_falls_through_a_git_winner_that_the_api_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git winner is NOT proof of a valid credential (found in live E2E).

    A PUBLIC repo's git endpoint serves ``ls-remote`` even with a REVOKED token,
    so the chain never advances and the executor's winner is the dead stored
    credential. The compare API DOES challenge it (401) — so the fetch must
    re-walk the chain and succeed on the next token, not degrade to
    ``behind_by=None``. Production shape: a revoked repo-scoped token cloned a
    public repo fine, then 401'd the compare that the ambient credential answered.
    """
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.store import JsonWikiStore
    from mewbo_graph.wiki.types import RepoCredential

    store = JsonWikiStore(root_dir=tmp_path)
    # store:repo = revoked (git accepts it anyway); store:host = the live one.
    CredentialStore.save(
        store,
        CredentialScope.from_slug(_SLUG),
        RepoCredential(kind="token", value="REVOKED"),
    )
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.home"),
        RepoCredential(kind="token", value="LIVE"),
    )

    # git says yes to EVERYTHING (public repo) — the revoked token "wins".
    monkeypatch.setattr("subprocess.run", _ls_remote_run(_REMOTE))

    tried: list[str | None] = []

    def _urlopen(req, timeout=None, context=None):
        auth = req.get_header("Authorization")
        tried.append(auth)
        if auth == "token REVOKED":
            raise urllib.error.HTTPError(_URL, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        return _FakeResp({"total_commits": 7})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", store=store, slug=_SLUG)

    assert result.behind_by == 7, "must fall through the API-rejected git winner"
    assert result.up_to_date is False
    assert tried == ["token REVOKED", "token LIVE"], "winner first, then the chain"


def test_compare_does_not_fork_ambient_when_winner_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 laziness: when the preferred (git-winner) token already answers the API,
    the ambient tier must never be reached — advancing to it forks a 10s-capped
    ``git credential fill``, and an eager chain paid that on EVERY freshness check."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.store import JsonWikiStore
    from mewbo_graph.wiki.types import RepoCredential

    store = JsonWikiStore(root_dir=tmp_path)
    CredentialStore.save(
        store,
        CredentialScope.from_slug(_SLUG),
        RepoCredential(kind="token", value="GOOD"),
    )

    ambient_calls: list[str] = []

    def _ambient(host: str) -> RepoCredential | None:
        ambient_calls.append(host)
        return RepoCredential(kind="token", value="AMBIENT")

    monkeypatch.setattr(cred_mod, "ambient_credential", _ambient)
    monkeypatch.setattr("subprocess.run", _ls_remote_run(_REMOTE))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, context=None: _FakeResp({"total_commits": 7}),
    )

    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", store=store, slug=_SLUG)
    assert result.behind_by == 7
    # The stored token satisfied BOTH the ls-remote and the compare — the ambient
    # subprocess was never forked.
    assert ambient_calls == []


def test_compare_does_not_retry_on_non_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-auth API failure (5xx/network) must not burn the rest of the chain —
    another credential cannot fix an unreachable endpoint."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.store import JsonWikiStore
    from mewbo_graph.wiki.types import RepoCredential

    store = JsonWikiStore(root_dir=tmp_path)
    CredentialStore.save(
        store,
        CredentialScope.from_slug(_SLUG),
        RepoCredential(kind="token", value="TOK"),
    )
    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: RepoCredential(kind="token", value="AMBIENT"),
    )
    monkeypatch.setattr("subprocess.run", _ls_remote_run(_REMOTE))

    calls: list[str | None] = []

    def _urlopen(req, timeout=None, context=None):
        calls.append(req.get_header("Authorization"))
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    result = RepoFreshness.check(_URL, _INDEXED, platform="gitea", store=store, slug=_SLUG)
    assert result.behind_by is None
    assert result.up_to_date is False  # sha moved, just uncountable
    assert len(calls) == 1  # aborted after the first non-auth failure


def test_ls_remote_uses_hardened_argv_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _run(cmd, **kwargs):
        if "ls-remote" in cmd:
            captured["cmd"] = list(cmd)
            captured["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, f"{_INDEXED}\tHEAD\n".encode(), b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("subprocess.run", _run)
    RepoFreshness.check(_URL, _INDEXED, platform="gitea", slug=_SLUG)

    assert "credential.helper=" in captured["cmd"]
    assert "ls-remote" in captured["cmd"]
    # Private TLD (.home) → self-signed TLS carve-out, mirroring the clone path.
    assert "http.sslVerify=false" in captured["cmd"]
    assert captured["env"].get("GIT_TERMINAL_PROMPT") == "0"
