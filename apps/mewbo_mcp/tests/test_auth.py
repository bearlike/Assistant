"""Tests for token pass-through authentication.

Validation is exercised against a real ``KeyStore`` (filesystem driver,
pointed at a tmp path) and the master token — no mocking of the verify path,
only the env/store I/O boundary. Header extraction is exercised against a
lightweight fake request/context (no live ASGI server).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mewbo_core.key_store import KeyStore
from mewbo_mcp.auth import (
    AuthError,
    authenticate,
    extract_bearer_token,
    validate_token,
)


def _ctx(headers: dict[str, str] | None):
    """Build a minimal Context-like object exposing request_context.request.headers."""
    request = SimpleNamespace(headers=headers) if headers is not None else None
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


def test_extract_bearer_token_ok():
    token = extract_bearer_token(_ctx({"authorization": "Bearer mk_abc"}))
    assert token == "mk_abc"


def test_extract_bearer_token_case_insensitive_header():
    token = extract_bearer_token(_ctx({"Authorization": "bearer mk_xyz"}))
    assert token == "mk_xyz"


def test_extract_bearer_token_missing_header_raises():
    with pytest.raises(AuthError):
        extract_bearer_token(_ctx({}))


def test_extract_bearer_token_malformed_raises():
    with pytest.raises(AuthError):
        extract_bearer_token(_ctx({"authorization": "Token mk_abc"}))


def test_extract_bearer_token_no_request_raises():
    with pytest.raises(AuthError):
        extract_bearer_token(_ctx(None))


# ---------------------------------------------------------------------------
# Token validation against a real KeyStore + master token
# ---------------------------------------------------------------------------


def test_validate_master_token(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_API_TOKEN", "msk-master")
    store = KeyStore(path=str(tmp_path / "keys.json"))
    # No raise = accepted.
    validate_token("msk-master", key_store=store)


def test_validate_stored_key_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_API_TOKEN", "msk-master")
    store = KeyStore(path=str(tmp_path / "keys.json"))
    plaintext, _ = store.create_key("agent-A")
    validate_token(plaintext, key_store=store)  # no raise


def test_validate_revoked_key_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_API_TOKEN", "msk-master")
    store = KeyStore(path=str(tmp_path / "keys.json"))
    plaintext, record = store.create_key("agent-B")
    store.revoke_key(record["id"])
    with pytest.raises(AuthError):
        validate_token(plaintext, key_store=store)


def test_validate_unknown_token_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_API_TOKEN", "msk-master")
    store = KeyStore(path=str(tmp_path / "keys.json"))
    with pytest.raises(AuthError):
        validate_token("mk_not_a_real_key", key_store=store)


def test_validate_empty_token_rejected(tmp_path):
    store = KeyStore(path=str(tmp_path / "keys.json"))
    with pytest.raises(AuthError):
        validate_token("", key_store=store)


# ---------------------------------------------------------------------------
# End-to-end: authenticate() extracts then validates, returning the token
# ---------------------------------------------------------------------------


def test_authenticate_returns_token_for_passthrough(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_API_TOKEN", "msk-master")
    store = KeyStore(path=str(tmp_path / "keys.json"))
    plaintext, _ = store.create_key("agent-C")
    ctx = _ctx({"authorization": f"Bearer {plaintext}"})
    assert authenticate(ctx, key_store=store) == plaintext


def test_authenticate_rejects_bad_token(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_API_TOKEN", "msk-master")
    store = KeyStore(path=str(tmp_path / "keys.json"))
    ctx = _ctx({"authorization": "Bearer mk_bad"})
    with pytest.raises(AuthError):
        authenticate(ctx, key_store=store)
