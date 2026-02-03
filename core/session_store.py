#!/usr/bin/env python3
"""Session transcript storage and management."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from core.common import get_logger
from core.types import Event, EventRecord

logging = get_logger(name="core.session_store")


def _utc_now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string.

    Returns:
        ISO-8601 UTC timestamp string.
    """
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SessionPaths:
    """Resolved filesystem paths for a session.

    Attributes:
        root: Root directory for session storage.
        session_id: Unique session identifier.
    """
    root: str
    session_id: str

    @property
    def session_dir(self) -> str:
        """Directory for session artifacts.

        Returns:
            Absolute directory path for the session.
        """
        return os.path.join(self.root, self.session_id)

    @property
    def transcript_path(self) -> str:
        """Path to the JSONL transcript file.

        Returns:
            Absolute path to the transcript file.
        """
        return os.path.join(self.session_dir, "transcript.jsonl")

    @property
    def summary_path(self) -> str:
        """Path to the summary JSON file.

        Returns:
            Absolute path to the summary file.
        """
        return os.path.join(self.session_dir, "summary.json")


class SessionStore:
    """Filesystem-backed storage for session transcripts and summaries."""
    def __init__(self, root_dir: str | None = None) -> None:
        """Initialize the store and ensure the root directory exists.

        Args:
            root_dir: Optional root directory override.
        """
        if root_dir is None:
            root_dir = os.getenv("MESEEKS_SESSION_DIR", "./data/sessions")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _index_path(self) -> str:
        """Return the path for the session index file.

        Returns:
            Absolute path to index.json.
        """
        return os.path.join(self.root_dir, "index.json")

    def _load_index(self) -> dict[str, dict[str, str]]:
        """Load the session index from disk, or return a default structure.

        Returns:
            Index data with tag mappings.
        """
        index_path = self._index_path()
        if not os.path.exists(index_path):
            return {"tags": {}}
        with open(index_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save_index(self, data: dict[str, dict[str, str]]) -> None:
        """Persist the session index to disk.

        Args:
            data: Index payload to serialize.
        """
        with open(self._index_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def create_session(self) -> str:
        """Create a new session directory and return its identifier.

        Returns:
            Newly generated session identifier.
        """
        session_id = uuid.uuid4().hex
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        return session_id

    def _paths(self, session_id: str) -> SessionPaths:
        """Build filesystem paths for a session.

        Args:
            session_id: Session identifier to resolve.

        Returns:
            SessionPaths with resolved filesystem locations.
        """
        return SessionPaths(root=self.root_dir, session_id=session_id)

    def append_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript.

        Args:
            session_id: Session identifier to append to.
            event: Event payload to persist.

        Raises:
            OSError: If writing to disk fails.
        """
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        payload: EventRecord = {"ts": _utc_now(), **event}
        with open(paths.transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all transcript events for a session.

        Args:
            session_id: Session identifier to load.

        Returns:
            List of event records in chronological order.
        """
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

    def load_recent_events(
        self,
        session_id: str,
        limit: int = 8,
        include_types: set[str] | None = None,
    ) -> list[EventRecord]:
        """Load the most recent events, optionally filtered by type.

        Args:
            session_id: Session identifier to load.
            limit: Maximum number of events to return.
            include_types: Optional set of event types to include.

        Returns:
            List of recent event records.
        """
        events = self.load_transcript(session_id)
        if include_types:
            events = [event for event in events if event.get("type") in include_types]
        if limit <= 0:
            return []
        return events[-limit:]

    def save_summary(self, session_id: str, summary: str) -> None:
        """Persist a summary for a session.

        Args:
            session_id: Session identifier to update.
            summary: Summary text to store.

        Raises:
            OSError: If writing to disk fails.
        """
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        with open(paths.summary_path, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "updated_at": _utc_now()}, handle, indent=2)

    def load_summary(self, session_id: str) -> str | None:
        """Load a previously saved summary, if present.

        Args:
            session_id: Session identifier to load.

        Returns:
            Summary text if stored; otherwise None.
        """
        paths = self._paths(session_id)
        if not os.path.exists(paths.summary_path):
            return None
        with open(paths.summary_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("summary")

    def list_sessions(self) -> list[str]:
        """List all session IDs present in the root directory.

        Returns:
            Sorted list of session identifiers.
        """
        if not os.path.exists(self.root_dir):
            return []
        return sorted(
            name
            for name in os.listdir(self.root_dir)
            if os.path.isdir(os.path.join(self.root_dir, name))
        )

    def fork_session(self, source_session_id: str) -> str:
        """Create a new session by copying events and summary from another.

        Args:
            source_session_id: Session identifier to clone.

        Returns:
            Identifier of the newly created session.
        """
        events = self.load_transcript(source_session_id)
        summary = self.load_summary(source_session_id)
        new_session_id = self.create_session()
        for event in events:
            self.append_event(new_session_id, event)
        if summary:
            self.save_summary(new_session_id, summary)
        return new_session_id

    def tag_session(self, session_id: str, tag: str) -> None:
        """Associate a tag with a session ID for quick lookup.

        Args:
            session_id: Session identifier to tag.
            tag: Friendly tag name.
        """
        index = self._load_index()
        index.setdefault("tags", {})[tag] = session_id
        self._save_index(index)

    def resolve_tag(self, tag: str) -> str | None:
        """Resolve a tag to a session ID, if present.

        Args:
            tag: Friendly tag name to resolve.

        Returns:
            Session identifier if found; otherwise None.
        """
        index = self._load_index()
        return index.get("tags", {}).get(tag)

    def list_tags(self) -> dict[str, str]:
        """Return a mapping of tags to session IDs.

        Returns:
            Dictionary mapping tags to session identifiers.
        """
        index = self._load_index()
        return dict(index.get("tags", {}))
