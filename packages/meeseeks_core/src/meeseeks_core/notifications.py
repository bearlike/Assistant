#!/usr/bin/env python3
"""Lightweight notification storage for Meeseeks."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from meeseeks_core.config import get_config_value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class NotificationRecord:
    """Typed record for serialized notifications."""

    id: str
    title: str
    message: str
    level: str
    created_at: str
    dismissed: bool
    session_id: str | None = None
    dismissed_at: str | None = None
    event_type: str | None = None
    metadata: dict[str, object] | None = None


class NotificationStore:
    """JSON-backed notification store for single-user UI."""

    def __init__(self, root_dir: str | None = None, filename: str = "notifications.json") -> None:
        """Initialize the notification store location."""
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        root_dir = os.path.abspath(root_dir)
        os.makedirs(root_dir, exist_ok=True)
        self._path = os.path.join(root_dir, filename)
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, object]]:
        """Load notification records from disk."""
        if not os.path.exists(self._path):
            return []
        with open(self._path, encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except json.JSONDecodeError:
                return []
        if isinstance(data, list):
            return data
        return []

    def _save(self, data: list[dict[str, object]]) -> None:
        """Persist notification records to disk."""
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def list(self, *, include_dismissed: bool = False) -> list[dict[str, object]]:
        """Return notifications, optionally including dismissed ones."""
        with self._lock:
            data = self._load()
        if not include_dismissed:
            data = [item for item in data if not item.get("dismissed")]
        return sorted(
            data,
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        )

    def add(
        self,
        *,
        title: str,
        message: str,
        level: str = "info",
        session_id: str | None = None,
        event_type: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Add a new notification record and return it."""
        record = NotificationRecord(
            id=uuid.uuid4().hex,
            title=title,
            message=message,
            level=level,
            created_at=_utc_now(),
            dismissed=False,
            session_id=session_id,
            dismissed_at=None,
            event_type=event_type,
            metadata=metadata,
        )
        payload = record.__dict__
        with self._lock:
            data = self._load()
            data.append(payload)
            self._save(data)
        return payload

    def dismiss(self, ids: Sequence[str]) -> int:
        """Mark notifications as dismissed."""
        if not ids:
            return 0
        dismissed_at = _utc_now()
        updated = 0
        with self._lock:
            data = self._load()
            for item in data:
                if item.get("id") in ids and not item.get("dismissed"):
                    item["dismissed"] = True
                    item["dismissed_at"] = dismissed_at
                    updated += 1
            self._save(data)
        return updated

    def clear(self, *, dismissed_only: bool = True) -> int:
        """Clear dismissed notifications (or all when requested)."""
        with self._lock:
            data = self._load()
            if dismissed_only:
                remaining = [item for item in data if not item.get("dismissed")]
            else:
                remaining = []
            removed = len(data) - len(remaining)
            self._save(remaining)
        return removed


__all__ = ["NotificationStore", "NotificationRecord"]
