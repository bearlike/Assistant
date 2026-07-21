#!/usr/bin/env python3
"""User store — durable ``UserRecord`` persistence with a JIT upsert.

A ``UserStoreBase`` ABC declares the persistence primitives (create/get/lookup/
update/list/delete); the ``upsert`` join used by JIT provisioning is a concrete
template built on top of them, so every backend inherits identical link logic.
Lookups by external subject are what let a returning federated user resolve to
the same record instead of forking a new one.
"""

from __future__ import annotations

import abc
from pathlib import Path

from mewbo_iam.principal import ExternalSubject
from mewbo_iam.stores._json import _JsonCollectionStore
from mewbo_iam.users import UserRecord

_DATA_FILENAME = "iam_users.json"


class UserStoreBase(abc.ABC):
    """Abstract interface for user-record storage backends."""

    @abc.abstractmethod
    def create(self, user: UserRecord) -> UserRecord:
        """Persist a new user record and return it."""

    @abc.abstractmethod
    def get(self, user_id: str) -> UserRecord | None:
        """Return a user by id, or ``None`` if not found."""

    @abc.abstractmethod
    def get_by_external(self, external: ExternalSubject) -> UserRecord | None:
        """Return the user linked to ``external`` (issuer+subject), or ``None``."""

    @abc.abstractmethod
    def update(self, user: UserRecord) -> UserRecord:
        """Full-document replace of an existing user, keyed by ``user.id``.

        Raises ``KeyError`` if no user with that id exists.
        """

    @abc.abstractmethod
    def list(self) -> list[UserRecord]:
        """Return all user records."""

    @abc.abstractmethod
    def delete(self, user_id: str) -> bool:
        """Delete a user by id. Returns ``True`` if one was removed."""

    # -- concrete template -------------------------------------------------

    def upsert(self, user: UserRecord) -> UserRecord:
        """Link-or-create by external subject — the JIT provisioning join.

        If any of ``user``'s external identities already resolves to a stored
        record, that record is updated in place (its ``id`` and ``created_at``
        are preserved so downstream ownership stamps stay valid). Otherwise the
        user is created as-is. Matching on the external subject — not email — is
        what makes the join survive an email change.
        """
        for external in user.external_identities:
            existing = self.get_by_external(external)
            if existing is not None:
                merged = user.model_copy(
                    update={"id": existing.id, "created_at": existing.created_at}
                )
                return self.update(merged)
        return self.create(user)


class JsonUserStore(_JsonCollectionStore, UserStoreBase):
    """JSON file-backed user store — one flat file, list-of-records."""

    _FILENAME = _DATA_FILENAME
    _MONGO_DRIVER = "MongoUserStore"

    def create(self, user: UserRecord) -> UserRecord:
        """Append a new user record."""
        with self._lock:
            records = self._read()
            records.append(user.model_dump(mode="json"))
            self._write(records)
        return user

    def get(self, user_id: str) -> UserRecord | None:
        """Look up a user by id."""
        with self._lock:
            records = self._read()
        for record in records:
            if record.get("id") == user_id:
                return UserRecord.model_validate(record)
        return None

    def get_by_external(self, external: ExternalSubject) -> UserRecord | None:
        """Look up a user by one of its external identities."""
        with self._lock:
            records = self._read()
        for record in records:
            for identity in record.get("external_identities", []):
                if (
                    identity.get("issuer") == external.issuer
                    and identity.get("subject") == external.subject
                ):
                    return UserRecord.model_validate(record)
        return None

    def update(self, user: UserRecord) -> UserRecord:
        """Full-document replace by id."""
        with self._lock:
            records = self._read()
            for index, record in enumerate(records):
                if record.get("id") == user.id:
                    records[index] = user.model_dump(mode="json")
                    self._write(records)
                    return user
        raise KeyError(f"User {user.id} not found")

    def list(self) -> list[UserRecord]:
        """Return all user records."""
        with self._lock:
            records = self._read()
        return [UserRecord.model_validate(record) for record in records]

    def delete(self, user_id: str) -> bool:
        """Delete a user by id."""
        with self._lock:
            records = self._read()
            remaining = [r for r in records if r.get("id") != user_id]
            if len(remaining) == len(records):
                return False
            self._write(remaining)
        return True


def create_user_store(path: str | Path | None = None) -> UserStoreBase:
    """Return the configured user store driver (json or mongodb)."""
    return JsonUserStore.resolve_driver(path)


__all__ = ["UserStoreBase", "JsonUserStore", "create_user_store"]
