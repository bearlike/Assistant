#!/usr/bin/env python3
"""MongoDB-backed session storage driver."""

from __future__ import annotations

import os
import uuid

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.session_store import SessionStoreBase, _utc_now
from mewbo_core.types import Event, EventRecord

logging = get_logger(name="core.session_store_mongo")


class MongoSessionStore(SessionStoreBase):
    """MongoDB-backed storage for session transcripts and summaries.

    Uses three collections:

    - ``sessions``: session metadata (created_at, archived_at, summary).
    - ``events``: append-only event log indexed by ``(session_id, ts)``.
    - ``tags``: tag-name → session-id mapping.

    A local ``root_dir`` is still maintained for binary attachment file
    storage (uploaded via the API, read by ``ContextBuilder``).
    """

    def __init__(
        self,
        root_dir: str | None = None,
        *,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Initialize MongoDB connection and local attachment directory."""
        super().__init__()
        # Local directory for attachment files.
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

        # MongoDB connection.
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

        self._ensure_indexes()

    # -- helpers ------------------------------------------------------------

    def _col(self, name: str) -> Collection:
        """Return a MongoDB collection by name."""
        return self._db[name]

    def _ensure_indexes(self) -> None:
        """Create indexes idempotently on first connection."""
        self._col("events").create_index(
            [("session_id", ASCENDING), ("ts", ASCENDING)],
            name="ix_events_session_ts",
            background=True,
        )

    # -- abstract implementations -------------------------------------------

    def create_session(self, owner: str | None = None) -> str:
        """Create a new session document and return its identifier."""
        session_id = uuid.uuid4().hex
        self.ensure_session(session_id, owner)
        return session_id

    def ensure_session(self, session_id: str, owner: str | None = None) -> None:
        """Idempotently materialise the ``sessions`` document for a known id.

        ``append_event`` only writes the ``events`` collection, so a session
        whose id was minted outside ``create_session`` (the realtime recorder)
        has events but no ``sessions`` document — invisible to ``list_sessions``,
        which reads the ``sessions`` collection. This upsert creates the record
        once. ``$setOnInsert`` means a replay never resets ``created_at`` or
        clears an ``archived_at`` already set on the existing doc (true no-op) —
        and it is what makes ``owner_subject`` set-once for free, so a second
        ``ensure_session`` can never re-point an existing session at a new owner.
        """
        self._col("sessions").update_one(
            {"_id": session_id},
            {
                "$setOnInsert": {
                    "created_at": _utc_now(),
                    "archived_at": None,
                    "terminated_at": None,
                    "owner_subject": owner,
                    "summary": None,
                    "summary_updated_at": None,
                    "title": None,
                    "title_updated_at": None,
                }
            },
            upsert=True,
        )
        # Create local directory for attachments.
        os.makedirs(os.path.join(self.root_dir, session_id), exist_ok=True)

    def session_dir(self, session_id: str) -> str:
        """Return the local attachment directory for a session."""
        path = os.path.join(self.root_dir, session_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _write_event(self, session_id: str, event: Event) -> None:
        """Insert an event document into the events collection, unconditionally."""
        record: EventRecord = {"ts": _utc_now(), **event}
        self._col("events").insert_one({"session_id": session_id, **record})
        self._publish_appended(session_id, record)

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all events for a session, sorted by timestamp."""
        cursor = (
            self._col("events")
            .find({"session_id": session_id}, {"_id": 0, "session_id": 0})
            .sort("ts", ASCENDING)
        )
        return list(cursor)

    def load_recent_events(
        self,
        session_id: str,
        limit: int = 8,
        include_types: set[str] | None = None,
    ) -> list[EventRecord]:
        """Tail the events collection with a BOUNDED query.

        Overrides the base template method, which materialises the whole
        transcript before slicing — an O(transcript) read per call. A caller
        that only wants the last ``limit`` events (the startup run sweep) instead
        pays a single indexed range read: sort ``ts`` DESC + ``limit`` at the
        store, then reverse to restore ascending order. ``include_types`` filters
        server-side. Mirrors the targeted-query overrides below
        (``last_attestation_hash``/``tags_for_session``).
        """
        if limit <= 0:
            return []
        query: dict[str, object] = {"session_id": session_id}
        if include_types:
            query["type"] = {"$in": list(include_types)}
        cursor = (
            self._col("events")
            .find(query, {"_id": 0, "session_id": 0})
            .sort("ts", DESCENDING)
            .limit(limit)
        )
        events = list(cursor)
        events.reverse()
        return events

    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Delete all events with ``ts > cutoff_ts``."""
        result = self._col("events").delete_many(
            {"session_id": session_id, "ts": {"$gt": cutoff_ts}}
        )
        return result.deleted_count

    def save_summary(self, session_id: str, summary: str) -> None:
        """Upsert the summary field on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {
                "$set": {
                    "summary": summary,
                    "summary_updated_at": _utc_now(),
                }
            },
            upsert=True,
        )

    def load_summary(self, session_id: str) -> str | None:
        """Load the summary field from the session document."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"summary": 1})
        if doc is None:
            return None
        return doc.get("summary")

    def save_title(self, session_id: str, title: str) -> None:
        """Upsert the title field on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {
                "$set": {
                    "title": title,
                    "title_updated_at": _utc_now(),
                }
            },
            upsert=True,
        )

    def load_title(self, session_id: str) -> str | None:
        """Load the title field from the session document."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"title": 1})
        if doc is None:
            return None
        title = doc.get("title")
        return title if isinstance(title, str) and title else None

    def list_sessions(self, owner: str | None = None) -> list[str]:
        """Return sorted session IDs, narrowed to what *owner* may see.

        The ``owner_subject: None`` arm of the filter is doing double duty:
        Mongo equality to ``null`` also matches a MISSING field, so it selects
        both a session explicitly stamped unowned and one whose document
        predates the field entirely — the same property ``terminate_session``
        relies on. Without it every session already on disk would disappear
        from its own creator's list.
        """
        query: dict[str, object] = {}
        if owner is not None:
            query = {"$or": [{"owner_subject": owner}, {"owner_subject": None}]}
        ids = self._col("sessions").distinct("_id", query)
        return sorted(str(sid) for sid in ids)

    def get_owner(self, session_id: str) -> str | None:
        """Return the session's ``owner_subject``, or ``None`` if unowned."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"owner_subject": 1})
        if doc is None:
            return None
        subject = doc.get("owner_subject")
        return subject if isinstance(subject, str) else None

    def tag_session(self, session_id: str, tag: str) -> None:
        """Upsert a tag → session_id mapping in the tags collection."""
        self._col("tags").update_one(
            {"_id": tag},
            {"$set": {"session_id": session_id}},
            upsert=True,
        )

    def resolve_tag(self, tag: str) -> str | None:
        """Look up a tag and return the associated session ID."""
        doc = self._col("tags").find_one({"_id": tag})
        if doc is None:
            return None
        return doc.get("session_id")

    def list_tags(self) -> dict[str, str]:
        """Return all tag → session_id mappings."""
        return {
            doc["_id"]: doc["session_id"]
            for doc in self._col("tags").find({}, {"_id": 1, "session_id": 1})
        }

    def last_attestation_hash(self, session_id: str) -> str:
        """Targeted query over the events collection.

        Overrides the base reverse-scan so recovery issues one filtered/sorted
        lookup instead of loading the whole transcript — mirrors the
        ``tags_for_session`` override.
        """
        from mewbo_core.attestation import GENESIS_HASH

        docs = list(
            self._col("events")
            .find({"session_id": session_id, "type": "attestation"})
            .sort("ts", DESCENDING)
            .limit(1)
        )
        if docs:
            payload = docs[0].get("payload")
            if isinstance(payload, dict):
                record_hash = payload.get("record_hash")
                if isinstance(record_hash, str) and record_hash:
                    return record_hash
        return GENESIS_HASH

    def tags_for_session(self, session_id: str) -> list[str]:
        """Tags pointing at a session via a targeted query.

        Overrides the base reverse-scan so ``list_sessions`` (which calls this
        per session) issues one filtered lookup instead of loading every tag.
        """
        return [
            str(doc["_id"])
            for doc in self._col("tags").find({"session_id": session_id}, {"_id": 1})
        ]

    def archive_session(self, session_id: str) -> None:
        """Set the archived_at timestamp on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {"$set": {"archived_at": _utc_now()}},
        )

    def unarchive_session(self, session_id: str) -> None:
        """Clear the archived_at field on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {"$set": {"archived_at": None}},
        )

    def is_archived(self, session_id: str) -> bool:
        """Check whether the session has a non-null archived_at field."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"archived_at": 1})
        if doc is None:
            return False
        return doc.get("archived_at") is not None

    def terminate_session(self, session_id: str) -> bool:
        """Set ``terminated_at`` once, only while it is still unset.

        The ``terminated_at: None`` filter clause makes the write set-once —
        Mongo equality to ``null`` also matches a missing field, so a doc
        predating this field still qualifies, while a second call finds the
        stamp already present and no-ops (keeping the original time stable).
        ``modified_count`` is the arbitration signal: it is 1 only for the
        call whose filtered update actually flipped ``None`` -> a timestamp.
        """
        result = self._col("sessions").update_one(
            {"_id": session_id, "terminated_at": None},
            {"$set": {"terminated_at": _utc_now()}},
        )
        self._mark_terminated_cached(session_id)
        return result.modified_count > 0

    def get_terminated_at(self, session_id: str) -> str | None:
        """Return the session's ``terminated_at`` timestamp, or ``None``."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"terminated_at": 1})
        if doc is None:
            return None
        return doc.get("terminated_at")


__all__ = ["MongoSessionStore"]
