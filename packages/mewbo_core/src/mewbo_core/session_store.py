#!/usr/bin/env python3
"""Session transcript storage and management.

Provides a ``SessionStoreBase`` ABC, a filesystem-backed ``SessionStore``
implementation, and a ``create_session_store()`` factory that returns the
configured driver (json or mongodb).
"""

from __future__ import annotations

import abc
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.types import Event, EventRecord

if TYPE_CHECKING:
    from mewbo_core.compact import CompactionMode, CompactionResult

logging = get_logger(name="core.session_store")


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SessionStoreBase(abc.ABC):
    """Abstract interface for session storage backends.

    Each driver implements the 13 abstract storage primitives.  Higher-level
    operations (``fork_session``, ``load_recent_events``, ``compact_session``)
    are concrete template methods built on top of those primitives.
    """

    root_dir: str  # local directory — always present (used for attachment files)

    # -- abstract primitives ------------------------------------------------

    @abc.abstractmethod
    def create_session(self) -> str:
        """Create a new session and return its identifier."""

    @abc.abstractmethod
    def append_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript."""

    @abc.abstractmethod
    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all transcript events for a session."""

    @abc.abstractmethod
    def save_summary(self, session_id: str, summary: str) -> None:
        """Persist a summary for a session."""

    @abc.abstractmethod
    def load_summary(self, session_id: str) -> str | None:
        """Load a previously saved summary, if present."""

    @abc.abstractmethod
    def save_title(self, session_id: str, title: str) -> None:
        """Persist a display title for a session."""

    @abc.abstractmethod
    def load_title(self, session_id: str) -> str | None:
        """Load a previously saved title, if present."""

    @abc.abstractmethod
    def list_sessions(self) -> list[str]:
        """List all session IDs."""

    @abc.abstractmethod
    def session_dir(self, session_id: str) -> str:
        """Return the local directory path for a session (used for attachments)."""

    @abc.abstractmethod
    def tag_session(self, session_id: str, tag: str) -> None:
        """Associate a tag with a session ID for quick lookup."""

    @abc.abstractmethod
    def resolve_tag(self, tag: str) -> str | None:
        """Resolve a tag to a session ID, if present."""

    @abc.abstractmethod
    def list_tags(self) -> dict[str, str]:
        """Return a mapping of tags to session IDs."""

    @abc.abstractmethod
    def archive_session(self, session_id: str) -> None:
        """Mark a session as archived."""

    @abc.abstractmethod
    def unarchive_session(self, session_id: str) -> None:
        """Remove archived status from a session."""

    @abc.abstractmethod
    def is_archived(self, session_id: str) -> bool:
        """Return True if a session is archived."""

    @abc.abstractmethod
    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Delete all events with ``ts > cutoff_ts``.

        Returns the number of deleted events. Used by the recovery
        pipeline to clean up a failed run before re-driving.
        """

    # -- concrete template methods ------------------------------------------

    def fork_session(self, source_session_id: str) -> str:
        """Create a new session by copying events, summary, and title from another."""
        events = self.load_transcript(source_session_id)
        summary = self.load_summary(source_session_id)
        title = self.load_title(source_session_id)
        new_session_id = self.create_session()
        for event in events:
            self.append_event(new_session_id, event)
        if summary:
            self.save_summary(new_session_id, summary)
        if title:
            self.save_title(new_session_id, title)
        return new_session_id

    def fork_session_at(self, source_session_id: str, cutoff_ts: str) -> str:
        """Fork a session, keeping only events with ``ts <= cutoff_ts``.

        Composes :meth:`fork_session` + :meth:`truncate_after` and clears the
        copied summary (which may reference events beyond the cutoff).
        """
        new_id = self.fork_session(source_session_id)
        self.truncate_after(new_id, cutoff_ts)
        self.save_summary(new_id, "")
        return new_id

    def load_recent_events(
        self,
        session_id: str,
        limit: int = 8,
        include_types: set[str] | None = None,
    ) -> list[EventRecord]:
        """Load the most recent events, optionally filtered by type."""
        events = self.load_transcript(session_id)
        if include_types:
            events = [event for event in events if event.get("type") in include_types]
        if limit <= 0:
            return []
        return events[-limit:]

    async def compact_session(
        self,
        session_id: str,
        mode: CompactionMode | None = None,
        **kwargs: Any,
    ) -> CompactionResult:
        """Compact a session's transcript using structured summarization."""
        from mewbo_core.compact import (
            CompactionMode as CM,
            CompactionResult as CR,
            compact_conversation,
        )

        resolved_mode: CM = CM(mode) if mode is not None else CM.PARTIAL
        events = self.load_transcript(session_id)
        result: CR = await compact_conversation(events, resolved_mode, **kwargs)
        self.save_summary(session_id, result.summary)
        return result


