"""Tests for JsonTriggerStore (mewbo_core.triggers.store).

Covers CRUD, list() filtering, list_armed()/cancel_for_session() template
methods (shared with MongoTriggerStore via TriggerStoreBase), and the
create_trigger_store() driver factory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from mewbo_core.triggers.spec import CronTrigger, TimeAtTrigger
from mewbo_core.triggers.store import (
    JsonTriggerStore,
    TriggerStoreBase,
    create_trigger_store,
)

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    """A JsonTriggerStore scoped to a tmp file."""
    return JsonTriggerStore(data_file=tmp_path / "triggers.json")


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


def test_isinstance():
    """JsonTriggerStore satisfies TriggerStoreBase."""
    assert issubclass(JsonTriggerStore, TriggerStoreBase)


def test_create_and_get_roundtrip(store):
    """create() persists; get() recovers the exact concrete kind."""
    trigger = _time_at()
    store.create(trigger)

    fetched = store.get(trigger.id)
    assert fetched is not None
    assert type(fetched) is TimeAtTrigger
    assert fetched.id == trigger.id
    assert fetched.session_id == trigger.session_id


def test_get_missing_returns_none(store):
    """get() on an unknown id returns None, not an exception."""
    assert store.get("does-not-exist") is None


def test_list_filters_by_session_kind_status(store):
    """list() composes session_id/kind/status filters."""
    a = _time_at(session_id="s1")
    b = _cron(session_id="s1")
    c = _cron(session_id="s2")
    store.create(a)
    store.create(b)
    store.create(c)

    assert {t.id for t in store.list()} == {a.id, b.id, c.id}
    assert {t.id for t in store.list(session_id="s1")} == {a.id, b.id}
    assert {t.id for t in store.list(kind="time.cron")} == {b.id, c.id}
    assert {t.id for t in store.list(session_id="s1", kind="time.at")} == {a.id}

    b.transition("paused")
    store.update(b)
    assert {t.id for t in store.list(status="armed")} == {a.id, c.id}
    assert {t.id for t in store.list(status="paused")} == {b.id}


def test_list_limit(store):
    """limit truncates the result list."""
    for _ in range(5):
        store.create(_cron(session_id="s1"))
    assert len(store.list(limit=2)) == 2
    assert len(store.list()) == 5


def test_update_replaces_full_document(store):
    """update() persists mutations made to the in-memory trigger object."""
    trigger = _time_at()
    store.create(trigger)

    trigger.transition("paused")
    trigger.last_error = "manual pause"
    result = store.update(trigger)
    assert result.status == "paused"

    fetched = store.get(trigger.id)
    assert fetched.status == "paused"
    assert fetched.last_error == "manual pause"


def test_update_missing_raises_keyerror(store):
    """update() on a trigger never create()'d raises KeyError."""
    trigger = _time_at()
    with pytest.raises(KeyError):
        store.update(trigger)


def test_list_armed_template_method(store):
    """list_armed() is list(status='armed') — built on the abstract list() primitive."""
    a = _time_at()
    b = _cron()
    store.create(a)
    store.create(b)
    b.transition("cancelled")
    store.update(b)

    armed = store.list_armed()
    assert {t.id for t in armed} == {a.id}


def test_cancel_for_session_cascades_non_terminal_only(store):
    """cancel_for_session cancels every non-terminal trigger for the session, skips terminals."""
    armed = _time_at(session_id="s1")
    paused = _cron(session_id="s1")
    already_done = _cron(session_id="s1")
    other_session = _cron(session_id="s2")

    store.create(armed)
    store.create(paused)
    store.create(already_done)
    store.create(other_session)

    paused.transition("paused")
    store.update(paused)
    already_done.transition("completed")
    store.update(already_done)

    count = store.cancel_for_session("s1")
    assert count == 2  # armed + paused; already_done untouched

    assert store.get(armed.id).status == "cancelled"
    assert store.get(paused.id).status == "cancelled"
    assert store.get(already_done.id).status == "completed"
    assert store.get(other_session.id).status == "armed"


def test_cancel_for_session_empty_is_zero(store):
    """cancel_for_session on a session with no triggers is a zero-count no-op."""
    assert store.cancel_for_session("nobody") == 0


def test_persists_across_instances(tmp_path):
    """Data written by one store instance is visible from a fresh instance on the same file."""
    path = tmp_path / "triggers.json"
    store1 = JsonTriggerStore(data_file=path)
    trigger = _time_at()
    store1.create(trigger)

    store2 = JsonTriggerStore(data_file=path)
    assert store2.get(trigger.id) is not None


def test_default_data_file_from_config(tmp_path):
    """With no explicit data_file, the store resolves one under runtime.config_dir."""
    with patch(
        "mewbo_core.triggers.store.get_config_value",
        return_value=str(tmp_path),
    ):
        store = JsonTriggerStore()
    assert store._data_file == tmp_path / "triggers.json"


def test_create_trigger_store_defaults_to_json(tmp_path):
    """create_trigger_store() returns a JsonTriggerStore when storage.driver is unset/json."""
    with patch("mewbo_core.triggers.store.get_config_value", return_value="json"):
        result = create_trigger_store(data_file=tmp_path / "triggers.json")
    assert isinstance(result, JsonTriggerStore)


def test_create_trigger_store_mongodb_driver_dispatches():
    """create_trigger_store() imports and constructs MongoTriggerStore for the mongodb driver."""
    with (
        patch("mewbo_core.triggers.store.get_config_value", return_value="mongodb"),
        patch("mewbo_core.triggers.store_mongo.MongoTriggerStore") as mock_cls,
    ):
        mock_cls.return_value = "sentinel-mongo-store"
        result = create_trigger_store()
    assert result == "sentinel-mongo-store"
    mock_cls.assert_called_once_with()
