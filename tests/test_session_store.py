"""Tests for session store persistence helpers."""

import os
import shutil
from unittest.mock import patch

import pytest
from mewbo_core.config import StorageConfig
from mewbo_core.session_store import SessionStore, SessionStoreBase, create_session_store
from pydantic import ValidationError


def test_session_store_roundtrip(tmp_path):
    """Persist events and summaries in the session store."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()

    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(session_id, {"type": "tool_result", "payload": {"text": "ok"}})

    events = store.load_transcript(session_id)
    assert len(events) == 2
    assert events[0]["type"] == "user"

    store.save_summary(session_id, "summary text")
    assert store.load_summary(session_id) == "summary text"


def test_session_store_recent_events_and_filters(tmp_path):
    """Filter recent events by type and respect zero limits."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi"}})
    store.append_event(session_id, {"type": "tool_result", "payload": {"text": "ok"}})

    assert store.load_recent_events(session_id, limit=0) == []
    filtered = store.load_recent_events(session_id, limit=5, include_types={"tool_result"})
    assert len(filtered) == 1
    assert filtered[0]["type"] == "tool_result"


def test_session_store_tag_and_fork(tmp_path):
    """Tag sessions and fork transcripts for new sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})

    store.tag_session(session_id, "primary")
    assert store.resolve_tag("primary") == session_id

    forked = store.fork_session(session_id)
    assert forked != session_id
    assert store.load_transcript(forked)


def test_session_store_load_transcript_skips_bad_lines(tmp_path):
    """Skip malformed transcript lines without failing."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    paths = store._paths(session_id)
    paths.session_dir and paths.transcript_path  # touch for coverage
    with open(paths.transcript_path, "w", encoding="utf-8") as handle:
        handle.write("{invalid}\n")
        handle.write('{"type": "user", "payload": {"text": "ok"}, "ts": "1"}\n')
    events = store.load_transcript(session_id)
    assert len(events) == 1
    assert events[0]["type"] == "user"


def test_session_store_list_sessions_missing_root(tmp_path):
    """Return empty list when session root is missing."""
    store = SessionStore(root_dir=str(tmp_path))
    root = store.root_dir
    if os.path.exists(root):
        shutil.rmtree(root)
    assert store.list_sessions() == []


def test_session_store_list_tags(tmp_path):
    """List stored tags for sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.tag_session(session_id, "primary")
    tags = store.list_tags()
    assert tags["primary"] == session_id


def test_session_store_archive_roundtrip(tmp_path):
    """Archive and unarchive sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.is_archived(session_id) is False
    store.archive_session(session_id)
    assert store.is_archived(session_id) is True
    store.unarchive_session(session_id)
    assert store.is_archived(session_id) is False


def test_session_store_terminate_roundtrip(tmp_path):
    """Terminate persists a stable timestamp; set-once and irreversible."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.is_terminated(session_id) is False
    assert store.get_terminated_at(session_id) is None

    assert store.terminate_session(session_id) is True
    first = store.get_terminated_at(session_id)
    assert first is not None
    assert store.is_terminated(session_id) is True

    # Set-once: a repeat terminate never moves the original timestamp, and
    # reports False — the arbitration signal SessionRuntime gates side
    # effects on (concurrency fix).
    assert store.terminate_session(session_id) is False
    assert store.get_terminated_at(session_id) == first

    # Survives a fresh store instance (durable in index.json).
    reopened = SessionStore(root_dir=str(tmp_path))
    assert reopened.is_terminated(session_id) is True
    assert reopened.get_terminated_at(session_id) == first


def test_append_event_dropped_after_terminate(tmp_path):
    """An append after terminate() is dropped (no-op), logged once, never raises."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "before"}})

    store.terminate_session(session_id)

    with patch("mewbo_core.session_store.logging") as mock_logging:
        store.append_event(session_id, {"type": "user", "payload": {"text": "after-1"}})
        store.append_event(session_id, {"type": "user", "payload": {"text": "after-2"}})
        # Two dropped appends, one structured-log call.
        assert mock_logging.warning.call_count == 1

    events = store.load_transcript(session_id)
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "before"


