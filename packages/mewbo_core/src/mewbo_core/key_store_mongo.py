#!/usr/bin/env python3
"""MongoDB-backed API key storage driver."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

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
        # ``database`` is a str here (the None branch above resolves it from
        # config, which defaults to a str) — narrow it for the ``__getitem__``.
        self._db: Database = self._client[cast("str", database)]

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
        """Mint a new key — the legacy, unscoped form of :meth:`create_scoped_key`.

        Every optional identity/authorization keyword omitted is exactly
        what makes a record indistinguishable from a legacy key (see
        :meth:`create_scoped_key`'s docstring), so this delegates rather than
        re-implementing the mint-insert sequence a second time.
        """
        return self.create_scoped_key(label)

    def create_scoped_key(
        self,
        label: str,
        *,
        owner_subject: str | None = None,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        team_id: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[str, PublicKeyRecord]:
        """Mint a new key, optionally stamped with identity/authorization fields."""
        plaintext, record = self._mint_scoped_record(
            label,
            "_id",
            owner_subject=owner_subject,
            roles=roles,
            scopes=scopes,
            team_id=team_id,
            expires_at=expires_at,
        )
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

    def resolve_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the matched active, unexpired record, else ``None``.

        Mirrors :meth:`verify_key` but also drops a matched record past its
        ``expires_at`` (the shared base helper owns the expiry rule).
        """
        active = (self._normalize(doc) for doc in self._col().find({"revoked_at": None}))
        return self._match_active_unexpired(plaintext, active, datetime.now(timezone.utc))

    def rotate_key(self, key_id: str) -> tuple[str, PublicKeyRecord] | None:
        """Mint a replacement for *key_id* and revoke the original. ``None`` if unknown.

        Atomicity: Mongo has no single write spanning two documents without a
        (replica-set-only) multi-document transaction, which this deployment
        does not assume is available — so this is "atomic-enough", not atomic.
        It INSERTS the replacement FIRST, then revokes the source — the
        fail-open order: a crash between the two steps leaves BOTH keys usable
        rather than leaving the caller with none (a stranded active key is
        recoverable by revoking it by hand; a stranded caller with no working
        key is not). The source is re-fetched fresh on each call, so retrying a
        failed rotation is safe.
        """
        source = self._col().find_one({"_id": key_id})
        if source is None:
            return None
        plaintext, new_record = self._mint_rotated_record(self._normalize(source), id_field="_id")
        self._col().insert_one(new_record)
        self._col().update_one(
            {"_id": key_id, "revoked_at": None}, {"$set": {"revoked_at": utc_now_iso()}}
        )
        return plaintext, _public_record(self._normalize(new_record))

    def list_keys_for_owner(self, subject: str) -> list[PublicKeyRecord]:
        """Metadata for every key owned by *subject* — server-side filtered.

        Overrides the generic base-class loop: Mongo pushes the
        ``owner_subject`` filter into the query instead of listing every key in
        the store and filtering in Python.
        """
        docs = self._col().find({"owner_subject": subject}, {"key_hash": 0}).sort("created_at", 1)
        return [self._normalize(doc) for doc in docs]

    def revoke_keys_for_owner(self, subject: str) -> int:
        """Revoke every ACTIVE key owned by *subject* in one write. Returns the count.

        A single ``update_many`` is strictly more atomic than the base class's
        generic list-then-loop: there is no window between reading the owner's
        key list and revoking each one individually where a concurrent key
        creation for that owner could be missed or double-counted — the filter
        and the write are one server-side operation.
        """
        result = self._col().update_many(
            {"owner_subject": subject, "revoked_at": None},
            {"$set": {"revoked_at": utc_now_iso()}},
        )
        return int(result.modified_count)

    @staticmethod
    def _normalize(doc: Mapping[str, Any]) -> KeyRecord:
        """Map the Mongo ``_id`` to the public ``id`` field.

        Accepts any ``Mapping`` (a stored dict OR a freshly minted ``KeyRecord``)
        and returns the record shape callers persist/return; the optional
        identity fields (owner/roles/scopes/team/expiry) pass through untouched.
        """
        out = dict(doc)
        if "_id" in out:
            out["id"] = out.pop("_id")
        return cast("KeyRecord", out)


__all__ = ["MongoKeyStore"]
