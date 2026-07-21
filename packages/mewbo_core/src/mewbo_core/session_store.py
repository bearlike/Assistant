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
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger, utc_now_iso as _utc_now
from mewbo_core.config import get_config_value
from mewbo_core.types import Event, EventRecord

if TYPE_CHECKING:
    from mewbo_core.compact import CompactionMode, CompactionResult

logging = get_logger(name="core.session_store")


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

    def __init__(self) -> None:
        """Initialise the in-memory termination cache shared by every backend.

        Populated by :meth:`_mark_terminated_cached` (called from each
        backend's ``terminate_session``) and consulted by
        :meth:`_guard_append`, so a hot-path append never pays a Mongo
        round-trip / index-file read to learn whether its own session was
        just terminated. Best-effort and per-process only: a session
        terminated by a DIFFERENT process/worker is still caught by the
        durable ``terminated_at`` stamp at every other termination-aware seam
        (``resolve_session``, recovery) — this cache only shortcuts the
        common same-process race between a live run and a concurrent
        ``/terminate`` call.
        """
        self._terminated_cache: set[str] = set()
        self._terminated_logged: set[str] = set()

    def _mark_terminated_cached(self, session_id: str) -> None:
        """Record *session_id* as terminated in the in-memory cache."""
        self._terminated_cache.add(session_id)

    def _guard_append(self, session_id: str) -> bool:
        """Return True iff *session_id* may still accept an appended event.

        Consults ONLY the in-memory cache (never a store round-trip) — see
        :meth:`__init__`. Structured-logs once per session (not once per
        dropped event) the first time an append is refused.
        """
        if session_id not in self._terminated_cache:
            return True
        if session_id not in self._terminated_logged:
            self._terminated_logged.add(session_id)
            logging.warning("Dropping event(s) appended to terminated session {}.", session_id)
        return False

    # -- abstract primitives ------------------------------------------------

    @abc.abstractmethod
    def create_session(self, owner: str | None = None) -> str:
        """Create a new session and return its identifier.

        *owner* is an OPAQUE subject string, never an identity object: core sits
        below the identity kernel in the dependency DAG and must not learn what a
        principal is. The caller that HAS one resolves it to a string first.
        ``None`` means unowned, which is what every session created without an
        authenticated caller is — see :meth:`list_sessions` for what that implies
        on the read side.
        """

    @abc.abstractmethod
    def ensure_session(self, session_id: str, owner: str | None = None) -> None:
        """Idempotently materialise a session RECORD for a known id.

        ``create_session`` mints its own uuid; some callers (the realtime
        write-behind recorder) need to back a session whose id was pre-minted
        elsewhere — e.g. for an in-flight Langfuse trace opened before any store
        write. Without a record, ``list_sessions`` (and every read surface built
        on it) never sees the id, so the transcript is an orphan: events exist,
        the session is invisible.

        This is the seam that closes that gap. It is idempotent — calling it on
        an existing session is a no-op (never resets created_at / archived state).
        ``create_session`` is implemented on top of it (mint id → materialise).

        The owner stamp is SET-ONCE at materialisation, like ``created_at`` and
        unlike ``archived_at``: a replay must not be able to re-point an existing
        session at a different subject, which would be an ownership takeover
        written through an idempotent no-op path.
        """

    @abc.abstractmethod
    def _write_event(self, session_id: str, event: Event) -> None:
        """Durably write *event*, unconditionally (no termination guard).

        The shared primitive :meth:`append_event` (guarded) and
        :meth:`append_terminal_event` (the termination tombstone's one
        exemption) both write through — the actual per-backend I/O
        (file append / Mongo insert) plus the ``_publish_appended`` fan-out.
        """

    def append_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript.

        No-ops (after a dropped-event log, once per session) once
        :meth:`terminate_session` has stamped this session terminated — see
        :meth:`_guard_append`. The ONE deliberate exception is
        :meth:`append_terminal_event`, used for the termination tombstone
        itself.
        """
        if not self._guard_append(session_id):
            return
        self._write_event(session_id, event)

    def append_terminal_event(self, session_id: str, event: Event) -> None:
        """Append an event exempt from the termination guard.

        ``SessionRuntime.terminate_session`` writes the ``session_terminated``
        tombstone through this seam immediately after the store's own
        ``terminate_session`` has already cached the session as terminated —
        an ordinary :meth:`append_event` call would otherwise drop its own
        closing event. No other caller should use this.
        """
        self._write_event(session_id, event)

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
    def list_sessions(self, owner: str | None = None) -> list[str]:
        """List session IDs, optionally narrowed to what *owner* may see.

        ``owner=None`` lists EVERYTHING — the historical behaviour, and what a
        caller holding a read-all authority (or no identity at all) gets.

        A non-``None`` *owner* narrows to that subject's own sessions PLUS every
        UNOWNED one. Unowned is not a hole in the filter, it is the migration
        semantic: sessions predating the owner stamp carry no subject, and no
        subject can be reconstructed for them after the fact. Hiding them would
        make a user's existing work vanish from their own list, which is a worse
        failure than showing a pre-existing session to someone who could already
        list it before the stamp existed. The unowned set is closed and shrinking
        — every session created by an authenticated caller from here on is
        stamped — so this is a fading allowance, not a permanent widening.

        Filtering belongs HERE rather than at the caller: the store already walks
        the record set, so narrowing at the route would mean loading every
        session's metadata only to discard most of it.
        """

    @abc.abstractmethod
    def get_owner(self, session_id: str) -> str | None:
        """Return the owning subject, or ``None`` if the session is unowned.

        The read-through sibling of the stamp ``ensure_session`` writes, mirroring
        how ``is_archived`` reads ``archive_session``'s write.
        """

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

    def tags_for_session(self, session_id: str) -> list[str]:
        """Return every tag pointing at ``session_id`` (reverse of ``resolve_tag``).

        Concrete default over ``list_tags`` so all backends share one
        implementation; the tag set is small. Provenance classification reads
        this to recover a session's origin (see ``session_provenance``).
        """
        return [tag for tag, sid in self.list_tags().items() if sid == session_id]

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
    def terminate_session(self, session_id: str) -> bool:
        """Permanently mark a session terminated (irreversible).

        Stamps ``terminated_at`` **once** — a repeat call never moves the
        original timestamp. There is deliberately NO un-terminate primitive:
        termination is a one-way door. Mirrors
        ``archive_session`` but without the reverse operation.

        Returns ``True`` iff THIS call was the one that newly stamped the
        timestamp, ``False`` if the session was already terminated. This is
        the arbitration signal ``SessionRuntime.terminate_session`` reads to
        run its side effects (cancel + callbacks + event) exactly once even
        under concurrent callers — the store's set-once write is the only
        thing racing safely, so the runtime must never decide on its own
        unlocked read.
        """

    @abc.abstractmethod
    def get_terminated_at(self, session_id: str) -> str | None:
        """Return the ISO ``terminated_at`` timestamp, or ``None`` if live.

        The read-through sibling of :meth:`terminate_session` (mirrors how
        ``is_archived`` reads ``archive_session``'s write).
        """

    def is_terminated(self, session_id: str) -> bool:
        """Return True iff a session was permanently terminated.

        Concrete over :meth:`get_terminated_at` so both backends share one
        implementation — the single derivation every termination guard reads.
        """
        return self.get_terminated_at(session_id) is not None

    @abc.abstractmethod
    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Delete all events with ``ts > cutoff_ts``.

        Returns the number of deleted events. Used by the recovery
        pipeline to clean up a failed run before re-driving.
        """

    # -- concrete template methods ------------------------------------------

    @staticmethod
    def _publish_appended(session_id: str, record: EventRecord) -> None:
        """Fan a just-appended record out to the process-wide event bus.

        The universal append choke-point: every backend calls this right after
        the durable write so SSE waiters wake immediately and ``on_event`` hooks
        fire — driven by the SAME persisted ``record`` ``load_transcript``
        returns, so a live SSE event is byte-identical to the backlog one.
        Best-effort: a bus failure must never break a durable append.
        """
        from mewbo_core.session_event_bus import get_session_event_bus

        try:
            get_session_event_bus().publish(session_id, record)
        except Exception:
            logging.warning("Session event bus publish failed.", exc_info=True)

    @staticmethod
    def merge_context_events(events: list[EventRecord]) -> dict[str, object]:
        """Reduce a transcript to its current context (most-recent payload wins).

        One reducer shared by :meth:`latest_context` (the trace-provenance path)
        and ``SessionRuntime.summarize_session`` (the origin/recovery path) so the
        rule for "what context is this session running under" lives in exactly one
        place. ``context`` events are sparse, so a full scan is cheap.
        """
        merged: dict[str, object] = {}
        for event in events:
            if event.get("type") == "context":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    merged.update(payload)
        return merged

    def latest_context(self, session_id: str) -> dict[str, object]:
        """Return the session's merged context (most-recent context event wins)."""
        return self.merge_context_events(self.load_transcript(session_id))

    def last_attestation_hash(self, session_id: str) -> str:
        """Return the attestation chain's head to re-seed on recovery.

        Concrete default over :meth:`load_transcript`: scans for the most
        recent ``type == "attestation"`` event and returns its persisted
        ``record_hash``, else the chain's genesis hash — mirrors the
        ``tags_for_session`` concrete-default idiom so every backend shares
        one scan; ``MongoSessionStore`` overrides with a targeted query.
        """
        from mewbo_core.attestation import GENESIS_HASH

        for event in reversed(self.load_transcript(session_id)):
            if event.get("type") != "attestation":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                record_hash = payload.get("record_hash")
                if isinstance(record_hash, str) and record_hash:
                    return record_hash
        return GENESIS_HASH

    def fork_session(self, source_session_id: str, owner: str | None = None) -> str:
        """Create a new session by copying events, summary, and title from another.

        The fork is stamped for whoever asked for it, NOT for the source's owner:
        a fork is a new session that happens to start with a copy of a transcript.
        Leaving it unstamped would be worse than either — an unowned fork of an
        owned session is visible to every lister, so forking would launder a
        session out of its owner's scope.
        """
        events = self.load_transcript(source_session_id)
        summary = self.load_summary(source_session_id)
        title = self.load_title(source_session_id)
        new_session_id = self.create_session(owner)
        for event in events:
            self.append_event(new_session_id, event)
        if summary:
            self.save_summary(new_session_id, summary)
        if title:
            self.save_title(new_session_id, title)
        return new_session_id

    def fork_session_at(
        self, source_session_id: str, cutoff_ts: str, owner: str | None = None
    ) -> str:
        """Fork a session, keeping only events with ``ts <= cutoff_ts``.

        Composes :meth:`fork_session` + :meth:`truncate_after` and clears the
        copied summary (which may reference events beyond the cutoff).
        """
        new_id = self.fork_session(source_session_id, owner)
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
        super().__init__()
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
            return {"tags": {}, "archived": {}, "terminated": {}, "owners": {}}
        with open(index_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save_index(self, data: dict[str, dict[str, str]]) -> None:
        """Persist the session index to disk."""
        with open(self._index_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def create_session(self, owner: str | None = None) -> str:
        """Create a new session directory and return its identifier."""
        session_id = uuid.uuid4().hex
        self.ensure_session(session_id, owner)
        return session_id

    def ensure_session(self, session_id: str, owner: str | None = None) -> None:
        """Idempotently create the session directory for a known id.

        For the filesystem driver a session "record" IS its directory (that's
        what :meth:`list_sessions` enumerates), so materialisation is a
        ``makedirs`` — ``exist_ok=True`` makes it a safe no-op on replay.

        The owner lands in its own index bucket beside ``archived``/``terminated``
        rather than in a per-session file, so the list filter costs one index read
        instead of one stat per session.

        The stamp is written ONLY when this call is the one that materialised the
        record — the filesystem analogue of the Mongo driver's ``$setOnInsert``,
        and the reason the directory's prior existence is checked before creating
        it. Stamping on any call would make an ALREADY-UNOWNED session claimable
        by whoever touched it next, and since an unowned session is visible to
        every lister, that is not necessarily its creator. An unowned call never
        writes at all, so a deployment with no identity configured leaves the
        index exactly as it was.
        """
        paths = self._paths(session_id)
        already_materialised = os.path.isdir(paths.session_dir)
        os.makedirs(paths.session_dir, exist_ok=True)
        if owner is None or already_materialised:
            return
        index = self._load_index()
        index.setdefault("owners", {})[session_id] = owner
        self._save_index(index)

    def get_owner(self, session_id: str) -> str | None:
        """Return the stamped owning subject, or ``None`` if unowned."""
        return self._load_index().get("owners", {}).get(session_id)

    def _paths(self, session_id: str) -> SessionPaths:
        """Build filesystem paths for a session."""
        return SessionPaths(root=self.root_dir, session_id=session_id)

    def session_dir(self, session_id: str) -> str:
        """Return the directory path for a session."""
        return self._paths(session_id).session_dir

    def _write_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript, unconditionally."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        payload: EventRecord = {"ts": _utc_now(), **event}
        with open(paths.transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        self._publish_appended(session_id, payload)

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

    def list_sessions(self, owner: str | None = None) -> list[str]:
        """List session IDs in the root directory, narrowed to *owner* if given."""
        if not os.path.exists(self.root_dir):
            return []
        session_ids = sorted(
            name
            for name in os.listdir(self.root_dir)
            if os.path.isdir(os.path.join(self.root_dir, name))
        )
        if owner is None:
            return session_ids
        # One index read for the whole listing, not one per session. An id absent
        # from the bucket is unowned and stays visible — see the base's contract.
        owners = self._load_index().get("owners", {})
        return [sid for sid in session_ids if owners.get(sid, owner) == owner]

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

    def terminate_session(self, session_id: str) -> bool:
        """Stamp ``terminated_at`` in the index once (never overwrites).

        Returns whether THIS call inserted the stamp — checked before the
        write so a repeat call reports ``False`` without touching the file.
        """
        index = self._load_index()
        terminated = index.setdefault("terminated", {})
        if session_id in terminated:
            self._mark_terminated_cached(session_id)
            return False
        terminated[session_id] = _utc_now()
        self._save_index(index)
        self._mark_terminated_cached(session_id)
        return True

    def get_terminated_at(self, session_id: str) -> str | None:
        """Return the stored ``terminated_at`` timestamp, or ``None``."""
        index = self._load_index()
        return index.get("terminated", {}).get(session_id)


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
