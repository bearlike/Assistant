"""Unit tests for ``meeseeks_api.ide`` (IdeManager + IdeStore)."""

# mypy: ignore-errors
# ruff: noqa: D101, D102, D103, D107
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from meeseeks_api.ide import (
    DockerUnavailable,
    IdeInstance,
    IdeManager,
    MaxLifetimeReached,
)
from meeseeks_core.config import WebIdeConfig

from docker.errors import APIError, NotFound

UTC = timezone.utc
VALID_SID = "a" * 32
OTHER_SID = "b" * 32


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


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


class InMemoryStore:
    """Mimics ``IdeStore`` using a dict."""

    def __init__(self) -> None:
        self._docs: dict[str, IdeInstance] = {}

    def get(self, session_id: str) -> IdeInstance | None:
        src = self._docs.get(session_id)
        if src is None:
            return None
        # Return a copy with status reset to 'pending', mirroring IdeStore._from_doc.
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

            raise DuplicateKeyError("duplicate session_id")
        # IdeInstance is frozen so the reference is immutable anyway.
        self._docs[instance.session_id] = instance

    def update_expiry(self, session_id: str, *, expires_at: datetime, extensions: int) -> None:
        from dataclasses import replace

        self._docs[session_id] = replace(
            self._docs[session_id],
            expires_at=expires_at,
            extensions=extensions,
        )

    def delete(self, session_id: str) -> bool:
        return self._docs.pop(session_id, None) is not None


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
        network="meeseeks-ide",
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
# ensure()
# ---------------------------------------------------------------------------


