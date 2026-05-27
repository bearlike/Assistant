#!/usr/bin/env python3
"""API key storage and verification.

Provides a ``KeyStoreBase`` ABC, a filesystem-backed ``KeyStore``
implementation, and a ``create_key_store()`` factory that returns the
configured driver (json or mongodb) — mirroring ``session_store.py``.

Keys are identity-only, full-power, and individually revocable. The
plaintext token is high-entropy (``secrets.token_urlsafe``) so it is hashed
with a single unsalted SHA-256 pass (NOT bcrypt/argon2 — those defend against
low-entropy passwords, which is not the threat model here). Only the hash is
persisted; the plaintext is returned exactly once, at creation.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Iterable
from typing import TypedDict

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.config import get_config_value, resolve_mewbo_home

logging = get_logger(name="core.key_store")

_KEY_PREFIX = "mk_"


class KeyRecord(TypedDict):
    """A persisted API key record (includes the secret hash)."""

    id: str
    label: str
    key_hash: str
    created_at: str
    revoked_at: str | None


class PublicKeyRecord(TypedDict):
    """An API key record with the secret hash stripped — safe to return."""

    id: str
    label: str
    created_at: str
    revoked_at: str | None


def _generate_key() -> str:
    """Return a fresh high-entropy plaintext API key."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(plaintext: str) -> str:
    """Hash a plaintext key with SHA-256 (hex digest)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _public_record(record: KeyRecord) -> PublicKeyRecord:
    """Strip the secret hash from a stored record before returning it."""
    return {k: v for k, v in record.items() if k != "key_hash"}  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class KeyStoreBase(abc.ABC):
    """Abstract interface for API key storage backends.

    A persisted record is ``{id, label, key_hash, created_at, revoked_at}``.
    ``created_at``/``revoked_at`` are ISO-8601 UTC strings; ``revoked_at`` is
    ``None`` while the key is active. Subclasses implement only persistence;
    the shared key-minting and hash-comparison logic lives here.
    """

    @staticmethod
    def _mint_record(label: str, id_field: str = "id") -> tuple[str, KeyRecord]:
        """Build a fresh ``(plaintext, persisted_record)`` pair.

        ``id_field`` is the record's primary-key name (``"id"`` for the file
        driver, ``"_id"`` for Mongo). The plaintext is returned once and only
        its hash is stored.
        """
        plaintext = _generate_key()
        record = {
            id_field: uuid.uuid4().hex,
            "label": label,
            "key_hash": _hash_key(plaintext),
            "created_at": utc_now_iso(),
            "revoked_at": None,
        }
        return plaintext, record  # type: ignore[return-value]

    @staticmethod
    def _match_active(
        plaintext: str, active_records: Iterable[KeyRecord]
    ) -> PublicKeyRecord | None:
        """Return the public form of the active record matching ``plaintext``.

        ``active_records`` must already exclude revoked keys. Compares each
        stored hash with ``hmac.compare_digest`` to avoid timing leaks.
        """
        if not plaintext:
            return None
        candidate = _hash_key(plaintext)
        for record in active_records:
            if hmac.compare_digest(record.get("key_hash", ""), candidate):
                return _public_record(record)
        return None

    @abc.abstractmethod
    def create_key(self, label: str) -> tuple[str, PublicKeyRecord]:
        """Mint a new key.

        Returns ``(plaintext_key, record_without_hash)``. The plaintext is
        returned exactly once and is never persisted or recoverable.
        """

    @abc.abstractmethod
    def list_keys(self) -> list[PublicKeyRecord]:
        """Return metadata for all keys — never the hash or plaintext."""

    @abc.abstractmethod
    def revoke_key(self, key_id: str) -> bool:
        """Revoke a key by ID. Returns ``True`` if a key was revoked."""

    @abc.abstractmethod
    def verify_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the active record matching ``plaintext``, else ``None``.

        A revoked key never matches. The returned record excludes the hash.
        """


# ---------------------------------------------------------------------------
# Filesystem (JSON) driver
# ---------------------------------------------------------------------------


class KeyStore(KeyStoreBase):
    """Filesystem-backed API key storage.

    Persists to ``$MEWBO_HOME/api_keys.json`` (the writable data dir, resolved
    via :func:`resolve_mewbo_home`). Writes are atomic (tmp file + ``os.replace``)
    to avoid corrupting the file on concurrent or interrupted writes.
    """

    def __init__(self, path: str | None = None) -> None:
        """Initialize the store, ensuring the parent directory exists."""
        if path is None:
            path = str(resolve_mewbo_home() / "api_keys.json")
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _load(self) -> list[KeyRecord]:
        """Load all key records from disk, or an empty list."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        keys = data.get("keys", [])
        return keys if isinstance(keys, list) else []

    def _save(self, keys: list[KeyRecord]) -> None:
        """Persist key records atomically (tmp file + replace)."""
        tmp = f"{self.path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"keys": keys}, handle, indent=2)
        os.replace(tmp, self.path)

    def create_key(self, label: str) -> tuple[str, PublicKeyRecord]:
        """Mint a new key and append its record to the store."""
        plaintext, record = self._mint_record(label)
        keys = self._load()
        keys.append(record)
        self._save(keys)
        return plaintext, _public_record(record)

    def list_keys(self) -> list[PublicKeyRecord]:
        """Return metadata for all keys (no hashes)."""
        return [_public_record(record) for record in self._load()]

    def revoke_key(self, key_id: str) -> bool:
        """Mark a key revoked. Returns ``True`` if a matching key was found."""
        keys = self._load()
        for record in keys:
            if record.get("id") == key_id:
                if record.get("revoked_at") is None:
                    record["revoked_at"] = utc_now_iso()
                    self._save(keys)
                return True
        return False

    def verify_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the active record matching ``plaintext``, else ``None``."""
        active = (r for r in self._load() if r.get("revoked_at") is None)
        return self._match_active(plaintext, active)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_key_store(path: str | None = None) -> KeyStoreBase:
    """Return the configured key store driver.

    Reuses the same ``storage.driver`` config the session store reads so the
    key store follows the same backend as sessions (json or mongodb).

    ``path`` is honored only by the json driver (the on-disk store location);
    it is ignored when the configured driver is ``mongodb``.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.key_store_mongo import MongoKeyStore

        try:
            return MongoKeyStore()
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return KeyStore(path=path)


__all__ = [
    "KeyRecord",
    "PublicKeyRecord",
    "KeyStoreBase",
    "KeyStore",
    "create_key_store",
]
