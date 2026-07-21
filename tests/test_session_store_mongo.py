"""Tests for MongoDB session store driver."""

from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.session_store import SessionStoreBase
from mewbo_core.session_store_mongo import MongoSessionStore


@pytest.fixture()
def mongo_store(tmp_path):
    """Create a MongoSessionStore backed by mongomock."""
    with patch(
        "mewbo_core.session_store_mongo.MongoClient",
        mongomock.MongoClient,
    ):
        store = MongoSessionStore(
            root_dir=str(tmp_path),
            uri="mongodb://localhost:27017",
            database="test_mewbo",
        )
    return store


def test_isinstance(mongo_store):
    """MongoSessionStore is a SessionStoreBase."""
    assert isinstance(mongo_store, SessionStoreBase)


def test_roundtrip(mongo_store):
    """Create session, append events, load transcript, save/load summary."""
    session_id = mongo_store.create_session()
    assert isinstance(session_id, str) and len(session_id) == 32

    mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    mongo_store.append_event(session_id, {"type": "tool_result", "payload": {"text": "ok"}})

    events = mongo_store.load_transcript(session_id)
    assert len(events) == 2
    assert events[0]["type"] == "user"
    assert events[1]["type"] == "tool_result"
    assert "ts" in events[0]

    mongo_store.save_summary(session_id, "summary text")
    assert mongo_store.load_summary(session_id) == "summary text"


def test_list_sessions(mongo_store):
    """List sessions returns sorted IDs."""
    id1 = mongo_store.create_session()
    id2 = mongo_store.create_session()
    sessions = mongo_store.list_sessions()
    assert id1 in sessions
    assert id2 in sessions
    assert sessions == sorted(sessions)


def test_recent_events_and_filters(mongo_store):
    """Filter recent events by type and respect zero limits."""
    session_id = mongo_store.create_session()
    mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    mongo_store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi"}})
    mongo_store.append_event(session_id, {"type": "tool_result", "payload": {"text": "ok"}})

    assert mongo_store.load_recent_events(session_id, limit=0) == []
    filtered = mongo_store.load_recent_events(session_id, limit=5, include_types={"tool_result"})
    assert len(filtered) == 1
    assert filtered[0]["type"] == "tool_result"


def test_load_recent_events_bounded_tail_order(mongo_store):
    """The Mongo override returns the LAST n events in ascending order.

    The override sorts ``ts`` DESC + ``limit`` at the store (a bounded range
    read, not a full-transcript load) then reverses. Explicit distinct ``ts``
    values keep the assertion deterministic under mongomock.
    """
    session_id = mongo_store.create_session()
    for i in range(5):
        mongo_store.append_event(
            session_id,
            {"type": "user", "payload": {"n": i}, "ts": f"2026-07-19T00:00:0{i}+00:00"},
        )

    tail = mongo_store.load_recent_events(session_id, limit=3)
    assert [e["payload"]["n"] for e in tail] == [2, 3, 4]
    # Whole-tail request returns everything, still ascending.
    assert [e["payload"]["n"] for e in mongo_store.load_recent_events(session_id, limit=10)] == [
        0,
        1,
        2,
        3,
        4,
    ]


def test_tag_and_resolve(mongo_store):
    """Tag a session and resolve it back."""
    session_id = mongo_store.create_session()
    mongo_store.tag_session(session_id, "primary")

    assert mongo_store.resolve_tag("primary") == session_id
    assert mongo_store.resolve_tag("nonexistent") is None

    tags = mongo_store.list_tags()
    assert tags["primary"] == session_id


def test_tag_overwrite(mongo_store):
    """Re-tagging updates the mapping."""
    id1 = mongo_store.create_session()
    id2 = mongo_store.create_session()
    mongo_store.tag_session(id1, "latest")
    mongo_store.tag_session(id2, "latest")
    assert mongo_store.resolve_tag("latest") == id2


def test_archive_roundtrip(mongo_store):
    """Archive and unarchive sessions."""
    session_id = mongo_store.create_session()
    assert mongo_store.is_archived(session_id) is False

    mongo_store.archive_session(session_id)
    assert mongo_store.is_archived(session_id) is True

    mongo_store.unarchive_session(session_id)
    assert mongo_store.is_archived(session_id) is False


