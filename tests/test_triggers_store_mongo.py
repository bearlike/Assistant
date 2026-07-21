"""Tests for MongoTriggerStore (mewbo_core.triggers.store_mongo).

Backed by mongomock, following the tests/test_session_store_mongo.py
pattern: patch MongoClient at the module import site before constructing
the store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.triggers.spec import CronTrigger, TimeAtTrigger, parse_trigger
from mewbo_core.triggers.store import TriggerStoreBase
from mewbo_core.triggers.store_mongo import MongoTriggerStore

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    """A MongoTriggerStore backed by mongomock."""
    with patch("mewbo_core.triggers.store_mongo.MongoClient", mongomock.MongoClient):
        return MongoTriggerStore(uri="mongodb://localhost:27017", database="test_triggers")


def _time_at(**overrides):
    defaults = dict(
        session_id="s1", wake_prompt="wake up", created_by="user", at=NOW + timedelta(hours=1)
    )
    defaults.update(overrides)
    return TimeAtTrigger(**defaults)


def _cron(**overrides):
    defaults = dict(session_id="s1", wake_prompt="w", created_by="agent", cron="*/5 * * * *")
    defaults.update(overrides)
    return CronTrigger(**defaults)


def test_isinstance(store):
    """MongoTriggerStore satisfies TriggerStoreBase."""
    assert isinstance(store, TriggerStoreBase)


def test_create_and_get_roundtrip(store):
    """create() persists; get() recovers the exact concrete kind via the discriminator."""
    trigger = _time_at()
    store.create(trigger)

    fetched = store.get(trigger.id)
    assert fetched is not None
    assert type(fetched) is TimeAtTrigger
    assert fetched.id == trigger.id


def test_get_missing_returns_none(store):
    """get() on an unknown id returns None."""
    assert store.get("nope") is None


def test_list_filters(store):
    """list() composes session_id/kind/status filters, same contract as JsonTriggerStore."""
    a = _time_at(session_id="s1")
    b = _cron(session_id="s1")
    c = _cron(session_id="s2")
    store.create(a)
    store.create(b)
    store.create(c)

    assert {t.id for t in store.list(session_id="s1")} == {a.id, b.id}
    assert {t.id for t in store.list(kind="time.cron")} == {b.id, c.id}


def test_list_limit(store):
    """limit truncates the cursor."""
    for _ in range(4):
        store.create(_cron(session_id="s1"))
    assert len(store.list(limit=2)) == 2


def test_update_replaces_and_returns_document(store):
    """update() persists mutations and returns the reloaded document."""
    trigger = _time_at()
    store.create(trigger)

    trigger.transition("paused")
    result = store.update(trigger)
    assert result.status == "paused"
    assert store.get(trigger.id).status == "paused"


def test_update_missing_raises_keyerror(store):
    """update() on a never-created trigger raises KeyError."""
    with pytest.raises(KeyError):
        store.update(_time_at())


def test_list_armed_template_method(store):
    """list_armed() (inherited from TriggerStoreBase) filters to status=armed."""
    a = _time_at()
    b = _cron()
    store.create(a)
    store.create(b)
    b.transition("failed")
    store.update(b)

    assert {t.id for t in store.list_armed()} == {a.id}


def test_cancel_for_session_cascade(store):
    """cancel_for_session (inherited template method) cascades over the Mongo backend too."""
    armed = _time_at(session_id="s1")
    done = _cron(session_id="s1")
    other = _cron(session_id="s2")
    store.create(armed)
    store.create(done)
    store.create(other)

    done.transition("completed")
    store.update(done)

    count = store.cancel_for_session("s1")
    assert count == 1

    assert store.get(armed.id).status == "cancelled"
    assert store.get(done.id).status == "completed"
    assert store.get(other.id).status == "armed"


def test_id_index_is_unique(store):
    """The 'id' index enforces uniqueness at the Mongo layer."""
    trigger = _time_at()
    store.create(trigger)
    with pytest.raises(Exception):  # duplicate key error from mongomock
        store._col.insert_one(trigger.model_dump(mode="json"))


def test_discriminated_union_round_trip_matches_json_store(store):
    """The same serialized trigger round-trips identically through JSON and Mongo parsing."""
    trigger = _cron()
    data = trigger.model_dump(mode="json")
    store.create(trigger)

    from_mongo = store.get(trigger.id)
    from_parse_trigger = parse_trigger(data)

    assert type(from_mongo) is type(from_parse_trigger) is CronTrigger
    assert from_mongo.model_dump(mode="json") == from_parse_trigger.model_dump(mode="json")
