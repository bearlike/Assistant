#!/usr/bin/env python3
"""Shared JSON-file persistence for the identity stores.

``_JsonCollectionStore`` owns the file path plus a lock and the atomic
load/save primitives (tmp file + ``os.replace``, mirroring ``key_store``). Each
concrete store extends it, names the file it owns, and layers typed
``create``/``get``/``list`` on top of :meth:`_read`/:meth:`_write`, so every JSON
driver gets crash-safe writes and thread-safety without re-implementing them.
It also owns :meth:`resolve_driver`, the ONE json-or-mongodb selection every
``create_*_store`` alias delegates to.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, ClassVar

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value

logging = get_logger(name="iam.stores.json")


class _JsonCollectionStore:
    """One JSON file holding a flat list of record dicts, guarded by a lock."""

    # The file this store owns under the config dir, and the name of its
    # sibling driver in ``stores.mongo``. Both are declared by every concrete
    # subclass; they are what let the default path and the driver selection be
    # inherited instead of re-spelled per store.
    _FILENAME: ClassVar[str]
    _MONGO_DRIVER: ClassVar[str]

    def __init__(self, path: str | Path | None = None) -> None:
        """Bind the store to ``path``, defaulting to ``<config_dir>/<_FILENAME>``.

        The default mirrors ``JsonTriggerStore``: the writable config dir,
        falling back to ``~/.mewbo`` when unset. The parent directory is created
        lazily on the first write.
        """
        if path is None:
            config_dir = get_config_value(
                "runtime", "config_dir", default=str(Path.home() / ".mewbo")
            )
            path = Path(config_dir) / self._FILENAME
        self._path = Path(path)
        self._lock = threading.Lock()

    @classmethod
    def resolve_driver(cls, path: str | Path | None = None) -> Any:
        """Return the configured driver — this JSON store, or its Mongo sibling.

        The ONE place ``storage.driver`` is read for the identity stores, so the
        five ``create_*_store`` aliases stay thin and the "mongodb selected but
        unreachable" message cannot drift between them. ``pymongo`` is imported
        only on the mongodb branch, keeping a json install free of it.

        Returns ``Any`` deliberately: the two branches share no type beyond the
        per-store ABC, which each factory alias re-declares as its own return
        annotation — that alias, not this method, is the typed contract.
        """
        if get_config_value("storage", "driver", default="json") != "mongodb":
            return cls(path=path)
        from mewbo_iam.stores import mongo

        try:
            return getattr(mongo, cls._MONGO_DRIVER)()
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. Error: {exc}"
            ) from exc

    def _read(self) -> list[dict[str, Any]]:
        """Load all record dicts, tolerating a missing or corrupt file."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            logging.warning("IAM store file {} unreadable; treating as empty.", self._path)
            return []
        return data if isinstance(data, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        """Persist records atomically (tmp file + replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(records, indent=2))
        os.replace(tmp, self._path)


__all__ = ["_JsonCollectionStore"]
