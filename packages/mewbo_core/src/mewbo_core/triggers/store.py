#!/usr/bin/env python3
"""Trigger storage — a ``TriggerStoreBase`` ABC + a JSON file-backed driver.

Mirrors ``session_store.py``'s shape: a handful of abstract primitives
(``create``/``get``/``list``/``update``) plus concrete template methods
(``list_armed``/``cancel_for_session``) built on top of them, so every
backend gets the cascade/filter logic for free instead of re-implementing it.
"""

from __future__ import annotations

import abc
import builtins
import json
import threading
from pathlib import Path

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.triggers.spec import TriggerSpec, TriggerStatus, parse_trigger

logging = get_logger(name="core.triggers.store")


class TriggerStoreBase(abc.ABC):
    """Abstract interface for trigger storage backends."""

    @abc.abstractmethod
    def create(self, trigger: TriggerSpec) -> TriggerSpec:
        """Persist a new trigger and return it."""

    @abc.abstractmethod
    def get(self, trigger_id: str) -> TriggerSpec | None:
        """Return a trigger by id, or ``None`` if not found."""

    @abc.abstractmethod
    def list(
        self,
        *,
        session_id: str | None = None,
        kind: str | None = None,
        status: TriggerStatus | None = None,
        limit: int | None = None,
    ) -> list[TriggerSpec]:
        """List triggers, optionally filtered by session/kind/status."""

    @abc.abstractmethod
    def update(self, trigger: TriggerSpec) -> TriggerSpec:
        """Full-document replace of an existing trigger, keyed by ``trigger.id``.

        Raises ``KeyError`` if no trigger with that id exists yet — callers
        must ``create`` before ``update``.
        """

    # -- concrete template methods -------------------------------------

    def list_armed(self) -> builtins.list[TriggerSpec]:
        """All triggers currently eligible for a scheduler to poll."""
        return self.list(status="armed")

    def cancel_for_session(self, session_id: str) -> int:
        """Cascade-cancel every non-terminal trigger owned by a session.

        Used when a session is hard-terminated: any trigger still waiting to
        wake it must not fire into a dead session. Returns the number of
        triggers actually moved to ``cancelled`` (terminal triggers are
        left untouched, not re-counted).
        """
        cancelled = 0
        for trigger in self.list(session_id=session_id):
            if trigger.is_terminal:
                continue
            trigger.transition("cancelled")
            self.update(trigger)
            cancelled += 1
        return cancelled


class JsonTriggerStore(TriggerStoreBase):
    """JSON file-backed trigger store — one flat file, list-of-records.

    Mirrors ``JsonProjectStore``'s layout (triggers are an independent flat
    collection, not per-session artifacts like the transcript store) —
    a single JSON array file guarded by a ``threading.Lock`` so the watcher
    thread and Flask request threads can share one store instance safely.
    """

    def __init__(self, data_file: str | Path | None = None) -> None:
        """Initialize the store, defaulting the file under the config dir."""
        if data_file is None:
            config_dir = get_config_value(
                "runtime", "config_dir", default=str(Path.home() / ".mewbo")
            )
            data_file = Path(config_dir) / "triggers.json"
        self._data_file = Path(data_file)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self._data_file.exists():
            return []
        try:
            return json.loads(self._data_file.read_text())
        except Exception:
            logging.warning("Trigger store file unreadable; treating as empty.", exc_info=True)
            return []

    def _save(self, records: list[dict]) -> None:
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        self._data_file.write_text(json.dumps(records, indent=2))

    def create(self, trigger: TriggerSpec) -> TriggerSpec:
        """Append a new trigger record."""
        with self._lock:
            records = self._load()
            records.append(trigger.model_dump(mode="json"))
            self._save(records)
        return trigger

    def get(self, trigger_id: str) -> TriggerSpec | None:
        """Look up a trigger by id."""
        with self._lock:
            records = self._load()
        for record in records:
            if record.get("id") == trigger_id:
                return parse_trigger(record)
        return None

    def list(
        self,
        *,
        session_id: str | None = None,
        kind: str | None = None,
        status: TriggerStatus | None = None,
        limit: int | None = None,
    ) -> list[TriggerSpec]:
        """List triggers, optionally filtered by session/kind/status."""
        with self._lock:
            records = self._load()
        triggers = [parse_trigger(record) for record in records]
        if session_id is not None:
            triggers = [t for t in triggers if t.session_id == session_id]
        if kind is not None:
            triggers = [t for t in triggers if t.kind == kind]
        if status is not None:
            triggers = [t for t in triggers if t.status == status]
        if limit is not None:
            triggers = triggers[:limit]
        return triggers

    def update(self, trigger: TriggerSpec) -> TriggerSpec:
        """Full-document replace by id."""
        with self._lock:
            records = self._load()
            for i, record in enumerate(records):
                if record.get("id") == trigger.id:
                    records[i] = trigger.model_dump(mode="json")
                    self._save(records)
                    return trigger
        raise KeyError(f"Trigger {trigger.id} not found")


def create_trigger_store(data_file: str | Path | None = None) -> TriggerStoreBase:
    """Return the configured trigger store driver (json or mongodb).

    Mirrors ``create_session_store``/``create_project_store``: reads
    ``storage.driver`` from the app config, defaulting to ``"json"``.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.triggers.store_mongo import MongoTriggerStore

        try:
            return MongoTriggerStore()
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return JsonTriggerStore(data_file=data_file)


__all__ = [
    "TriggerStoreBase",
    "JsonTriggerStore",
    "create_trigger_store",
]
