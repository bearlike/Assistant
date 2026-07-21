#!/usr/bin/env python3
"""Custom system-instructions storage — an ABC plus a JSON file-backed driver.

Mirrors ``triggers/store.py``: a couple of abstract primitives (``get``/``put``)
plus a concrete template method (``record_error``) built on top of them, so
every backend inherits the read-modify-write instead of re-implementing it.
"""

from __future__ import annotations

import abc
import json
import threading
from pathlib import Path

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.config import get_config_value
from mewbo_core.system_instructions.spec import GLOBAL_INSTRUCTIONS_ID, SystemInstructionsDoc

logging = get_logger(name="core.system_instructions.store")


class SystemInstructionsStoreBase(abc.ABC):
    """Abstract interface for custom-system-instruction storage backends."""

    @abc.abstractmethod
    def get(self, doc_id: str = GLOBAL_INSTRUCTIONS_ID) -> SystemInstructionsDoc | None:
        """Return the stored document, or ``None`` when none has been authored."""

    @abc.abstractmethod
    def put(self, doc: SystemInstructionsDoc) -> SystemInstructionsDoc:
        """Upsert *doc* (full-document replace, keyed by ``doc.id``) and return it."""

    # -- concrete template method --------------------------------------

    def record_error(self, doc_id: str, error: str | None) -> None:
        """Persist the outcome of the most recent render attempt.

        Best-effort and never raises: it runs on the run's critical path, where
        a store hiccup must not take down a turn. A no-op when the document is
        gone or the error is unchanged, so a healthy run doesn't write on every
        invocation.
        """
        try:
            doc = self.get(doc_id)
            if doc is None or doc.last_error == error:
                return
            self.put(doc.model_copy(update={"last_error": error}))
        except Exception:
            logging.warning(
                "Could not record the system-instructions render error.", exc_info=True
            )


class JsonSystemInstructionsStore(SystemInstructionsStoreBase):
    """JSON file-backed store — one flat file, id-keyed records.

    Guarded by a ``threading.Lock`` so Flask request threads and the trigger
    watcher thread can share a single instance safely.
    """

    def __init__(self, data_file: str | Path | None = None) -> None:
        """Initialize the store, defaulting the file under the config dir."""
        if data_file is None:
            config_dir = get_config_value(
                "runtime", "config_dir", default=str(Path.home() / ".mewbo")
            )
            data_file = Path(config_dir) / "system_instructions.json"
        self._data_file = Path(data_file)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        if not self._data_file.exists():
            return {}
        try:
            records = json.loads(self._data_file.read_text())
        except Exception:
            logging.warning(
                "System-instructions store file unreadable; treating as empty.", exc_info=True
            )
            return {}
        return records if isinstance(records, dict) else {}

    def _save(self, records: dict[str, dict]) -> None:
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        self._data_file.write_text(json.dumps(records, indent=2))

    def get(self, doc_id: str = GLOBAL_INSTRUCTIONS_ID) -> SystemInstructionsDoc | None:
        """Look up the document by id."""
        with self._lock:
            records = self._load()
        record = records.get(doc_id)
        if not record:
            return None
        try:
            return SystemInstructionsDoc.model_validate(record)
        except Exception:
            logging.warning(
                "Stored system-instructions document is invalid; ignoring it.", exc_info=True
            )
            return None

    def put(self, doc: SystemInstructionsDoc) -> SystemInstructionsDoc:
        """Upsert the document, stamping ``updated_at``."""
        stamped = doc.model_copy(update={"updated_at": utc_now_iso()})
        with self._lock:
            records = self._load()
            records[stamped.id] = stamped.model_dump(mode="json")
            self._save(records)
        return stamped


def create_system_instructions_store(
    data_file: str | Path | None = None,
) -> SystemInstructionsStoreBase:
    """Return the configured system-instructions store driver (json or mongodb).

    Mirrors ``create_trigger_store``/``create_session_store``: reads
    ``storage.driver`` from the app config, defaulting to ``"json"``.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.system_instructions.store_mongo import MongoSystemInstructionsStore

        try:
            return MongoSystemInstructionsStore()
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return JsonSystemInstructionsStore(data_file=data_file)


__all__ = [
    "SystemInstructionsStoreBase",
    "JsonSystemInstructionsStore",
    "create_system_instructions_store",
]
