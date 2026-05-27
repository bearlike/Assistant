#!/usr/bin/env python3
"""MongoDB-backed API key storage driver."""

from __future__ import annotations

from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.config import get_config_value
from mewbo_core.key_store import KeyRecord, KeyStoreBase, PublicKeyRecord, _public_record

logging = get_logger(name="core.key_store_mongo")


class MongoKeyStore(KeyStoreBase):
    """MongoDB-backed storage for API keys.

    Uses a single ``api_keys`` collection. Mirrors the connection and config
    handling of ``MongoSessionStore``.
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Initialize the MongoDB connection."""
        if uri is None:
            uri = get_config_value("storage", "mongodb", "uri", default="mongodb://localhost:27017")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="mewbo")

        self._client: MongoClient = MongoClient(
            uri, maxPoolSize=10, minPoolSize=2, serverSelectionTimeoutMS=5000
        )
        self._db: Database = self._client[database]

        # Fail fast: verify MongoDB is reachable before continuing.
        try:
            self._client.admin.command("ping")
        except Exception as exc:
            raise ConnectionError(
                f"MongoDB is unreachable at the configured URI. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc

    def _col(self) -> Collection:
        """Return the api_keys collection."""
        return self._db["api_keys"]

    def create_key(self, label: str) -> tuple[str, PublicKeyRecord]:
        """Mint a new key and insert its record."""
        plaintext, record = self._mint_record(label, id_field="_id")
        self._col().insert_one(record)
        return plaintext, _public_record(self._normalize(record))

    def list_keys(self) -> list[PublicKeyRecord]:
        """Return metadata for all keys (no hashes)."""
        docs = self._col().find({}, {"key_hash": 0}).sort("created_at", 1)
        return [self._normalize(doc) for doc in docs]

    def revoke_key(self, key_id: str) -> bool:
        """Mark a key revoked. Returns ``True`` if a matching key was found."""
        result = self._col().update_one(
            {"_id": key_id, "revoked_at": None},
            {"$set": {"revoked_at": utc_now_iso()}},
        )
        if result.matched_count:
            return True
        # Already revoked but present → still a known key.
        return self._col().count_documents({"_id": key_id}, limit=1) > 0

    def verify_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the active record matching ``plaintext``, else ``None``.

        Mirrors the file driver: fetch the active (non-revoked) records and
        compare each stored hash against the candidate with
        ``hmac.compare_digest`` rather than relying on a DB-equality filter.
        """
        active = (self._normalize(doc) for doc in self._col().find({"revoked_at": None}))
        return self._match_active(plaintext, active)

    @staticmethod
    def _normalize(doc: dict[str, Any]) -> KeyRecord:
        """Map the Mongo ``_id`` to the public ``id`` field."""
        out = dict(doc)
        if "_id" in out:
            out["id"] = out.pop("_id")
        return out


__all__ = ["MongoKeyStore"]
