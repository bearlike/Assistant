"""Tests for the API key management endpoints and key-based auth."""

# mypy: ignore-errors
import pytest
from mewbo_api import backend
from mewbo_core.key_store import KeyStore


@pytest.fixture()
def key_backend(tmp_path, monkeypatch):
    """Swap in a temp-file KeyStore so tests don't touch the real $MEWBO_HOME."""
    store = KeyStore(path=str(tmp_path / "api_keys.json"))
    monkeypatch.setattr(backend, "key_store", store)
    return store


def _master_headers():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


@pytest.mark.usefixtures("key_backend")
def test_create_key_requires_master_token():
    """POST /api/keys rejects a missing credential."""
    client = backend.app.test_client()
    resp = client.post("/api/keys", json={"label": "x"})
    assert resp.status_code == 401


def test_issued_key_cannot_manage_keys(key_backend):
    """An issued key is rejected on the master-only key-management routes."""
    plaintext, _ = key_backend.create_key("issued")
    client = backend.app.test_client()

    # POST with an issued key → rejected.
    resp = client.post(
        "/api/keys", json={"label": "x"}, headers={"X-API-KEY": plaintext}
    )
    assert resp.status_code == 401

    # GET with an issued key → rejected.
    resp = client.get("/api/keys", headers={"X-API-KEY": plaintext})
    assert resp.status_code == 401


@pytest.mark.usefixtures("key_backend")
def test_create_list_revoke_with_master_token():
    """Master token can mint, list, and revoke keys; plaintext shown once."""
    client = backend.app.test_client()

    # Mint.
    resp = client.post("/api/keys", json={"label": "ci"}, headers=_master_headers())
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["label"] == "ci"
    assert body["key"].startswith("mk_")
    assert body["created_at"]
    key_id = body["id"]

    # List — metadata only, never the hash or plaintext.
    resp = client.get("/api/keys", headers=_master_headers())
    assert resp.status_code == 200
    keys = resp.get_json()["keys"]
    assert any(k["id"] == key_id for k in keys)
    assert all("key_hash" not in k for k in keys)
    assert body["key"] not in resp.get_data(as_text=True)

    # Revoke.
    resp = client.delete(f"/api/keys/{key_id}", headers=_master_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"id": key_id, "revoked": True}

    # Revoking an unknown id → 404.
    resp = client.delete("/api/keys/nope", headers=_master_headers())
    assert resp.status_code == 404


@pytest.mark.usefixtures("key_backend")
def test_create_key_requires_label():
    """POST /api/keys rejects an empty label."""
    client = backend.app.test_client()
    resp = client.post("/api/keys", json={"label": "  "}, headers=_master_headers())
    assert resp.status_code == 400


def test_issued_key_accepted_on_protected_route(key_backend):
    """An issued key authorizes a normal protected route (GET /api/tools)."""
    plaintext, _ = key_backend.create_key("worker")
    client = backend.app.test_client()
    resp = client.get("/api/tools", headers={"X-API-KEY": plaintext})
    assert resp.status_code == 200


def test_revoked_key_rejected_on_protected_route(key_backend):
    """A revoked key no longer authorizes a protected route."""
    plaintext, record = key_backend.create_key("worker")
    key_backend.revoke_key(record["id"])
    client = backend.app.test_client()
    resp = client.get("/api/tools", headers={"X-API-KEY": plaintext})
    assert resp.status_code == 401


@pytest.mark.usefixtures("key_backend")
def test_master_token_still_works_on_protected_route():
    """The master token remains a valid break-glass credential."""
    client = backend.app.test_client()
    resp = client.get("/api/tools", headers=_master_headers())
    assert resp.status_code == 200


def test_issued_key_accepted_via_query_param(key_backend):
    """An issued key passed via ?api_key= authorizes a protected route (SSE path)."""
    plaintext, _ = key_backend.create_key("worker")
    client = backend.app.test_client()
    resp = client.get(f"/api/tools?api_key={plaintext}")
    assert resp.status_code == 200


def test_delete_already_revoked_key_is_idempotent(key_backend):
    """DELETE on an already-revoked key returns 200, not 404."""
    _, record = key_backend.create_key("issued")
    key_id = record["id"]
    client = backend.app.test_client()

    first = client.delete(f"/api/keys/{key_id}", headers=_master_headers())
    assert first.status_code == 200
    assert first.get_json() == {"id": key_id, "revoked": True}

    # Revoking again is idempotent — still a known key, still 200.
    second = client.delete(f"/api/keys/{key_id}", headers=_master_headers())
    assert second.status_code == 200
    assert second.get_json() == {"id": key_id, "revoked": True}
