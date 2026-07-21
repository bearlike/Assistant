"""Route tests for /v1/git/credentials* — the product-wide git credential registry.

Uses a temp JsonWikiStore and a stub runtime, mirroring test_routes.py's
fixture shape (there is no shared conftest for the wiki Flask app fixtures).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

API_KEY = "test-key-123"


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    return rt


@pytest.fixture()
def app(tmp_path: Path, monkeypatch, store, runtime_stub):
    """Flask test app with the git-credentials blueprint mounted standalone."""
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.git_credentials_routes as gc_routes
    from flask import Flask
    from mewbo_api.wiki.git_credentials_routes import register

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register(flask_app, runtime_stub)

    yield flask_app, store

    gc_routes._runtime = None


@pytest.fixture()
def client(app):
    flask_app, store = app
    return flask_app.test_client(), store


# ── Auth ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/git/credentials"),
    ("PUT", "/v1/git/credentials/git.example.com"),
    ("DELETE", "/v1/git/credentials/git.example.com"),
    ("POST", "/v1/git/credentials/git.example.com/validate"),
])
def test_auth_required(client, method, path):
    c, _ = client
    resp = c.open(path, method=method)
    assert resp.status_code == 401


# ── GET /v1/git/credentials ──────────────────────────────────────────────────


def test_list_credentials_empty(client):
    c, _ = client
    resp = c.get("/v1/git/credentials", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    assert resp.get_json() == {"credentials": []}


def test_list_credentials_redacts_value_never_present(client):
    """The raw credential value must never appear anywhere in the response —
    not in a field, not anywhere in the serialized body."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    c, store = client
    token_value = "ghp_supersecrettoken1234"
    ssh_value = "-----BEGIN OPENSSH PRIVATE KEY-----\nsupersecretkeymaterial\n-----END-----"
    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.example.com"),
        RepoCredential(kind="token", value=token_value, username=None),
    )
    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(kind="ssh_key", value=ssh_value, username="git"),
    )

    resp = c.get("/v1/git/credentials", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    raw = resp.get_data(as_text=True)
    assert token_value not in raw
    assert "supersecretkeymaterial" not in raw

    creds = {row["scope"]: row for row in resp.get_json()["credentials"]}
    assert creds["git.example.com"]["scopeType"] == "host"
    assert creds["git.example.com"]["kind"] == "token"
    assert creds["git.example.com"]["valueHint"] == "…1234"
    assert creds["git.example.com"]["username"] is None
    assert creds["git.example.com/org/repo"]["scopeType"] == "repo"
    assert creds["git.example.com/org/repo"]["kind"] == "ssh_key"
    assert creds["git.example.com/org/repo"]["valueHint"] == "ssh key"
    assert creds["git.example.com/org/repo"]["username"] == "git"


# ── PUT / DELETE roundtrip ───────────────────────────────────────────────────


def test_put_then_list_roundtrip_repo_scope(client):
    c, store = client
    resp = c.put(
        "/v1/git/credentials/git.example.com/org/repo",
        json={"kind": "token", "value": "ghp_newtoken1234"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    cred = CredentialStore.load(store, CredentialScope.from_slug("git.example.com/org/repo"))
    assert cred is not None
    assert cred.kind == "token"
    assert cred.value == "ghp_newtoken1234"
    assert cred.updated_at is not None  # stamped by CredentialStore.save

    listing = c.get("/v1/git/credentials", headers={"X-Api-Key": API_KEY}).get_json()
    assert listing["credentials"][0]["scope"] == "git.example.com/org/repo"
    assert listing["credentials"][0]["scopeType"] == "repo"


def test_put_host_scope_with_username(client):
    c, store = client
    resp = c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token", "value": "ghp_hosttoken1234", "username": "bot"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200

    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    cred = CredentialStore.load(store, CredentialScope.from_slug("git.example.com"))
    assert cred is not None
    assert cred.username == "bot"

    listing = c.get("/v1/git/credentials", headers={"X-Api-Key": API_KEY}).get_json()
    assert listing["credentials"][0]["scopeType"] == "host"


def test_put_overwrites_existing_credential(client):
    c, store = client
    c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token", "value": "ghp_old1111"},
        headers={"X-Api-Key": API_KEY},
    )
    c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token", "value": "ghp_new2222"},
        headers={"X-Api-Key": API_KEY},
    )
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    cred = CredentialStore.load(store, CredentialScope.from_slug("git.example.com"))
    assert cred is not None
    assert cred.value == "ghp_new2222"


def test_delete_roundtrip(client):
    c, store = client
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.example.com"),
        RepoCredential(kind="token", value="ghp_x", username=None),
    )
    resp = c.delete("/v1/git/credentials/git.example.com", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert CredentialStore.load(store, CredentialScope.from_slug("git.example.com")) is None


def test_delete_absent_returns_404(client):
    c, _ = client
    resp = c.delete("/v1/git/credentials/no.such.host", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 404


# ── PUT validation ────────────────────────────────────────────────────────────


def test_put_rejects_empty_value(client):
    c, _ = client
    resp = c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token", "value": ""},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_put_rejects_invalid_kind(client):
    c, _ = client
    resp = c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "bogus", "value": "x"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_put_rejects_missing_value(client):
    c, _ = client
    resp = c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400


# ── POST .../validate ─────────────────────────────────────────────────────────


def test_validate_not_found(client):
    c, _ = client
    resp = c.post(
        "/v1/git/credentials/no.such.host/validate", json={}, headers={"X-Api-Key": API_KEY}
    )
    assert resp.status_code == 404


def test_validate_requires_repo_url_for_host_scope(client):
    c, store = client
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    CredentialStore.save(
        store,
        CredentialScope.from_slug("git.example.com"),
        RepoCredential(kind="token", value="ghp_x", username=None),
    )
    resp = c.post(
        "/v1/git/credentials/git.example.com/validate", json={}, headers={"X-Api-Key": API_KEY}
    )
    assert resp.status_code == 400


def test_validate_ok_defaults_repo_url_from_scope(client, monkeypatch):
    c, store = client
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(kind="token", value="ghp_validtoken", username=None),
    )
    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return MagicMock(returncode=0, stdout=b"abc123\tHEAD\n", stderr=b"")

    monkeypatch.setattr("mewbo_api.wiki.git_credentials_routes.subprocess.run", _fake_run)
    resp = c.post(
        "/v1/git/credentials/git.example.com/org/repo/validate",
        json={},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "ls-remote" in seen["cmd"]
    assert "credential.helper=" in seen["cmd"]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    # The default repoUrl (derived from the repo scope) is what's embedded, and
    # the injected token never leaks into the request assertions above — this
    # just confirms the URL argument carries the derived host, not raw scope.
    url_arg = seen["cmd"][seen["cmd"].index("HEAD") - 1]
    assert "git.example.com/org/repo" in url_arg


def test_validate_fail_scrubs_secret_from_detail(client, monkeypatch):
    c, store = client
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(kind="token", value="ghp_secrettoken", username=None),
    )

    def _fake_run(cmd, **kwargs):
        return MagicMock(
            returncode=128,
            stdout=b"",
            stderr=b"fatal: Authentication failed for url containing ghp_secrettoken",
        )

    monkeypatch.setattr("mewbo_api.wiki.git_credentials_routes.subprocess.run", _fake_run)
    resp = c.post(
        "/v1/git/credentials/git.example.com/org/repo/validate",
        json={"repoUrl": "https://git.example.com/org/repo"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert "ghp_secrettoken" not in body["detail"]
    assert "<redacted>" in body["detail"]


def test_validate_times_out_gracefully(client, monkeypatch):
    c, store = client
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(kind="token", value="ghp_x", username=None),
    )

    def _fake_run(cmd, **kwargs):
        import subprocess

        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr("mewbo_api.wiki.git_credentials_routes.subprocess.run", _fake_run)
    resp = c.post(
        "/v1/git/credentials/git.example.com/org/repo/validate",
        json={},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert "timed out" in body["detail"]


def test_validate_token_threads_username_into_url(client, monkeypatch):
    """[L3/C1] A credential's own username is injected into the ls-remote URL
    exactly as the clone chain does (GitLab oauth2 / deploy-token style), so
    validate authenticates the same way a real clone would — not always as the
    default x-access-token."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    c, store = client
    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(kind="token", value="glpat_secret", username="oauth2"),
    )
    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return MagicMock(returncode=0, stdout=b"abc\tHEAD\n", stderr=b"")

    monkeypatch.setattr("mewbo_api.wiki.git_credentials_routes.subprocess.run", _fake_run)
    resp = c.post(
        "/v1/git/credentials/git.example.com/org/repo/validate",
        json={"repoUrl": "https://git.example.com/org/repo"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    url_arg = seen["cmd"][seen["cmd"].index("HEAD") - 1]
    assert "oauth2" in url_arg
    assert "x-access-token" not in url_arg


def test_validate_ssh_key_rejects_https_url(client):
    """[A2] An SSH key validated against an https URL never runs the anonymous
    https probe as a verdict — it returns ok:false with an explicit 'need an SSH
    URL' detail (the repo-scope default is https://<scope>, so an SSH key MUST
    supply an SSH repoUrl)."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    c, store = client
    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(
            kind="ssh_key", value="-----BEGIN KEY-----\nx\n-----END-----", username="git"
        ),
    )
    resp = c.post(
        "/v1/git/credentials/git.example.com/org/repo/validate",
        json={}, headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert "SSH repository URL" in body["detail"]


def test_validate_ssh_key_runs_against_ssh_url(client, monkeypatch):
    """[A2] Given an SSH-form URL the key IS exercised: ls-remote runs with
    GIT_SSH_COMMAND against the scp-form URL (never the anonymous https probe)."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    c, store = client
    CredentialStore.save(
        store, CredentialScope.from_slug("git.example.com/org/repo"),
        RepoCredential(
            kind="ssh_key", value="-----BEGIN KEY-----\nx\n-----END-----", username="git"
        ),
    )
    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return MagicMock(returncode=0, stdout=b"abc\tHEAD\n", stderr=b"")

    monkeypatch.setattr("mewbo_api.wiki.git_credentials_routes.subprocess.run", _fake_run)
    resp = c.post(
        "/v1/git/credentials/git.example.com/org/repo/validate",
        json={"repoUrl": "git@git.example.com:org/repo"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert "GIT_SSH_COMMAND" in seen["env"]
    assert "git@git.example.com:org/repo" in seen["cmd"]


# ── Boundary validation: the scope + the body are both validated ─────────────


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("PUT", ""), ("DELETE", ""), ("POST", "/validate")],
)
def test_malformed_scope_is_a_clean_400(client, method, suffix):
    """A malformed scope fails FAST at the boundary.

    It used to be accepted and written under a key the resolution chain could
    never look up — surfacing much later as an opaque "the clone silently fell
    back to anonymous" on a private repo.
    """
    c, _ = client
    # A URL is not a store key (the '://' scheme is the tell).
    resp = c.open(
        "/v1/git/credentials/https://git.example.com/org/repo" + suffix,
        method=method,
        json={"kind": "token", "value": "ghp_x"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "validation"
    assert "scope" in body["fields"]


def test_malformed_scope_never_reaches_the_store(client):
    """The 400 is a REJECTION, not a cosmetic error — nothing is persisted."""
    c, store = client
    c.put(
        "/v1/git/credentials/git.example.com//repo",  # empty inner segment
        json={"kind": "token", "value": "ghp_x"},
        headers={"X-Api-Key": API_KEY},
    )
    assert store.list_credentials() == {}


def test_put_body_forbids_extra_fields(client):
    """``extra="forbid"`` — a client can't smuggle a scope/updatedAt through the body.

    Silently ignoring them would let a caller believe it had set a scope other
    than the one in the URL (the store key), i.e. two bindings that disagree.
    """
    c, _ = client
    resp = c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token", "value": "ghp_x", "scope": "evil.com"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_put_normalises_the_scope_key(client):
    """A scope pasted as a remote URL keys the SLUG the resolution chain looks up.

    Without normalisation the credential is stored but never resolved.
    """
    c, store = client
    resp = c.put(
        "/v1/git/credentials/GIT.EXAMPLE.COM/org/repo.git",
        json={"kind": "token", "value": "ghp_x"},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200

    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    assert (
        CredentialStore.load(store, CredentialScope.from_slug("git.example.com/org/repo"))
        is not None
    )
    listing = c.get("/v1/git/credentials", headers={"X-Api-Key": API_KEY}).get_json()
    assert listing["credentials"][0]["scope"] == "git.example.com/org/repo"


def test_put_token_value_is_stripped(client):
    """A PAT pasted with a trailing newline silently 401s — strip it at the model."""
    c, store = client
    c.put(
        "/v1/git/credentials/git.example.com",
        json={"kind": "token", "value": "  ghp_padded\n"},
        headers={"X-Api-Key": API_KEY},
    )

    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore

    cred = CredentialStore.load(store, CredentialScope.from_slug("git.example.com"))
    assert cred is not None and cred.value == "ghp_padded"