# ---------------------------------------------------------------------------
# Filesystem (JSON) driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPaths:
    """Resolved filesystem paths for a session."""

    root: str
    session_id: str

    @property
    def session_dir(self) -> str:
        """Directory for session artifacts."""
        return os.path.join(self.root, self.session_id)

    @property
    def transcript_path(self) -> str:
        """Path to the JSONL transcript file."""
        return os.path.join(self.session_dir, "transcript.jsonl")

    @property
    def summary_path(self) -> str:
        """Path to the summary JSON file."""
        return os.path.join(self.session_dir, "summary.json")

    @property
    def title_path(self) -> str:
        """Path to the title JSON file."""
        return os.path.join(self.session_dir, "title.json")


class SessionStore(SessionStoreBase):
    """Filesystem-backed storage for session transcripts and summaries."""

    def __init__(self, root_dir: str | None = None) -> None:
        """Initialize the store and ensure the root directory exists."""
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _index_path(self) -> str:
        """Return the path for the session index file."""
        return os.path.join(self.root_dir, "index.json")

    def _load_index(self) -> dict[str, dict[str, str]]:
        """Load the session index from disk or return defaults."""
        index_path = self._index_path()
        if not os.path.exists(index_path):
            return {"tags": {}, "archived": {}}
        with open(index_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save_index(self, data: dict[str, dict[str, str]]) -> None:
        """Persist the session index to disk."""
        with open(self._index_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def create_session(self) -> str:
        """Create a new session directory and return its identifier."""
        session_id = uuid.uuid4().hex
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        return session_id

    def _paths(self, session_id: str) -> SessionPaths:
        """Build filesystem paths for a session."""
        return SessionPaths(root=self.root_dir, session_id=session_id)

    def session_dir(self, session_id: str) -> str:
        """Return the directory path for a session."""
        return self._paths(session_id).session_dir

    def append_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        payload: EventRecord = {"ts": _utc_now(), **event}
        with open(paths.transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all transcript events for a session."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.transcript_path):
            return []
        events: list[EventRecord] = []
        with open(paths.transcript_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logging.warning("Skipping malformed transcript line.")
        return events

    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Rewrite the transcript keeping only events with ``ts <= cutoff_ts``."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.transcript_path):
            return 0
        events = self.load_transcript(session_id)
        kept = [e for e in events if e.get("ts", "") <= cutoff_ts]
        removed = len(events) - len(kept)
        if removed:
            with open(paths.transcript_path, "w", encoding="utf-8") as handle:
                for event in kept:
                    handle.write(json.dumps(event) + "\n")
        return removed

    def save_summary(self, session_id: str, summary: str) -> None:
        """Persist a summary for a session."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        with open(paths.summary_path, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "updated_at": _utc_now()}, handle, indent=2)

    def load_summary(self, session_id: str) -> str | None:
        """Load a previously saved summary, if present."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.summary_path):
            return None
        with open(paths.summary_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("summary")

    def save_title(self, session_id: str, title: str) -> None:
        """Persist a display title for a session."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        with open(paths.title_path, "w", encoding="utf-8") as handle:
            json.dump({"title": title, "updated_at": _utc_now()}, handle, indent=2)

    def load_title(self, session_id: str) -> str | None:
        """Load a previously saved title, if present."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.title_path):
            return None
        with open(paths.title_path, encoding="utf-8") as handle:
            data = json.load(handle)
        title = data.get("title")
        return title if isinstance(title, str) and title else None

    def list_sessions(self) -> list[str]:
        """List all session IDs present in the root directory."""
        if not os.path.exists(self.root_dir):
            return []
        return sorted(
            name
            for name in os.listdir(self.root_dir)
            if os.path.isdir(os.path.join(self.root_dir, name))
        )

    def tag_session(self, session_id: str, tag: str) -> None:
        """Associate a tag with a session ID for quick lookup."""
        index = self._load_index()
        index.setdefault("tags", {})[tag] = session_id
        self._save_index(index)

    def resolve_tag(self, tag: str) -> str | None:
        """Resolve a tag to a session ID, if present."""
        index = self._load_index()
        return index.get("tags", {}).get(tag)

    def list_tags(self) -> dict[str, str]:
        """Return a mapping of tags to session IDs."""
        index = self._load_index()
        return dict(index.get("tags", {}))

    def archive_session(self, session_id: str) -> None:
        """Mark a session as archived."""
        index = self._load_index()
        archived = index.setdefault("archived", {})
        archived[session_id] = _utc_now()
        self._save_index(index)

    def unarchive_session(self, session_id: str) -> None:
        """Remove archived status from a session."""
        index = self._load_index()
        archived = index.get("archived", {})
        if session_id in archived:
            archived.pop(session_id, None)
            index["archived"] = archived
            self._save_index(index)

    def is_archived(self, session_id: str) -> bool:
        """Return True if a session is archived."""
        index = self._load_index()
        archived = index.get("archived", {})
        return session_id in archived


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_session_store(root_dir: str | None = None) -> SessionStoreBase:
    """Return the configured session store driver.

    Reads ``storage.driver`` from the app config.  Defaults to ``"json"``
    (filesystem).  Set to ``"mongodb"`` to use MongoDB.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.session_store_mongo import MongoSessionStore

        try:
            return MongoSessionStore(root_dir=root_dir)
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return SessionStore(root_dir=root_dir)


__all__ = [
    "SessionStoreBase",
    "SessionStore",
    "SessionPaths",
    "create_session_store",
]
