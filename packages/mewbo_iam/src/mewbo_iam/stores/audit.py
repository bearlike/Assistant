#!/usr/bin/env python3
"""Auth-audit store — append-only persistence for the ``AuthAuditEvent`` union.

Audit is write-once: :meth:`append` adds an event, and :meth:`list` reads them
back (newest first) with optional filters for the ``audit.read`` surface. There
is no update or delete — a mutable audit trail is not an audit trail.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from mewbo_iam.audit import AuthAuditEvent, parse_audit_event
from mewbo_iam.stores._json import _JsonCollectionStore

_DATA_FILENAME = "iam_auth_audit.json"


class AuthAuditStoreBase(abc.ABC):
    """Abstract interface for auth-audit storage backends."""

    @abc.abstractmethod
    def append(self, event: AuthAuditEvent) -> AuthAuditEvent:
        """Persist one audit event (append-only) and return it."""

    @abc.abstractmethod
    def _stored_records(
        self, *, actor_subject: str | None, type: str | None
    ) -> Sequence[Mapping[str, Any]]:
        """Raw stored records matching the exact-match filters, in ANY order.

        Only the two exact-value filters are pushed down to the backend; time
        ordering and windowing are the base's job (see :meth:`list`).
        """

    # -- concrete template -------------------------------------------------

    def list(
        self,
        *,
        actor_subject: str | None = None,
        type: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[AuthAuditEvent]:
        """Return events newest-first, optionally filtered by actor/type/time.

        **Ordering happens HERE, on parsed instants, never in a backend query.**
        ``ts`` is persisted as an ISO-8601 string, so a store-side sort on that
        field is lexicographic: ``...Z`` and ``...+00:00`` spellings of the same
        instant order by their text, not their time. Sorting after the parse is
        what makes every driver agree — and ``limit`` is applied last, on top of
        the correct order, so "newest N" really is the newest N.
        """
        records = self._stored_records(actor_subject=actor_subject, type=type)
        events: list[AuthAuditEvent] = [parse_audit_event(record) for record in records]
        events.sort(key=lambda event: event.ts, reverse=True)
        if since is not None:
            events = [event for event in events if event.ts >= since]
        if limit is not None:
            events = events[:limit]
        return events


class JsonAuthAuditStore(_JsonCollectionStore, AuthAuditStoreBase):
    """JSON file-backed, append-only audit store."""

    _FILENAME = _DATA_FILENAME
    _MONGO_DRIVER = "MongoAuthAuditStore"

    def append(self, event: AuthAuditEvent) -> AuthAuditEvent:
        """Append one event to the trail."""
        with self._lock:
            records = self._read()
            records.append(event.model_dump(mode="json"))
            self._write(records)
        return event

    def _stored_records(
        self, *, actor_subject: str | None, type: str | None
    ) -> Sequence[Mapping[str, Any]]:
        """Every stored record, filtered on the raw dict BEFORE parsing.

        The discriminator lives only on the concrete variants, so matching it
        here — rather than on parsed events — keeps the filter off the base
        envelope's type.
        """
        with self._lock:
            records = self._read()
        if actor_subject is not None:
            records = [record for record in records if record.get("actor_subject") == actor_subject]
        if type is not None:
            records = [record for record in records if record.get("type") == type]
        return records


def create_auth_audit_store(path: str | Path | None = None) -> AuthAuditStoreBase:
    """Return the configured auth-audit store driver (json or mongodb)."""
    return JsonAuthAuditStore.resolve_driver(path)


__all__ = ["AuthAuditStoreBase", "JsonAuthAuditStore", "create_auth_audit_store"]