def test_append_event_written_before_terminate(tmp_path):
    """Events written before termination are unaffected by the guard."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    assert len(store.load_transcript(session_id)) == 1


def test_session_store_terminate_isolated(tmp_path):
    """Terminating one session never marks a sibling terminated."""
    store = SessionStore(root_dir=str(tmp_path))
    a = store.create_session()
    b = store.create_session()
    store.terminate_session(a)
    assert store.is_terminated(a) is True
    assert store.is_terminated(b) is False


def test_create_session_store_default_json(tmp_path):
    """Factory returns SessionStore (json) when no driver is configured."""
    with patch("mewbo_core.session_store.get_config_value", return_value="json"):
        store = create_session_store(root_dir=str(tmp_path))
    assert isinstance(store, SessionStore)
    assert isinstance(store, SessionStoreBase)


def test_create_session_store_mongodb(tmp_path):
    """Factory returns MongoSessionStore when driver is 'mongodb'."""
    import mongomock
    from mewbo_core.session_store_mongo import MongoSessionStore

    with (
        patch("mewbo_core.session_store.get_config_value", return_value="mongodb"),
        patch("mewbo_core.session_store_mongo.MongoClient", mongomock.MongoClient),
    ):
        store = create_session_store(root_dir=str(tmp_path))
    assert isinstance(store, MongoSessionStore)
    assert isinstance(store, SessionStoreBase)


def test_base_class_template_fork(tmp_path):
    """Verify fork_session works through the base class template method."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.save_summary(session_id, "summary text")
    store.save_title(session_id, "my title")

    forked_id = store.fork_session(session_id)
    assert forked_id != session_id
    events = store.load_transcript(forked_id)
    assert len(events) == 1
    assert store.load_summary(forked_id) == "summary text"
    assert store.load_title(forked_id) == "my title"


def test_fork_session_at(tmp_path):
    """Fork only events up to cutoff_ts and clear stale summary."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "q1"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(session_id, {"type": "user", "payload": {"text": "q2"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "a2"}})
    store.save_summary(session_id, "full session summary")
    store.save_title(session_id, "my title")

    events = store.load_transcript(session_id)
    assert len(events) == 4
    # Fork at the first assistant response (keep first 2 events)
    cutoff_ts = events[1]["ts"]
    forked_id = store.fork_session_at(session_id, cutoff_ts)

    assert forked_id != session_id
    forked_events = store.load_transcript(forked_id)
    assert len(forked_events) == 2
    assert forked_events[0]["payload"]["text"] == "q1"
    assert forked_events[1]["payload"]["text"] == "a1"
    # Summary should be cleared (stale after truncation)
    assert store.load_summary(forked_id) == ""
    # Title is preserved
    assert store.load_title(forked_id) == "my title"
    # Source session is unmodified
    assert len(store.load_transcript(session_id)) == 4
    assert store.load_summary(session_id) == "full session summary"


def test_last_attestation_hash_defaults_genesis(tmp_path):
    """No attestation events yet -> genesis hash."""
    from mewbo_core.attestation import GENESIS_HASH

    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.last_attestation_hash(session_id) == GENESIS_HASH


def test_last_attestation_hash_reads_most_recent_record(tmp_path):
    """Scans for the LAST attestation event's record_hash, ignoring other events."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        session_id, {"type": "attestation", "payload": {"record_hash": "hash-one"}}
    )
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi back"}})
    store.append_event(
        session_id, {"type": "attestation", "payload": {"record_hash": "hash-two"}}
    )
    assert store.last_attestation_hash(session_id) == "hash-two"


def test_last_attestation_hash_reseeds_chain_continuously(tmp_path):
    """A chain re-seeded from a pre-existing tail links continuously (recovery)."""
    from mewbo_core.attestation import AttestationChain

    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(
        session_id, {"type": "attestation", "payload": {"record_hash": "prior-tail-hash"}}
    )

    seed = store.last_attestation_hash(session_id)
    assert seed == "prior-tail-hash"

    events: list = []
    chain = AttestationChain(session_id=session_id, head=seed)
    chain.record_spawn(
        events.append,
        agent_id="a1",
        parent_id=None,
        depth=1,
        agent_type=None,
        model="test-model",
        capability_mode="all",
        contract=None,
    )
    assert events[0]["payload"]["prev_hash"] == seed


def test_session_store_title_roundtrip(tmp_path):
    """Persist and reload session titles."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.load_title(session_id) is None
    store.save_title(session_id, "a concise title")
    assert store.load_title(session_id) == "a concise title"
    # Overwrite semantics
    store.save_title(session_id, "edited title")
    assert store.load_title(session_id) == "edited title"


def test_session_store_load_title_missing(tmp_path):
    """Return None when no title was ever saved."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.load_title(session_id) is None


def test_session_store_load_title_empty_string(tmp_path):
    """Treat an empty stored title as absent."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.save_title(session_id, "")
    assert store.load_title(session_id) is None


def test_unknown_storage_driver_raises():
    """Unknown storage driver should raise, not silently fall back to json."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MEWBO_STORAGE_DRIVER", None)
        with pytest.raises(ValidationError, match="Unknown storage driver"):
            StorageConfig(driver="postgres")


def test_create_session_store_mongodb_unreachable(tmp_path):
    """Factory raises RuntimeError when MongoDB is unreachable."""
    with (
        patch("mewbo_core.session_store.get_config_value", return_value="mongodb"),
        patch(
            "mewbo_core.session_store_mongo.MongoClient",
            side_effect=Exception("connection refused"),
        ),
    ):
        with pytest.raises(RuntimeError, match="not available"):
            create_session_store(root_dir=str(tmp_path))
