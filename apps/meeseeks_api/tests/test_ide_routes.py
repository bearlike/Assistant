"""Tests for the ``ide_routes`` Flask-RESTX namespace."""

# mypy: ignore-errors
# ruff: noqa: D101, D102, D103
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask import Flask
from flask_restx import Api
from meeseeks_api import ide_routes
from meeseeks_api.ide import (
    DockerUnavailable,
    IdeInstance,
    MaxLifetimeReached,
)

UTC = timezone.utc
VALID_SID = "a" * 32
INVALID_SID = "not-a-valid-session-id"


def _make_instance(sid: str = VALID_SID) -> IdeInstance:
    now = datetime.now(UTC)
    return IdeInstance(
        session_id=sid,
        status="ready",
        project_name="demo",
        project_path="/tmp/demo",
        password="pw",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        max_deadline=now + timedelta(hours=8),
        extensions=0,
        cpus=1.0,
        memory="1g",
    )


@pytest.fixture
def fake_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture
def fake_runtime() -> MagicMock:
    rt = MagicMock()
    rt.session_store.list_sessions.return_value = [VALID_SID]
    rt.session_store.load_transcript.return_value = [
        {"type": "context", "payload": {"project": "demo"}}
    ]
    return rt


@pytest.fixture
def client(
    fake_manager: MagicMock, fake_runtime: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> Any:
    # Patch project lookup used by _resolve_session_project.
    class FakeProject:
        path = "/tmp/demo"

    class FakeCfg:
        projects = {"demo": FakeProject()}

    monkeypatch.setattr(
        "meeseeks_core.config.get_config", lambda: FakeCfg()
    )

    app = Flask("ide-test")
    api = Api(app)
    api.add_namespace(ide_routes.ide_ns, path="/api")
    ide_routes.init_ide(fake_manager, fake_runtime, lambda: None)
    return app.test_client()


# ---------------------------------------------------------------------------
# POST /ide
# ---------------------------------------------------------------------------


def test_post_creates_returns_201(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.ensure.return_value = (_make_instance(), True)
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["session_id"] == VALID_SID
    assert body["url"] == f"/ide/{VALID_SID}/"
    assert body["project_name"] == "demo"
    assert body["password"] == "pw"


def test_post_reconnect_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.ensure.return_value = (_make_instance(), False)
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 200


def test_post_rejects_invalid_session_id(client: Any) -> None:
    resp = client.post(f"/api/sessions/{INVALID_SID}/ide")
    assert resp.status_code == 404


def test_post_404_when_session_unknown(
    client: Any, fake_runtime: MagicMock
) -> None:
    fake_runtime.session_store.list_sessions.return_value = []
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 404


def test_post_409_when_session_has_no_project(
    client: Any, fake_runtime: MagicMock
) -> None:
    fake_runtime.session_store.load_transcript.return_value = [
        {"type": "context", "payload": {}}
    ]
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 409


def test_post_503_when_docker_down(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.ensure.side_effect = DockerUnavailable("no sock")
    resp = client.post(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /ide
# ---------------------------------------------------------------------------


def test_get_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.get.return_value = _make_instance()
    resp = client.get(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 200
    assert resp.get_json()["session_id"] == VALID_SID


def test_get_returns_404_when_absent(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.get.return_value = None
    resp = client.get(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 404


def test_get_invalid_session_id_returns_404(client: Any) -> None:
    resp = client.get(f"/api/sessions/{INVALID_SID}/ide")
    assert resp.status_code == 404


def test_get_503_when_docker_down(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.get.side_effect = DockerUnavailable("nope")
    resp = client.get(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# DELETE /ide
# ---------------------------------------------------------------------------


def test_delete_returns_204(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.stop.return_value = True
    resp = client.delete(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 204


def test_delete_returns_404_when_nothing(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.stop.return_value = False
    resp = client.delete(f"/api/sessions/{VALID_SID}/ide")
    assert resp.status_code == 404


def test_delete_invalid_session_id_returns_404(client: Any) -> None:
    resp = client.delete(f"/api/sessions/{INVALID_SID}/ide")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /ide/extend
# ---------------------------------------------------------------------------


def test_extend_hours_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.return_value = _make_instance()
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    fake_manager.extend.assert_called_once()
    call_kwargs = fake_manager.extend.call_args.kwargs
    assert call_kwargs["hours"] == 1


def test_extend_absolute_returns_200(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.return_value = _make_instance()
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"expires_at": future}),
        content_type="application/json",
    )
    assert resp.status_code == 200


def test_extend_rejects_both_fields(client: Any) -> None:
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1, "expires_at": future}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_extend_rejects_neither(client: Any) -> None:
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_extend_409_at_cap(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.side_effect = MaxLifetimeReached(
        datetime.now(UTC) + timedelta(hours=8)
    )
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 100}),
        content_type="application/json",
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "max_lifetime_reached"
    assert "max_deadline" in body


def test_extend_404_when_missing(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.side_effect = LookupError("missing")
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_extend_invalid_session_id_returns_404(client: Any) -> None:
    resp = client.post(
        f"/api/sessions/{INVALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_extend_503_when_docker_down(client: Any, fake_manager: MagicMock) -> None:
    fake_manager.extend.side_effect = DockerUnavailable("no sock")
    resp = client.post(
        f"/api/sessions/{VALID_SID}/ide/extend",
        data=json.dumps({"hours": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 503
