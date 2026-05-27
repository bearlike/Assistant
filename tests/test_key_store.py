#!/usr/bin/env python3
"""Tests for the API key store (file and MongoDB drivers)."""

from __future__ import annotations

import json
from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.key_store import (
    KeyStore,
    KeyStoreBase,
    _hash_key,
    create_key_store,
)
from mewbo_core.key_store_mongo import MongoKeyStore

# ---------------------------------------------------------------------------
# Shared lifecycle suite — runs against every driver.
# ---------------------------------------------------------------------------


def _assert_lifecycle(store: KeyStoreBase) -> None:
    """create → verify (accepts) → list (no hash) → revoke → verify (rejects)."""
    plaintext, record = store.create_key("ci-token")
    assert plaintext.startswith("mk_")
    assert record["label"] == "ci-token"
    assert record["revoked_at"] is None
    assert "key_hash" not in record
    assert "id" in record and record["id"]

    # Verify accepts the freshly minted key and never leaks the hash.
    verified = store.verify_key(plaintext)
    assert verified is not None
    assert verified["id"] == record["id"]
    assert "key_hash" not in verified

    # A bogus token is rejected.
    assert store.verify_key("mk_not-a-real-key") is None
    assert store.verify_key("") is None

    # list_keys never leaks the hash or plaintext.
    listed = store.list_keys()
    assert len(listed) == 1
    assert listed[0]["id"] == record["id"]
    assert "key_hash" not in listed[0]
    assert plaintext not in json.dumps(listed)

    # Revoke, then verify now rejects.
    assert store.revoke_key(record["id"]) is True
    assert store.verify_key(plaintext) is None
    listed_after = store.list_keys()
    assert listed_after[0]["revoked_at"] is not None

    # Revoking an unknown id returns False.
    assert store.revoke_key("does-not-exist") is False


# ---------------------------------------------------------------------------
# File driver.
# ---------------------------------------------------------------------------


@pytest.fixture()
def file_store(tmp_path):
    """A KeyStore writing to a temp $MEWBO_HOME/api_keys.json."""
    return KeyStore(path=str(tmp_path / "api_keys.json"))


def test_file_isinstance(file_store):
    """KeyStore is a KeyStoreBase."""
    assert isinstance(file_store, KeyStoreBase)


def test_file_lifecycle(file_store):
    """Full create/verify/list/revoke lifecycle on the file driver."""
    _assert_lifecycle(file_store)


def test_file_revoke_is_idempotent(file_store):
    """Revoking an already-revoked key still returns True (key remains known)."""
    _, record = file_store.create_key("issued")
    key_id = record["id"]
    assert file_store.revoke_key(key_id) is True
    # Second revoke: the key is known but already revoked → still True.
    assert file_store.revoke_key(key_id) is True
    assert file_store.list_keys()[0]["revoked_at"] is not None


def test_file_persists_only_hash(tmp_path):
    """The on-disk file stores the hash, never the plaintext."""
    path = tmp_path / "api_keys.json"
    store = KeyStore(path=str(path))
    plaintext, _ = store.create_key("disk")

    raw = path.read_text(encoding="utf-8")
    assert plaintext not in raw
    assert _hash_key(plaintext) in raw

    # A second store sees the same persisted key.
    reopened = KeyStore(path=str(path))
    assert reopened.verify_key(plaintext) is not None


def test_file_default_path_uses_mewbo_home(tmp_path, monkeypatch):
    """With no explicit path, the store writes to $MEWBO_HOME/api_keys.json."""
    monkeypatch.setenv("MEWBO_HOME", str(tmp_path))
    store = KeyStore()
    assert store.path == str((tmp_path / "api_keys.json").resolve())


def test_factory_returns_file_store_for_json_driver(tmp_path, monkeypatch):
    """create_key_store() returns the file driver when storage.driver is json."""
    monkeypatch.setattr(
        "mewbo_core.key_store.get_config_value",
        lambda *_a, **_k: "json",
    )
    store = create_key_store(path=str(tmp_path / "api_keys.json"))
    assert isinstance(store, KeyStore)


# ---------------------------------------------------------------------------
# Mongo driver (mongomock, mirroring test_session_store_mongo.py).
# ---------------------------------------------------------------------------


@pytest.fixture()
def mongo_store():
    """A MongoKeyStore backed by mongomock."""
    with patch(
        "mewbo_core.key_store_mongo.MongoClient",
        mongomock.MongoClient,
    ):
        store = MongoKeyStore(
            uri="mongodb://localhost:27017",
            database="test_mewbo_keys",
        )
    return store


def test_mongo_isinstance(mongo_store):
    """MongoKeyStore is a KeyStoreBase."""
    assert isinstance(mongo_store, KeyStoreBase)


def test_mongo_lifecycle(mongo_store):
    """Full create/verify/list/revoke lifecycle on the Mongo driver."""
    _assert_lifecycle(mongo_store)


def test_factory_returns_mongo_store_for_mongodb_driver(monkeypatch):
    """create_key_store() returns the Mongo driver when storage.driver is mongodb."""
    monkeypatch.setattr(
        "mewbo_core.key_store.get_config_value",
        lambda *_a, **_k: "mongodb",
    )
    with patch(
        "mewbo_core.key_store_mongo.MongoClient",
        mongomock.MongoClient,
    ):
        store = create_key_store()
    assert isinstance(store, MongoKeyStore)
