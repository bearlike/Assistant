#!/usr/bin/env python3
"""Role store — persists ``RoleRecord``s; seeds and protects the built-ins.

The store seeds :data:`BUILTIN_ROLES` on first load and refuses to update or
delete a built-in name — that guard is a concrete template on the base, so every
backend enforces it identically. Custom roles go through the same ``upsert``/
``delete`` after clearing the guard.
"""

from __future__ import annotations

import abc
from pathlib import Path

from mewbo_iam.roles import BUILTIN_ROLE_NAMES, BUILTIN_ROLES, RoleRecord
from mewbo_iam.stores._json import _JsonCollectionStore

_DATA_FILENAME = "iam_roles.json"


class RoleStoreBase(abc.ABC):
    """Abstract interface for role storage backends.

    Subclasses implement the raw persistence primitives (``_insert``/``_replace``/
    ``_remove``/``get``/``list``); the built-in seeding and the immutable-builtin
    guard live here so no backend can forget them.
    """

    @abc.abstractmethod
    def _insert(self, role: RoleRecord) -> RoleRecord:
        """Persist a not-yet-existing role."""

    @abc.abstractmethod
    def _replace(self, role: RoleRecord) -> RoleRecord:
        """Overwrite an existing role by name. Raises ``KeyError`` if absent."""

    @abc.abstractmethod
    def _remove(self, name: str) -> bool:
        """Delete a role by name. Returns ``True`` if one was removed."""

    @abc.abstractmethod
    def get(self, name: str) -> RoleRecord | None:
        """Return a role by name, or ``None``."""

    @abc.abstractmethod
    def list(self) -> list[RoleRecord]:
        """Return all roles."""

    # -- concrete template methods ----------------------------------------

    def seed_builtins(self) -> None:
        """Insert any missing built-in roles. Idempotent — safe every startup."""
        for role in BUILTIN_ROLES:
            if self.get(role.name) is None:
                self._insert(role)

    def upsert(self, role: RoleRecord) -> RoleRecord:
        """Create or replace a custom role. Rejects any built-in name."""
        if role.name in BUILTIN_ROLE_NAMES:
            raise ValueError(f"cannot modify built-in role {role.name!r}")
        if self.get(role.name) is None:
            return self._insert(role)
        return self._replace(role)

    def delete(self, name: str) -> bool:
        """Delete a custom role. Rejects any built-in name."""
        if name in BUILTIN_ROLE_NAMES:
            raise ValueError(f"cannot delete built-in role {name!r}")
        return self._remove(name)


class JsonRoleStore(_JsonCollectionStore, RoleStoreBase):
    """JSON file-backed role store."""

    _FILENAME = _DATA_FILENAME
    _MONGO_DRIVER = "MongoRoleStore"

    def _insert(self, role: RoleRecord) -> RoleRecord:
        """Append a new role record."""
        with self._lock:
            records = self._read()
            records.append(role.model_dump(mode="json"))
            self._write(records)
        return role

    def _replace(self, role: RoleRecord) -> RoleRecord:
        """Overwrite an existing role by name."""
        with self._lock:
            records = self._read()
            for index, record in enumerate(records):
                if record.get("name") == role.name:
                    records[index] = role.model_dump(mode="json")
                    self._write(records)
                    return role
        raise KeyError(f"Role {role.name} not found")

    def _remove(self, name: str) -> bool:
        """Delete a role by name."""
        with self._lock:
            records = self._read()
            remaining = [r for r in records if r.get("name") != name]
            if len(remaining) == len(records):
                return False
            self._write(remaining)
        return True

    def get(self, name: str) -> RoleRecord | None:
        """Look up a role by name."""
        with self._lock:
            records = self._read()
        for record in records:
            if record.get("name") == name:
                return RoleRecord.model_validate(record)
        return None

    def list(self) -> list[RoleRecord]:
        """Return all roles."""
        with self._lock:
            records = self._read()
        return [RoleRecord.model_validate(record) for record in records]


def create_role_store(path: str | Path | None = None) -> RoleStoreBase:
    """Return the configured role store driver, with built-ins seeded."""
    store: RoleStoreBase = JsonRoleStore.resolve_driver(path)
    store.seed_builtins()
    return store


__all__ = ["RoleStoreBase", "JsonRoleStore", "create_role_store"]