def test_ensure_creates_container_and_persists(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        instance, created = manager.ensure(VALID_SID, "demo", project_path)

    assert created is True
    assert instance.session_id == VALID_SID
    assert instance.container_name == f"meeseeks-ide-{VALID_SID}"
    assert instance.status == "starting"  # probe not ready
    assert instance.password  # auto-generated

    # mongo doc persisted
    assert store.get(VALID_SID) is not None

    # deadline file written with epoch seconds
    deadline_file = os.path.join(manager._cfg.state_dir, f"{VALID_SID}.deadline")
    assert os.path.isfile(deadline_file)
    with open(deadline_file) as fh:
        epoch = int(fh.read())
    assert epoch == int(instance.expires_at.timestamp())

    # docker run was called once with structured kwargs
    assert len(fake_client.containers.run_calls) == 1
    call = fake_client.containers.run_calls[0]
    assert call["name"] == f"meeseeks-ide-{VALID_SID}"
    assert call["image"] == "codercom/code-server:latest"
    assert call["entrypoint"][0] == "sh"
    assert call["entrypoint"][1] == "-c"
    assert VALID_SID in call["entrypoint"][2]
    assert call["environment"]["PASSWORD"] == instance.password
    assert call["network"] == "meeseeks-ide"
    assert call["detach"] is True
    assert call["auto_remove"] is False
    assert call["nano_cpus"] == int(1.0 * 1e9)
    # volumes as structured dict
    vols = call["volumes"]
    assert vols[project_path] == {"bind": "/home/coder/project", "mode": "rw"}
    assert vols[deadline_file] == {"bind": "/meeseeks/deadline", "mode": "ro"}


def test_ensure_reconnect_when_container_alive(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        first, created1 = manager.ensure(VALID_SID, "demo", project_path)
        second, created2 = manager.ensure(VALID_SID, "demo", project_path)

    assert created1 is True
    assert created2 is False
    assert first.session_id == second.session_id
    assert first.password == second.password
    assert second.status == "ready"


def test_ensure_recreates_when_container_vanished(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        first, _ = manager.ensure(VALID_SID, "demo", project_path)
        # Simulate the container being removed out-of-band.
        fake_client.containers._by_name.clear()
        second, created = manager.ensure(VALID_SID, "demo", project_path)

    assert created is True
    assert second.password != first.password  # new password generated
    # Second run call happened
    assert len(fake_client.containers.run_calls) == 2


def test_ensure_recreates_when_container_exited(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    """Reconnect path must treat a self-terminated (exited) container as dead.

    Regression: the watchdog's ``kill 1`` leaves the container in ``exited``
    state. ``containers.get`` still resolves it, so a literal "exists?" check
    would return True and hand the caller a stale doc whose expires_at is in
    the past. ``ensure()`` must force-remove the exited container and respawn.
    """
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        first, _ = manager.ensure(VALID_SID, "demo", project_path)
        # Watchdog self-terminated the container: it's still in the daemon
        # but its status is now "exited".
        fake_client.containers._by_name[first.container_name].status = "exited"
        second, created = manager.ensure(VALID_SID, "demo", project_path)

    assert created is True
    assert second.password != first.password  # fresh spawn
    assert len(fake_client.containers.run_calls) == 2
    # The stale exited container must have been removed before the new
    # containers.run — otherwise docker would 409 on the name collision.
    assert not fake_client.containers._by_name[second.container_name].removed


def test_get_removes_exited_container_and_returns_none(
    manager: IdeManager,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        first, _ = manager.ensure(VALID_SID, "demo", project_path)
        fake_client.containers._by_name[first.container_name].status = "exited"

        result = manager.get(VALID_SID)

    assert result is None
    # Exited container was force-removed by get() so a subsequent POST can
    # respawn cleanly.
    assert first.container_name not in fake_client.containers._by_name


def test_ensure_rolls_back_mongo_on_run_failure(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    fake_client.containers.run_should_raise = APIError("no image")
    with pytest.raises(APIError):
        manager.ensure(VALID_SID, "demo", project_path)

    assert store.get(VALID_SID) is None
    deadline_file = os.path.join(manager._cfg.state_dir, f"{VALID_SID}.deadline")
    assert not os.path.exists(deadline_file)


def test_ensure_rejects_invalid_session_id(manager: IdeManager, project_path: str) -> None:
    with pytest.raises(ValueError):
        manager.ensure("nope", "demo", project_path)
    with pytest.raises(ValueError):
        manager.ensure("A" * 32, "demo", project_path)  # uppercase rejected


def test_ensure_handles_duplicate_key_race(
    cfg: WebIdeConfig, fake_client: FakeDockerClient, project_path: str
) -> None:
    store = InMemoryStore()
    # Pre-seed with existing instance.
    now = datetime.now(UTC)
    pre = IdeInstance(
        session_id=VALID_SID,
        status="pending",
        project_name="demo",
        project_path=project_path,
        password="xx",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        max_deadline=now + timedelta(hours=8),
        extensions=0,
        cpus=1.0,
        memory="1g",
    )
    store._docs[VALID_SID] = pre
    # Register container so alive check returns True.
    fake_client.containers._by_name[pre.container_name] = FakeContainer(
        pre.container_name, fake_client.containers
    )
    manager = IdeManager(cfg, store, docker_client=fake_client)

    # Forcing ensure() to hit the insert path by wiping the get() shortcut.
    original_get = store.get
    call_count = {"n": 0}

    def get_flaky(sid: str) -> IdeInstance | None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # pretend empty for the initial lookup
        return original_get(sid)

    store.get = get_flaky  # type: ignore[assignment]
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        instance, created = manager.ensure(VALID_SID, "demo", project_path)
    assert created is False
    assert instance.password == "xx"


# ---------------------------------------------------------------------------
# extend()
# ---------------------------------------------------------------------------


def test_extend_relative_hours(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        original, _ = manager.ensure(VALID_SID, "demo", project_path)
        extended = manager.extend(VALID_SID, hours=2)

    assert extended.extensions == 1
    assert extended.expires_at > original.expires_at


def test_extend_absolute_expires_at(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        manager.ensure(VALID_SID, "demo", project_path)
        target = datetime.now(UTC) + timedelta(hours=3)
        extended = manager.extend(VALID_SID, expires_at=target)
    assert abs((extended.expires_at - target).total_seconds()) < 1
    assert extended.extensions == 1


def test_extend_rejects_past_max_deadline(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        manager.ensure(VALID_SID, "demo", project_path)
        with pytest.raises(MaxLifetimeReached):
            manager.extend(
                VALID_SID,
                expires_at=datetime.now(UTC) + timedelta(hours=100),
            )


def test_extend_rejects_neither_or_both(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)
    with pytest.raises(ValueError):
        manager.extend(VALID_SID)
    with pytest.raises(ValueError):
        manager.extend(VALID_SID, hours=1, expires_at=datetime.now(UTC) + timedelta(hours=2))


def test_extend_unknown_session_raises(manager: IdeManager) -> None:
    with pytest.raises(LookupError):
        manager.extend(VALID_SID, hours=1)


def test_extend_updates_deadline_file(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)
        extended = manager.extend(VALID_SID, hours=3)

    deadline_file = os.path.join(manager._cfg.state_dir, f"{VALID_SID}.deadline")
    with open(deadline_file) as fh:
        epoch = int(fh.read())
    assert epoch == int(extended.expires_at.timestamp())


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


def test_stop_removes_everything(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)

    deadline_file = os.path.join(manager._cfg.state_dir, f"{VALID_SID}.deadline")
    assert manager.stop(VALID_SID) is True
    assert store.get(VALID_SID) is None
    assert not os.path.exists(deadline_file)
    assert f"meeseeks-ide-{VALID_SID}" not in fake_client.containers._by_name


def test_stop_returns_false_when_nothing_existed(manager: IdeManager) -> None:
    assert manager.stop(VALID_SID) is False


def test_stop_rejects_invalid_session_id(manager: IdeManager) -> None:
    with pytest.raises(ValueError):
        manager.stop("bad")


# ---------------------------------------------------------------------------
# get() + drift reconciliation
# ---------------------------------------------------------------------------


def test_get_returns_none_when_absent(manager: IdeManager) -> None:
    assert manager.get(VALID_SID) is None


def test_get_reconciles_drift_when_container_vanished(
    manager: IdeManager,
    store: InMemoryStore,
    fake_client: FakeDockerClient,
    project_path: str,
) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=False):
        manager.ensure(VALID_SID, "demo", project_path)
    fake_client.containers._by_name.clear()  # container vanished

    result = manager.get(VALID_SID)
    assert result is None
    assert store.get(VALID_SID) is None


def test_get_returns_ready_when_probe_passes(manager: IdeManager, project_path: str) -> None:
    with patch.object(IdeManager, "_probe_ready", return_value=True):
        manager.ensure(VALID_SID, "demo", project_path)
        state = manager.get(VALID_SID)
    assert state is not None
    assert state.status == "ready"


# ---------------------------------------------------------------------------
# _probe_ready
# ---------------------------------------------------------------------------


def test_probe_ready_200() -> None:
    class FakeResp:
        status = 200

        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        assert IdeManager._probe_ready(VALID_SID) is True


def test_probe_ready_connection_refused() -> None:
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        assert IdeManager._probe_ready(VALID_SID) is False


def test_probe_ready_timeout() -> None:
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        assert IdeManager._probe_ready(VALID_SID) is False


def test_probe_ready_non_200() -> None:
    class FakeResp:
        status = 502

        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        assert IdeManager._probe_ready(VALID_SID) is False


# ---------------------------------------------------------------------------
# docker connectivity
# ---------------------------------------------------------------------------


def test_docker_unavailable_wrapped(cfg: WebIdeConfig) -> None:
    from docker.errors import DockerException

    class RaisingStore:
        def get(self, sid: str) -> None:
            return None

        def insert(self, inst: IdeInstance) -> None:
            pass

        def delete(self, sid: str) -> bool:
            return False

    mgr = IdeManager(cfg, RaisingStore(), docker_client=None)
    with patch("meeseeks_api.ide.docker_from_env", side_effect=DockerException("no sock")):
        with pytest.raises(DockerUnavailable):
            mgr._docker()
