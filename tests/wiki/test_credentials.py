"""Tests for RepoCredential, the credential store methods, and CredentialStore."""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import RepoCredential


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def test_token_credential_roundtrips() -> None:
    cred = RepoCredential(kind="token", value="ghp_secret", username=None)
    dumped = cred.model_dump(mode="json")
    assert dumped == {"kind": "token", "value": "ghp_secret", "username": None}
    assert RepoCredential.model_validate(dumped) == cred


def test_ssh_key_credential_roundtrips() -> None:
    cred = RepoCredential(
        kind="ssh_key", value="-----BEGIN KEY-----\nabc\n-----END KEY-----", username="git"
    )
    assert RepoCredential.model_validate(cred.model_dump(mode="json")) == cred


def test_empty_value_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RepoCredential(kind="token", value="", username=None)


def test_unknown_kind_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RepoCredential.model_validate({"kind": "password", "value": "x", "username": None})


def test_extra_field_is_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RepoCredential.model_validate(
            {"kind": "token", "value": "x", "username": None, "extra": 1}
        )


def test_json_store_credential_crud(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_credentials("org/repo") is None
    cred = RepoCredential(kind="token", value="ghp_x", username=None)
    store.save_credentials("org/repo", cred.model_dump(mode="json"))
    assert store.get_credentials("org/repo") == cred.model_dump(mode="json")
    assert store.delete_credentials("org/repo") is True
    assert store.delete_credentials("org/repo") is False
    assert store.get_credentials("org/repo") is None


def test_json_store_credential_file_is_0600(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_credentials("org/repo", {"kind": "token", "value": "s", "username": None})
    cred_file = tmp_path / "credentials" / "org__repo.json"
    assert cred_file.exists()
    assert (cred_file.stat().st_mode & 0o777) == 0o600


def test_json_store_credentials_isolated_dir(tmp_path: Path) -> None:
    """Credentials live in their OWN subdir, never alongside job submissions."""
    store = _store(tmp_path)
    store.save_credentials("org/repo", {"kind": "token", "value": "s", "username": None})
    assert (tmp_path / "credentials").is_dir()
    # Token text must not appear anywhere under jobs/ or projects/.
    for sub in ("jobs", "projects"):
        for f in (tmp_path / sub).rglob("*"):
            if f.is_file():
                assert "s" not in f.read_text() or "value" not in f.read_text()


def test_credential_store_save_load_delete(tmp_path: Path) -> None:
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    cred = RepoCredential(kind="token", value="ghp_x", username=None)
    assert CredentialStore.load(store, "org/repo") is None
    CredentialStore.save(store, "org/repo", cred)
    loaded = CredentialStore.load(store, "org/repo")
    assert loaded == cred
    assert CredentialStore.delete(store, "org/repo") is True
    assert CredentialStore.load(store, "org/repo") is None


def test_credential_store_encode_is_identity_today(tmp_path: Path) -> None:
    """The on-disk blob equals the model dump — the seam is identity for now."""
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    cred = RepoCredential(kind="ssh_key", value="KEYDATA", username="git")
    CredentialStore.save(store, "org/repo", cred)
    raw = store.get_credentials("org/repo")
    assert raw == cred.model_dump(mode="json")


def test_credential_store_load_ignores_malformed_blob(tmp_path: Path) -> None:
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    store.save_credentials("org/repo", {"kind": "token"})  # missing value
    assert CredentialStore.load(store, "org/repo") is None


def test_recovery_counter_increments_and_isolated(tmp_path: Path) -> None:
    """The slug-keyed recovery counter increments, is per-slug, and lives on its
    own surface (never the submission sidecar)."""
    store = _store(tmp_path)
    assert store.get_recovery_attempts("org/repo") == 0
    assert store.bump_recovery_attempts("org/repo") == 1
    assert store.bump_recovery_attempts("org/repo") == 2
    assert store.get_recovery_attempts("org/repo") == 2
    # A different slug has its own independent counter.
    assert store.get_recovery_attempts("org/other") == 0
    # The submission sidecar is untouched by the counter writes.
    store.save_job_submission("j1", {"slug": "org/repo", "dirs": ["src"]})
    store.bump_recovery_attempts("org/repo")
    assert store.get_job_submission("j1") == {"slug": "org/repo", "dirs": ["src"]}
