#!/usr/bin/env python3
"""MongoDB-backed custom-system-instructions storage driver."""

from __future__ import annotations

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.config import get_config_value
from mewbo_core.system_instructions.spec import GLOBAL_INSTRUCTIONS_ID, SystemInstructionsDoc
from mewbo_core.system_instructions.store import SystemInstructionsStoreBase

logging = get_logger(name="core.system_instructions.store_mongo")


class MongoSystemInstructionsStore(SystemInstructionsStoreBase):
    """MongoDB-backed storage, in a single ``system_instructions`` collection.

    Documents key on the model's own ``id`` field (not Mongo's ``_id``),
    mirroring ``MongoTriggerStore``.
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Initialize the MongoDB connection and ensure indexes."""
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

        self._col: Collection = self._db["system_instructions"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Create indexes idempotently on first connection."""
        self._col.create_index(
            [("id", ASCENDING)],
            name="ix_system_instructions_id",
            unique=True,
            background=True,
        )

    def get(self, doc_id: str = GLOBAL_INSTRUCTIONS_ID) -> SystemInstructionsDoc | None:
        """Look up the document by id."""
        doc = self._col.find_one({"id": doc_id}, {"_id": 0})
        if not doc:
            return None
        try:
            return SystemInstructionsDoc.model_validate(doc)
        except Exception:
            logging.warning(
                "Stored system-instructions document is invalid; ignoring it.", exc_info=True
            )
            return None

    def put(self, doc: SystemInstructionsDoc) -> SystemInstructionsDoc:
        """Upsert the document, stamping ``updated_at``."""
        stamped = doc.model_copy(update={"updated_at": utc_now_iso()})
        self._col.update_one(
            {"id": stamped.id},
            {"$set": stamped.model_dump(mode="json")},
            upsert=True,
        )
        return stamped


__all__ = ["MongoSystemInstructionsStore"]
