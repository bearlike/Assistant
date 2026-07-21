#!/usr/bin/env python3
"""MongoDB-backed trigger storage driver."""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.triggers.spec import TriggerSpec, TriggerStatus, parse_trigger
from mewbo_core.triggers.store import TriggerStoreBase

logging = get_logger(name="core.triggers.store_mongo")


class MongoTriggerStore(TriggerStoreBase):
    """MongoDB-backed storage for triggers, in a single ``triggers`` collection.

    Documents key on the model's own ``id`` field (not Mongo's ``_id``),
    mirroring ``MongoProjectStore``. Indexed on ``(session_id, status)`` for
    the ``cancel_for_session`` cascade and ``id`` (unique) for point lookups.
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Initialize MongoDB connection and ensure indexes."""
        if uri is None:
            uri = get_config_value("storage", "mongodb", "uri", default="mongodb://localhost:27017")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="mewbo")

        self._client: MongoClient = MongoClient(
            uri, maxPoolSize=10, minPoolSize=2, serverSelectionTimeoutMS=5000
        )
        self._db: Database = self._client[database]

        try:
            self._client.admin.command("ping")
        except Exception as exc:
            raise ConnectionError(
                f"MongoDB is unreachable at the configured URI. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc

        self._col: Collection = self._db["triggers"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Create indexes idempotently on first connection."""
        self._col.create_index(
            [("session_id", ASCENDING), ("status", ASCENDING)],
            name="ix_triggers_session_status",
            background=True,
        )
        self._col.create_index(
            [("id", ASCENDING)],
            name="ix_triggers_id",
            unique=True,
            background=True,
        )

    def create(self, trigger: TriggerSpec) -> TriggerSpec:
        """Insert a new trigger document."""
        self._col.insert_one(trigger.model_dump(mode="json"))
        return trigger

    def get(self, trigger_id: str) -> TriggerSpec | None:
        """Look up a trigger by id."""
        doc = self._col.find_one({"id": trigger_id}, {"_id": 0})
        return parse_trigger(doc) if doc else None

    def list(
        self,
        *,
        session_id: str | None = None,
        kind: str | None = None,
        status: TriggerStatus | None = None,
        limit: int | None = None,
    ) -> list[TriggerSpec]:
        """List triggers, optionally filtered by session/kind/status."""
        query: dict[str, Any] = {}
        if session_id is not None:
            query["session_id"] = session_id
        if kind is not None:
            query["kind"] = kind
        if status is not None:
            query["status"] = status
        cursor = self._col.find(query, {"_id": 0})
        if limit is not None:
            cursor = cursor.limit(limit)
        return [parse_trigger(doc) for doc in cursor]

    def update(self, trigger: TriggerSpec) -> TriggerSpec:
        """Full-document replace by id."""
        result = self._col.find_one_and_update(
            {"id": trigger.id},
            {"$set": trigger.model_dump(mode="json")},
            return_document=True,
            projection={"_id": 0},
        )
        if not result:
            raise KeyError(f"Trigger {trigger.id} not found")
        return parse_trigger(result)


__all__ = ["MongoTriggerStore"]
