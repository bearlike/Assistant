#!/usr/bin/env python3
"""MongoDB-backed session storage driver."""

from __future__ import annotations

import os
import uuid

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from meeseeks_core.common import get_logger
from meeseeks_core.config import get_config_value
from meeseeks_core.session_store import SessionStoreBase, _utc_now
from meeseeks_core.types import Event, EventRecord

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
        # Local directory for attachment files.
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

        # MongoDB connection.
        if uri is None:
            uri = get_config_value("storage", "mongodb", "uri", default="mongodb://localhost:27017")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="meeseeks")

        self._client: MongoClient = MongoClient(uri, maxPoolSize=10, minPoolSize=2)
        self._db: Database = self._client[database]
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

    def create_session(self) -> str:
        """Create a new session document and return its identifier."""
        session_id = uuid.uuid4().hex
        self._col("sessions").insert_one(
            {
                "_id": session_id,
                "created_at": _utc_now(),
                "archived_at": None,
                "summary": None,
                "summary_updated_at": None,
            }
        )
        # Create local directory for attachments.
        os.makedirs(os.path.join(self.root_dir, session_id), exist_ok=True)
        return session_id

    def session_dir(self, session_id: str) -> str:
        """Return the local attachment directory for a session."""
        path = os.path.join(self.root_dir, session_id)
        os.makedirs(path, exist_ok=True)
        return path

    def append_event(self, session_id: str, event: Event) -> None:
        """Insert an event document into the events collection."""
        record: EventRecord = {"ts": _utc_now(), **event}
        self._col("events").insert_one({"session_id": session_id, **record})

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all events for a session, sorted by timestamp."""
        cursor = (
            self._col("events")
            .find({"session_id": session_id}, {"_id": 0, "session_id": 0})
            .sort("ts", ASCENDING)
        )
        return list(cursor)

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

    def list_sessions(self) -> list[str]:
        """Return sorted session IDs from the sessions collection."""
        ids = self._col("sessions").distinct("_id")
        return sorted(str(sid) for sid in ids)

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


__all__ = ["MongoSessionStore"]
