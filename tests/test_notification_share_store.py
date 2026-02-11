"""Tests for notification and share stores."""

from meeseeks_core.config import set_config_override
from meeseeks_core.notifications import NotificationStore
from meeseeks_core.share_store import ShareStore


def test_notification_store_handles_corrupt_json(tmp_path):
    """Return empty list when stored JSON is invalid."""
    set_config_override({"runtime": {"session_dir": str(tmp_path)}})
    store = NotificationStore()
    with open(store._path, "w", encoding="utf-8") as handle:
        handle.write("{invalid}")
    assert store.list() == []


def test_notification_store_dismiss_and_clear(tmp_path):
    """Dismiss and clear notifications with edge cases."""
    store = NotificationStore(root_dir=str(tmp_path))
    first = store.add(title="one", message="first")
    second = store.add(title="two", message="second")
    assert store.dismiss([]) == 0
    assert store.dismiss([first["id"], second["id"]]) == 2
    removed = store.clear(dismissed_only=False)
    assert removed == 2
    assert store.list(include_dismissed=True) == []


def test_notification_store_ignores_non_list_payload(tmp_path):
    """Return empty list when stored JSON is not a list."""
    set_config_override({"runtime": {"session_dir": str(tmp_path)}})
    store = NotificationStore()
    with open(store._path, "w", encoding="utf-8") as handle:
        handle.write('{"foo": "bar"}')
    assert store.list() == []


def test_share_store_handles_invalid_json_and_tokens(tmp_path):
    """Handle corrupt JSON and missing token cases."""
    set_config_override({"runtime": {"session_dir": str(tmp_path)}})
    store = ShareStore()
    with open(store._path, "w", encoding="utf-8") as handle:
        handle.write("{invalid}")
    assert store.resolve("") is None
    assert store.resolve("token") is None
    assert store.revoke("") is False
    assert store.revoke("missing") is False
    with open(store._path, "w", encoding="utf-8") as handle:
        handle.write('["token"]')
    assert store.resolve("token") is None


def test_share_store_revokes_existing_token(tmp_path):
    """Revoke a valid share token."""
    store = ShareStore(root_dir=str(tmp_path))
    record = store.create("session-1")
    assert store.revoke(record["token"]) is True
