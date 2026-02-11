#!/usr/bin/env python3
"""Session share token storage."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

from meeseeks_core.config import get_config_value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ShareStore:
    """JSON-backed share token store for session exports."""

    def __init__(self, root_dir: str | None = None, filename: str = "shares.json") -> None:
        """Initialize the share token store location."""
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        root_dir = os.path.abspath(root_dir)
        os.makedirs(root_dir, exist_ok=True)
        self._path = os.path.join(root_dir, filename)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, object]]:
        """Load share token records from disk."""
        if not os.path.exists(self._path):
            return {}
        with open(self._path, encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except json.JSONDecodeError:
                return {}
        if isinstance(data, dict):
            return data
        return {}

    def _save(self, data: dict[str, dict[str, object]]) -> None:
        """Persist share token records to disk."""
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def create(self, session_id: str) -> dict[str, object]:
        """Create and store a new share token."""
        token = uuid.uuid4().hex
        record: dict[str, object] = {"session_id": session_id, "created_at": _utc_now()}
        with self._lock:
            data = self._load()
            data[token] = record
            self._save(data)
        return {"token": token, **record}

    def resolve(self, token: str) -> dict[str, object] | None:
        """Resolve a share token to its record."""
        if not token:
            return None
        with self._lock:
            data = self._load()
            record = data.get(token)
        if not record:
            return None
        return {"token": token, **record}

    def revoke(self, token: str) -> bool:
        """Revoke a share token."""
        if not token:
            return False
        with self._lock:
            data = self._load()
            if token not in data:
                return False
            data.pop(token, None)
            self._save(data)
        return True


__all__ = ["ShareStore"]
