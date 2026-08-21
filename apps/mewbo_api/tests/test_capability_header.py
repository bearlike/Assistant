"""Tests for X-Mewbo-Capabilities header parsing in the API.

The persisted list is NORMALISED — sorted and deduped — not stored in the order
the client happened to send. That is `mewbo_core.capabilities.parse_capability_header`,
the one wire seam, matching its siblings `parse_capabilities` (manifests) and
`augment_session_capabilities`, both of which already sorted. Capabilities are
consumed with SET semantics everywhere (`filter_by_capabilities`, `issubset`,
membership), so header order carries no meaning, and normalising it means two
clients advertising the same set persist the same payload.
"""

# mypy: ignore-errors
from mewbo_api import backend
from mewbo_core.session.session_store import SessionStore


def _reset_backend(tmp_path, monkeypatch):
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


def test_session_create_stores_client_capabilities(monkeypatch, tmp_path):
    """X-Mewbo-Capabilities header is parsed and persisted in context event."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()

    resp = client.post(
        "/api/sessions",
        json={},
        headers={
            "X-API-KEY": backend.MASTER_API_TOKEN,
            "X-Mewbo-Capabilities": "stlite, foo",
        },
    )
    assert resp.status_code == 200
    session_id = resp.get_json()["session_id"]

    events = backend.session_store.load_transcript(session_id)
    ctx_events = [e for e in events if e.get("type") == "context"]
    caps = next(
        (
            e["payload"].get("client_capabilities")
            for e in ctx_events
            if "client_capabilities" in e.get("payload", {})
        ),
        None,
    )
    # Sorted, not header order — see the module docstring. `foo` is unknown to the
    # first-party registry and is deliberately KEPT: a third-party plugin's
    # capability must survive the parse or its gate silently stops working.
    assert caps == ["foo", "stlite"]


def test_session_create_without_header_stores_no_capabilities(monkeypatch, tmp_path):
    """Session creation without the capabilities header writes no client_capabilities."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()

    resp = client.post(
        "/api/sessions",
        json={},
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.status_code == 200
    session_id = resp.get_json()["session_id"]

    events = backend.session_store.load_transcript(session_id)
    ctx_events = [e for e in events if e.get("type") == "context"]
    for e in ctx_events:
        assert "client_capabilities" not in e.get("payload", {})


def test_session_create_strips_whitespace_from_capabilities(monkeypatch, tmp_path):
    """Whitespace around capability tokens is stripped during parsing."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()

    resp = client.post(
        "/api/sessions",
        json={},
        headers={
            "X-API-KEY": backend.MASTER_API_TOKEN,
            "X-Mewbo-Capabilities": "  stlite  ,  other-feature  ",
        },
    )
    assert resp.status_code == 200
    session_id = resp.get_json()["session_id"]

    events = backend.session_store.load_transcript(session_id)
    ctx_events = [e for e in events if e.get("type") == "context"]
    caps = next(
        (
            e["payload"].get("client_capabilities")
            for e in ctx_events
            if "client_capabilities" in e.get("payload", {})
        ),
        None,
    )
    # Whitespace stripped, then sorted — see the module docstring.
    assert caps == ["other-feature", "stlite"]
