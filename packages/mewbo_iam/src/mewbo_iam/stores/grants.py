#!/usr/bin/env python3
"""Access-grant store — the explicit shares ``AccessDecider`` reads.

Grants are value records (no surrogate id): a grant is identified by its
``(resource_kind, resource_id, grantee, level)`` tuple, so re-granting the same
share is idempotent and revoking matches on value. The read used at every
enforcement site is :meth:`list_for_resource`.
"""

from __future__ import annotations

import abc
from pathlib import Path

from mewbo_iam.access import AccessGrant, Grantee
from mewbo_iam.stores._json import _JsonCollectionStore

_DATA_FILENAME = "iam_access_grants.json"


class AccessGrantStoreBase(abc.ABC):
    """Abstract interface for access-grant storage backends."""

    @abc.abstractmethod
    def grant(self, grant: AccessGrant) -> AccessGrant:
        """Persist a grant (idempotent on its value tuple) and return it."""

    @abc.abstractmethod
    def revoke(self, grant: AccessGrant) -> bool:
        """Remove a matching grant. Returns ``True`` if one was removed."""

    @abc.abstractmethod
    def list_for_resource(self, resource_kind: str, resource_id: str) -> list[AccessGrant]:
        """Every grant on one resource — the ``AccessDecider`` input."""

    @abc.abstractmethod
    def list_for_grantee(self, grantee: Grantee) -> list[AccessGrant]:
        """Every grant held by one user or team."""


class JsonAccessGrantStore(_JsonCollectionStore, AccessGrantStoreBase):
    """JSON file-backed access-grant store."""

    _FILENAME = _DATA_FILENAME
    _MONGO_DRIVER = "MongoAccessGrantStore"

    def grant(self, grant: AccessGrant) -> AccessGrant:
        """Append a grant, skipping an exact duplicate (idempotent)."""
        payload = grant.model_dump(mode="json")
        with self._lock:
            records = self._read()
            if payload not in records:
                records.append(payload)
                self._write(records)
        return grant

    def revoke(self, grant: AccessGrant) -> bool:
        """Remove a grant matching the given value exactly."""
        payload = grant.model_dump(mode="json")
        with self._lock:
            records = self._read()
            remaining = [r for r in records if r != payload]
            if len(remaining) == len(records):
                return False
            self._write(remaining)
        return True

    def list_for_resource(self, resource_kind: str, resource_id: str) -> list[AccessGrant]:
        """Every grant on one resource."""
        with self._lock:
            records = self._read()
        return [
            AccessGrant.model_validate(record)
            for record in records
            if record.get("resource_kind") == resource_kind
            and record.get("resource_id") == resource_id
        ]

    def list_for_grantee(self, grantee: Grantee) -> list[AccessGrant]:
        """Every grant held by one user or team."""
        with self._lock:
            records = self._read()
        matches: list[AccessGrant] = []
        for record in records:
            recorded = record.get("grantee", {})
            if recorded.get("kind") == grantee.kind and recorded.get("id") == grantee.id:
                matches.append(AccessGrant.model_validate(record))
        return matches


def create_access_grant_store(path: str | Path | None = None) -> AccessGrantStoreBase:
    """Return the configured access-grant store driver (json or mongodb)."""
    return JsonAccessGrantStore.resolve_driver(path)


__all__ = ["AccessGrantStoreBase", "JsonAccessGrantStore", "create_access_grant_store"]