def test_terminate_roundtrip(mongo_store):
    """Terminate stamps a stable set-once timestamp on the sessions doc."""
    session_id = mongo_store.create_session()
    assert mongo_store.is_terminated(session_id) is False
    assert mongo_store.get_terminated_at(session_id) is None

    assert mongo_store.terminate_session(session_id) is True
    first = mongo_store.get_terminated_at(session_id)
    assert first is not None
    assert mongo_store.is_terminated(session_id) is True

    # Set-once: the ``terminated_at: None`` filter clause means a repeat call
    # matches nothing (modified_count == 0) and leaves the original timestamp
    # untouched — False is the arbitration signal SessionRuntime gates its
    # termination side effects on (concurrency fix).
    assert mongo_store.terminate_session(session_id) is False
    assert mongo_store.get_terminated_at(session_id) == first


def test_last_attestation_hash_defaults_genesis(mongo_store):
    """No attestation events yet -> genesis hash."""
    from mewbo_core.attestation import GENESIS_HASH

    session_id = mongo_store.create_session()
    assert mongo_store.last_attestation_hash(session_id) == GENESIS_HASH


def test_last_attestation_hash_targeted_query(mongo_store):
    """Overridden targeted query returns the most recent record, ignoring other events."""
    session_id = mongo_store.create_session()
    mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    mongo_store.append_event(
        session_id, {"type": "attestation", "payload": {"record_hash": "hash-one"}}
    )
    mongo_store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi back"}})
    mongo_store.append_event(
        session_id, {"type": "attestation", "payload": {"record_hash": "hash-two"}}
    )
    assert mongo_store.last_attestation_hash(session_id) == "hash-two"


def test_last_attestation_hash_reseeds_chain_continuously(mongo_store):
    """A chain re-seeded from a pre-existing tail links continuously (recovery)."""
    from mewbo_core.attestation import AttestationChain

    session_id = mongo_store.create_session()
    mongo_store.append_event(
        session_id, {"type": "attestation", "payload": {"record_hash": "prior-tail-hash"}}
    )

    seed = mongo_store.last_attestation_hash(session_id)
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


def test_append_event_dropped_after_terminate(mongo_store):
    """An append after terminate() is dropped (no-op), logged once, never raises."""
    session_id = mongo_store.create_session()
    mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "before"}})

    mongo_store.terminate_session(session_id)

    # _guard_append is inherited from SessionStoreBase (session_store.py), so
    # its logger call resolves against THAT module's namespace regardless of
    # which concrete backend is calling it.
    with patch("mewbo_core.session_store.logging") as mock_logging:
        mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "after-1"}})
        mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "after-2"}})
        # Two dropped appends, one structured-log call.
        assert mock_logging.warning.call_count == 1

    events = mongo_store.load_transcript(session_id)
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "before"


def test_append_event_written_before_terminate(mongo_store):
    """Events written before termination are unaffected by the guard."""
    session_id = mongo_store.create_session()
    mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    assert len(mongo_store.load_transcript(session_id)) == 1


def test_is_terminated_missing(mongo_store):
    """is_terminated returns False for a nonexistent session."""
    assert mongo_store.is_terminated("nonexistent") is False
    assert mongo_store.get_terminated_at("nonexistent") is None


def test_fork_session(mongo_store):
    """Fork copies events, summary, and title to a new session."""
    session_id = mongo_store.create_session()
    mongo_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    mongo_store.save_summary(session_id, "test summary")
    mongo_store.save_title(session_id, "my title")

    forked_id = mongo_store.fork_session(session_id)
    assert forked_id != session_id
    assert len(mongo_store.load_transcript(forked_id)) == 1
    assert mongo_store.load_summary(forked_id) == "test summary"
    assert mongo_store.load_title(forked_id) == "my title"


def test_title_roundtrip(mongo_store):
    """Mongo driver persists and reloads titles."""
    session_id = mongo_store.create_session()
    assert mongo_store.load_title(session_id) is None
    mongo_store.save_title(session_id, "concise title")
    assert mongo_store.load_title(session_id) == "concise title"
    mongo_store.save_title(session_id, "edited")
    assert mongo_store.load_title(session_id) == "edited"


def test_title_missing(mongo_store):
    """load_title returns None for nonexistent sessions."""
    assert mongo_store.load_title("nonexistent") is None


def test_session_dir(mongo_store, tmp_path):
    """session_dir returns a local filesystem path and creates it."""
    session_id = mongo_store.create_session()
    path = mongo_store.session_dir(session_id)
    assert str(tmp_path) in path
    assert session_id in path
    import os

    assert os.path.isdir(path)


def test_summary_missing(mongo_store):
    """load_summary returns None for nonexistent sessions."""
    assert mongo_store.load_summary("nonexistent") is None


def test_is_archived_missing(mongo_store):
    """is_archived returns False for nonexistent sessions."""
    assert mongo_store.is_archived("nonexistent") is False
