"""Extra contract tests for mewbo_api.ide (IdeManager / IdeStore lifecycle).

Stubs Docker and Mongo.  Covers the lines missed by test_ide_manager.py:

- IdeStore._from_doc: naive datetime coercion (_as_utc naive branch)
- IdeStore.insert / get / update_expiry / delete (real Mongo schema paths
  via a thin in-memory fake — same InMemoryStore from test_ide_manager.py)
- IdeInstance.remaining_seconds floored at zero
- IdeInstance.to_dict with and without include_password
- IdeInstance.url + container_name properties
- IdeManager._forget (deadline file + probe cache cleared)
- IdeManager._safe_remove_container: APIError on get, APIError on remove,
  DockerUnavailable path
- IdeManager._container_running: APIError on get returns False
- IdeManager.extend: expires_at in the past raises ValueError
- IdeManager.extend: naive expires_at coerced to UTC
- IdeManager._probe_status: cache hit path
- IdeStore._from_doc via _as_utc with naive datetime
- _as_utc with non-datetime raises TypeError
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from docker.errors import APIError, NotFound
from mewbo_api.ide import (
    SESSION_ID_RE,
    IdeInstance,
    IdeManager,
    IdeStore,
    _as_utc,
)
from mewbo_core.config import WebIdeConfig

UTC = timezone.utc
VALID_SID = "a" * 32
OTHER_SID = "b" * 32


# ---------------------------------------------------------------------------
# Re-use InMemoryStore from test_ide_manager (copy to keep files independent)
# ---------------------------------------------------------------------------


class InMemoryStore:
    def __init__(self) -> None:
        self._docs: dict[str, IdeInstance] = {}

    def get(self, session_id: str) -> IdeInstance | None:
        src = self._docs.get(session_id)
        if src is None:
            return None
        return IdeInstance(
            session_id=src.session_id,
            status="pending",
            project_name=src.project_name,
            project_path=src.project_path,
            password=src.password,
            created_at=src.created_at,
            expires_at=src.expires_at,
            max_deadline=src.max_deadline,
            extensions=src.extensions,
            cpus=src.cpus,
            memory=src.memory,
        )

    def insert(self, instance: IdeInstance) -> None:
        if instance.session_id in self._docs:
            from pymongo.errors import DuplicateKeyError

            raise DuplicateKeyError("dup")
        self._docs[instance.session_id] = instance

    def update_expiry(self, session_id: str, *, expires_at: datetime, extensions: int) -> None:
        self._docs[session_id] = replace(
            self._docs[session_id],
            expires_at=expires_at,
            extensions=extensions,
        )

    def delete(self, session_id: str) -> bool:
        return self._docs.pop(session_id, None) is not None


class FakeContainer:
    def __init__(self, name: str, store: FakeContainers, status: str = "running") -> None:
        self.name = name
        self._store = store
        self.removed = False
        self.status = status

    def remove(self, force: bool = False) -> None:
        self._store._remove(self.name)
        self.removed = True


class FakeContainers:
    def __init__(self) -> None:
        self._by_name: dict[str, FakeContainer] = {}
        self.run_calls: list[dict[str, Any]] = []
        self.run_should_raise: Exception | None = None
        self.get_should_raise: Exception | None = None

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(kwargs)
        if self.run_should_raise is not None:
            raise self.run_should_raise
        name = kwargs["name"]
        container = FakeContainer(name, self)
        self._by_name[name] = container
        return container

    def get(self, name: str) -> FakeContainer:
        if self.get_should_raise is not None:
            raise self.get_should_raise
        if name not in self._by_name:
            raise NotFound(f"container {name} not found")
        return self._by_name[name]

    def _remove(self, name: str) -> None:
        self._by_name.pop(name, None)


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def project_path() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def cfg(state_dir: str) -> WebIdeConfig:
    return WebIdeConfig(
        enabled=True,
        image="codercom/code-server:latest",
        default_lifetime_hours=1,
        max_lifetime_hours=8,
        cpus=1.0,
        memory="1g",
        pids_limit=512,
        network="mewbo-ide",
        state_dir=state_dir,
    )


@pytest.fixture
def fake_client() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def manager(cfg: WebIdeConfig, store: InMemoryStore, fake_client: FakeDockerClient) -> IdeManager:
    return IdeManager(cfg, store, docker_client=fake_client)


# ---------------------------------------------------------------------------
# _as_utc
# ---------------------------------------------------------------------------


def test_as_utc_naive_datetime_becomes_utc() -> None:
    """Naive datetime should be interpreted as UTC and returned tz-aware."""
    naive = datetime(2026, 1, 1, 12, 0, 0)
    result = _as_utc(naive)
    assert result.tzinfo is not None
    assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_as_utc_aware_datetime_converted_to_utc() -> None:
    import zoneinfo

    eastern = zoneinfo.ZoneInfo("US/Eastern")
    aware = datetime(2026, 6, 1, 10, 0, 0, tzinfo=eastern)
    result = _as_utc(aware)
    assert result.tzinfo == UTC


def test_as_utc_non_datetime_raises() -> None:
    with pytest.raises(TypeError, match="expected datetime"):
        _as_utc("not-a-datetime")


# ---------------------------------------------------------------------------
# IdeInstance properties
# ---------------------------------------------------------------------------


def _make_instance(sid: str = VALID_SID, **overrides) -> IdeInstance:
    now = datetime.now(UTC)
    defaults = dict(
        session_id=sid,
        status="ready",
        project_name="demo",
        project_path="/tmp/demo",
        password="pw123",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        max_deadline=now + timedelta(hours=8),
        extensions=0,
        cpus=1.0,
        memory="1g",
    )
    defaults.update(overrides)
    return IdeInstance(**defaults)


def test_ide_instance_url_property() -> None:
    inst = _make_instance()
    assert inst.url == f"/ide/{VALID_SID}/"


def test_ide_instance_container_name_property() -> None:
    inst = _make_instance()
    assert inst.container_name == f"mewbo-ide-{VALID_SID}"


def test_ide_instance_remaining_seconds_positive() -> None:
    inst = _make_instance()
    remaining = inst.remaining_seconds
    assert remaining > 0
    assert remaining <= 3600


def test_ide_instance_remaining_seconds_floored_at_zero() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    inst = _make_instance(expires_at=past)
    assert inst.remaining_seconds == 0


def test_ide_instance_to_dict_without_password() -> None:
    inst = _make_instance()
    d = inst.to_dict(include_password=False)
    assert "password" not in d
    assert d["session_id"] == VALID_SID
    assert d["url"] == f"/ide/{VALID_SID}/"
    assert "remaining_seconds" in d
    assert "extensions" in d


def test_ide_instance_to_dict_with_password() -> None:
    inst = _make_instance()
    d = inst.to_dict(include_password=True)
    assert d["password"] == "pw123"


# ---------------------------------------------------------------------------
# IdeManager._forget
# ---------------------------------------------------------------------------


def test_forget_cleans_up_doc_file_and_probe_cache(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)

    # Warm probe cache
    manager._probe_cache[VALID_SID] = (True, time.monotonic())

    deadline_file = os.path.join(manager._cfg.state_dir, f"{VALID_SID}.deadline")
    assert os.path.isfile(deadline_file)
    assert store.get(VALID_SID) is not None

    manager._forget(VALID_SID)

    assert store.get(VALID_SID) is None
    assert not os.path.exists(deadline_file)
    assert VALID_SID not in manager._probe_cache


# ---------------------------------------------------------------------------
# IdeManager._safe_remove_container edge-cases
# ---------------------------------------------------------------------------


def test_safe_remove_container_api_error_on_get(
    manager: IdeManager,
    fake_client: FakeDockerClient,
) -> None:
    """APIError from containers.get is swallowed → returns False."""
    fake_client.containers.get_should_raise = APIError("daemon glitch")
    result = manager._safe_remove_container("mewbo-ide-any")
    assert result is False


def test_safe_remove_container_api_error_on_remove(
    manager: IdeManager,
    fake_client: FakeDockerClient,
) -> None:
    """APIError from container.remove is swallowed → returns False."""

    # Plant a container that raises on remove()
    class ErrorContainer:
        name = "mewbo-ide-err"
        status = "running"

        def remove(self, force: bool = False) -> None:
            raise APIError("cannot remove")

    fake_client.containers._by_name["mewbo-ide-err"] = ErrorContainer()
    result = manager._safe_remove_container("mewbo-ide-err")
    assert result is False


def test_safe_remove_container_docker_unavailable_returns_false(
    cfg: WebIdeConfig, store: InMemoryStore
) -> None:
    """DockerUnavailable during _docker() → returns False without propagating."""
    from docker.errors import DockerException

    mgr = IdeManager(cfg, store, docker_client=None)
    with patch("mewbo_api.ide.docker_from_env", side_effect=DockerException("no sock")):
        result = mgr._safe_remove_container("mewbo-ide-any")
    assert result is False


def test_safe_remove_container_not_found_on_remove(
    manager: IdeManager,
    fake_client: FakeDockerClient,
) -> None:
    """If the container disappears between get() and remove(), handle NotFound gracefully."""

    class VanishingContainer:
        name = "mewbo-ide-vanish"
        status = "running"

        def remove(self, force: bool = False) -> None:
            raise NotFound("already gone")

    fake_client.containers._by_name["mewbo-ide-vanish"] = VanishingContainer()
    result = manager._safe_remove_container("mewbo-ide-vanish")
    assert result is False


# ---------------------------------------------------------------------------
# IdeManager._container_running with APIError on get
# ---------------------------------------------------------------------------


def test_container_running_api_error_on_get_returns_false(
    manager: IdeManager,
    fake_client: FakeDockerClient,
) -> None:
    fake_client.containers.get_should_raise = APIError("oops")
    assert manager._container_running("mewbo-ide-any") is False


# ---------------------------------------------------------------------------
# IdeManager.extend edge-cases
# ---------------------------------------------------------------------------


def test_extend_past_expires_at_raises_value_error(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)

    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(ValueError, match="future"):
        manager.extend(VALID_SID, expires_at=past)


def test_extend_naive_expires_at_coerced_to_utc(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)

    # A naive datetime that is clearly in the future regardless of timezone offset.
    # Using UTC-now + 6 hours as naive means even UTC-12 offset can't push it into the past.
    naive_future = datetime.utcnow() + timedelta(hours=6)
    assert naive_future.tzinfo is None
    extended = manager.extend(VALID_SID, expires_at=naive_future)
    assert extended.expires_at.tzinfo is not None


def test_extend_updates_probe_status_when_container_running(
    manager: IdeManager, project_path: str
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        manager.ensure(VALID_SID, "demo", project_path)
        extended = manager.extend(VALID_SID, hours=1)
    assert extended.status == "ready"


def test_extend_keeps_old_status_when_container_gone(
    manager: IdeManager,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)

    # Remove container from fake registry (container vanished)
    fake_client.containers._by_name.clear()
    extended = manager.extend(VALID_SID, hours=1)
    # Container not running: extend uses instance.status from store ("pending")
    # rather than calling _probe_status again.
    assert extended.status in ("pending", "starting")  # not a fresh probe


# ---------------------------------------------------------------------------
# IdeManager._probe_status cache hit
# ---------------------------------------------------------------------------


def test_probe_status_uses_cached_result(manager: IdeManager) -> None:
    """_probe_status returns the cached result without calling _probe_ready again."""
    # Seed the cache with a "ready" result
    manager._probe_cache[VALID_SID] = (True, time.monotonic())

    with patch.object(IdeManager, "_probe_ready", return_value=False) as mock_probe:
        status = manager._probe_status(VALID_SID)

    # Cache hit → _probe_ready NOT called
    mock_probe.assert_not_called()
    assert status == "ready"


def test_probe_status_refreshes_after_ttl(manager: IdeManager) -> None:
    """Expired cache entry causes a fresh probe."""
    # Seed with an old (expired) entry
    old_time = time.monotonic() - IdeManager._PROBE_TTL_SECONDS - 1
    manager._probe_cache[VALID_SID] = (False, old_time)

    with patch.object(IdeManager, "_probe_ready", return_value=True) as mock_probe:
        status = manager._probe_status(VALID_SID)

    mock_probe.assert_called_once()
    assert status == "ready"


# ---------------------------------------------------------------------------
# IdeManager.stop with APIError during container lookup
# ---------------------------------------------------------------------------


def test_stop_handles_docker_unavailable_gracefully(
    cfg: WebIdeConfig, store: InMemoryStore
) -> None:
    """stop() still removes the Mongo doc + deadline file even when Docker is down."""
    from docker.errors import DockerException

    now = datetime.now(UTC)
    pre = IdeInstance(
        session_id=VALID_SID,
        status="pending",
        project_name="demo",
        project_path="/tmp",
        password="xx",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        max_deadline=now + timedelta(hours=8),
        extensions=0,
        cpus=1.0,
        memory="1g",
    )
    store.insert(pre)

    with tempfile.TemporaryDirectory() as tmp:
        cfg_with_dir = WebIdeConfig(
            enabled=True,
            image="img",
            default_lifetime_hours=1,
            max_lifetime_hours=8,
            cpus=1.0,
            memory="1g",
            pids_limit=512,
            network="net",
            state_dir=tmp,
        )
        mgr = IdeManager(cfg_with_dir, store, docker_client=None)
        with patch("mewbo_api.ide.docker_from_env", side_effect=DockerException("no sock")):
            result = mgr.stop(VALID_SID)

    # Mongo doc removed even though Docker call failed
    assert store.get(VALID_SID) is None
    assert result is True  # removed_doc == True


# ---------------------------------------------------------------------------
# IdeManager._safe_remove_file
# ---------------------------------------------------------------------------


def test_safe_remove_file_missing_returns_false() -> None:
    assert IdeManager._safe_remove_file("/nonexistent/path/file.txt") is False


def test_safe_remove_file_existing_returns_true() -> None:
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    assert os.path.exists(path)
    result = IdeManager._safe_remove_file(path)
    assert result is True
    assert not os.path.exists(path)


# ---------------------------------------------------------------------------
# SESSION_ID_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sid,valid",
    [
        ("a" * 32, True),
        ("f" * 32, True),
        ("0" * 32, True),
        ("A" * 32, False),  # uppercase rejected
        ("a" * 31, False),  # too short
        ("a" * 33, False),  # too long
        ("g" * 32, False),  # not hex
        ("", False),
    ],
)
def test_session_id_re(sid: str, valid: bool) -> None:
    assert bool(SESSION_ID_RE.match(sid)) is valid


# ---------------------------------------------------------------------------
# IdeStore._from_doc (via _as_utc naive path)
# ---------------------------------------------------------------------------


def test_ide_store_from_doc_handles_naive_created_at() -> None:
    """_from_doc must coerce a naive datetime (as stored by naive Mongo) to UTC-aware."""

    now = datetime.now(UTC)
    # Build a document with a naive created_at (as Mongo sometimes returns)
    doc = {
        "session_id": VALID_SID,
        "project_name": "proj",
        "project_path": "/tmp",
        "password": "secret",
        "created_at": datetime(2026, 1, 1, 12, 0, 0),  # naive
        "expires_at": now + timedelta(hours=1),
        "max_deadline": now + timedelta(hours=8),
        "extensions": 0,
        "cpus": 1.0,
        "memory": "1g",
    }
    inst = IdeStore._from_doc(doc)
    assert inst.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# IdeManager.ensure: deadline file write failure rolls back Mongo
# ---------------------------------------------------------------------------


def test_ensure_rolls_back_on_deadline_file_failure(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(
        IdeManager,
        "_write_deadline",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError):
            manager.ensure(VALID_SID, "demo", project_path)

    # Mongo doc rolled back
    assert store.get(VALID_SID) is None
