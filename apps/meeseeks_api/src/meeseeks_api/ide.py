"""Per-session "Open in Web IDE" orchestration.

Single module containing three cleanly separated sections:

- ``IdeInstance``: immutable dataclass describing one running container.
- ``IdeStore``: thin MongoDB wrapper (the single source of truth for state).
- ``IdeManager``: Docker orchestration (spawn, extend, stop, health probe).

Mongo is the single source of truth. Container labels are written for debug /
disaster recovery only and are never read back to hydrate state. Drift between
Mongo and Docker is reconciled lazily in ``get()``.

Containers self-terminate: a watchdog started inside the entrypoint polls the
bind-mounted ``/meeseeks/deadline`` file and sends ``kill 1`` once the wall
clock passes the stored epoch. Extending a session just overwrites that file.
"""

from __future__ import annotations

import os
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from docker.errors import APIError, DockerException, NotFound
from loguru import logger
from meeseeks_core.config import WebIdeConfig
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from docker import from_env as docker_from_env  # type: ignore[attr-defined]

UTC = timezone.utc
SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
IdeStatus = Literal["pending", "starting", "ready"]

# Container watchdog: the only ``sh -c`` string in the feature.
# ``{sid}`` is interpolated from a value that MUST be regex-validated to
# ``^[a-f0-9]{32}$`` before reaching this code path.
WATCHDOG_CMD = (
    "(while [ $(date +%s) -lt $(cat /meeseeks/deadline) ]; do sleep 15; done; "
    "kill 1) & "
    "exec /usr/bin/entrypoint.sh --auth password --bind-addr 0.0.0.0:8080 "
    "--disable-telemetry --disable-update-check "
    "--abs-proxy-base-path /ide/{sid} /home/coder/project"
)

PROBE_URL = "http://127.0.0.1:5126/ide/{sid}/healthz"


class MaxLifetimeReached(Exception):
    """Raised when an extension would exceed ``max_deadline``."""

    def __init__(self, max_deadline: datetime) -> None:
        """Store the hard cap that blocked the extension."""
        super().__init__(f"max lifetime reached: {max_deadline.isoformat()}")
        self.max_deadline = max_deadline


class DockerUnavailable(Exception):
    """Raised when the docker daemon is unreachable."""


# ---------------------------------------------------------------------------
# IdeInstance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdeInstance:
    """State of a single running code-server container for one session."""

    session_id: str
    status: IdeStatus
    project_name: str
    project_path: str
    password: str
    created_at: datetime
    expires_at: datetime
    max_deadline: datetime
    extensions: int
    cpus: float
    memory: str

    @property
    def url(self) -> str:
        """Return the browser-facing URL behind ``ide-proxy``."""
        return f"/ide/{self.session_id}/"

    @property
    def container_name(self) -> str:
        """Return the deterministic docker container name."""
        return f"meeseeks-ide-{self.session_id}"

    @property
    def remaining_seconds(self) -> int:
        """Return seconds until ``expires_at``, floored at zero."""
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))

    def to_dict(self, *, include_password: bool = False) -> dict[str, object]:
        """Serialize to the JSON shape returned by the API routes.

        ``password`` is included only on ``POST`` responses so the secret
        isn't re-sent on every 30s status poll from the session page.
        """
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "status": self.status,
            "url": self.url,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "max_deadline": self.max_deadline.isoformat(),
            "remaining_seconds": self.remaining_seconds,
            "extensions": self.extensions,
            "cpus": self.cpus,
            "memory": self.memory,
        }
        if include_password:
            payload["password"] = self.password
        return payload


# ---------------------------------------------------------------------------
# IdeStore
# ---------------------------------------------------------------------------


class IdeStore:
    """Thin MongoDB wrapper for the ``ide_instances`` collection."""

    def __init__(self, db: Database) -> None:
        """Bind to a database and ensure the unique index exists."""
        self._col: Collection = db["ide_instances"]
        self._col.create_index("session_id", unique=True, name="ux_ide_session_id")

    def get(self, session_id: str) -> IdeInstance | None:
        """Return the stored instance or ``None`` if absent."""
        doc = self._col.find_one({"session_id": session_id})
        if doc is None:
            return None
        return self._from_doc(doc)

    def insert(self, instance: IdeInstance) -> None:
        """Insert a new instance. Raises ``DuplicateKeyError`` on race."""
        # Build the document explicitly so the persisted schema is obvious
        # and cannot accidentally pick up new fields that belong in-memory only.
        self._col.insert_one(
            {
                "session_id": instance.session_id,
                "project_name": instance.project_name,
                "project_path": instance.project_path,
                "password": instance.password,
                "created_at": instance.created_at,
                "expires_at": instance.expires_at,
                "max_deadline": instance.max_deadline,
                "extensions": instance.extensions,
                "cpus": instance.cpus,
                "memory": instance.memory,
            }
        )

    def update_expiry(self, session_id: str, *, expires_at: datetime, extensions: int) -> None:
        """Atomically bump ``expires_at`` and the extension counter."""
        self._col.update_one(
            {"session_id": session_id},
            {"$set": {"expires_at": expires_at, "extensions": extensions}},
        )

    def delete(self, session_id: str) -> bool:
        """Delete the doc and return whether one was actually removed."""
        return self._col.delete_one({"session_id": session_id}).deleted_count > 0

    @staticmethod
    def _from_doc(doc: dict[str, object]) -> IdeInstance:
        """Hydrate an ``IdeInstance`` from a Mongo document (status defaults to pending)."""
        return IdeInstance(
            session_id=str(doc["session_id"]),
            status="pending",
            project_name=str(doc["project_name"]),
            project_path=str(doc["project_path"]),
            password=str(doc["password"]),
            created_at=_as_utc(doc["created_at"]),
            expires_at=_as_utc(doc["expires_at"]),
            max_deadline=_as_utc(doc["max_deadline"]),
            extensions=int(doc.get("extensions") or 0),  # type: ignore[call-overload]
            cpus=float(doc["cpus"]),  # type: ignore[arg-type]
            memory=str(doc["memory"]),
        )


def _as_utc(value: object) -> datetime:
    """Coerce a Mongo datetime (possibly naive) to a UTC-aware datetime."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# IdeManager
# ---------------------------------------------------------------------------


class IdeManager:
    """Docker orchestration + lifecycle for per-session IDE containers."""

    #: Probe results are cached this many seconds to cut blocking HTTP calls
    #: during tight polling loops (frontend loader polls at 1 Hz).
    _PROBE_TTL_SECONDS: float = 2.0

    def __init__(
        self,
        config: WebIdeConfig,
        store: IdeStore,
        *,
        docker_client: Any = None,
    ) -> None:
        """Initialize with feature config, Mongo-backed store, and optional docker client."""
        self._cfg = config
        self._store = store
        self._client = docker_client
        self._probe_cache: dict[str, tuple[bool, float]] = {}
        os.makedirs(self._cfg.state_dir, exist_ok=True)

    # -- public API --------------------------------------------------------

    def ensure(
        self, session_id: str, project_name: str, project_path: str
    ) -> tuple[IdeInstance, bool]:
        """Create or reconnect to the container for a session.

        Returns ``(instance, created)`` where ``created`` is True if a brand
        new container was spawned (HTTP 201) and False on reconnect (HTTP 200).
        """
        self._validate_session_id(session_id)
        existing = self._store.get(session_id)
        if existing is not None:
            if self._container_running(existing.container_name):
                return replace(existing, status=self._probe_status(session_id)), False
            logger.warning(
                "ide: stale state for session {}; container gone or exited, recreating",
                session_id,
            )
            # Force-remove any lingering exited container first — otherwise the
            # subsequent ``containers.run(name=...)`` would 409 on name conflict.
            self._safe_remove_container(existing.container_name)
            self._forget(session_id)

        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self._cfg.default_lifetime_hours)
        max_deadline = now + timedelta(hours=self._cfg.max_lifetime_hours)
        instance = IdeInstance(
            session_id=session_id,
            status="pending",
            project_name=project_name,
            project_path=project_path,
            password=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=expires_at,
            max_deadline=max_deadline,
            extensions=0,
            cpus=self._cfg.cpus,
            memory=self._cfg.memory,
        )

        # Write ordering: mongo -> deadline file -> container. Rollback in reverse.
        try:
            self._store.insert(instance)
        except DuplicateKeyError:
            # Lost the race with a concurrent spawn; read and return winner.
            winner = self._store.get(session_id)
            if winner is None:
                raise
            if self._container_running(winner.container_name):
                winner = replace(winner, status=self._probe_status(session_id))
            return winner, False

        deadline_path = self._deadline_file(session_id)
        try:
            self._write_deadline(deadline_path, expires_at)
        except OSError:
            self._store.delete(session_id)
            raise

        try:
            self._run_container(instance)
        except Exception:
            self._safe_remove_container(instance.container_name)
            self._safe_remove_file(deadline_path)
            self._store.delete(session_id)
            raise

        logger.info(
            "ide: spawned container {} for session {}",
            instance.container_name,
            session_id,
        )
        return replace(instance, status=self._probe_status(session_id)), True

    def get(self, session_id: str) -> IdeInstance | None:
        """Return current instance state, reconciling drift lazily.

        Raises ``DockerUnavailable`` if the daemon is unreachable so callers
        can return 503 without destroying persistent state on a transient
        daemon blip.
        """
        self._validate_session_id(session_id)
        instance = self._store.get(session_id)
        if instance is None:
            return None
        if not self._container_running(instance.container_name):
            logger.warning(
                "ide: container {} not running; cleaning up",
                instance.container_name,
            )
            # Force-remove the exited container so a subsequent POST can
            # respawn under the same name without a 409 conflict.
            self._safe_remove_container(instance.container_name)
            self._forget(session_id)
            return None
        return replace(instance, status=self._probe_status(session_id))

    def extend(
        self,
        session_id: str,
        *,
        hours: int | None = None,
        expires_at: datetime | None = None,
    ) -> IdeInstance:
        """Push the deadline forward. Exactly one of ``hours``/``expires_at`` required."""
        self._validate_session_id(session_id)
        if (hours is None) == (expires_at is None):
            raise ValueError("exactly one of hours / expires_at must be provided")
        instance = self._store.get(session_id)
        if instance is None:
            raise LookupError(f"no ide instance for session {session_id}")

        now = datetime.now(UTC)
        if hours is not None:
            new_expires_at = now + timedelta(hours=hours)
        else:
            assert expires_at is not None
            new_expires_at = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            new_expires_at = new_expires_at.astimezone(UTC)

        if new_expires_at <= now:
            raise ValueError("expires_at must be in the future")
        if new_expires_at > instance.max_deadline:
            raise MaxLifetimeReached(instance.max_deadline)

        new_extensions = instance.extensions + 1
        # Deadline file first, then Mongo. The file is what the container's
        # watchdog actually reads; if we write Mongo first and the file write
        # fails, the API would report an extended deadline while the container
        # self-terminates at the old one. File-then-Mongo reverses that risk:
        # if Mongo fails after a successful file write, the container simply
        # lives longer than Mongo records — strictly safer, and ``get()``
        # reconciles it on the next read.
        self._write_deadline(self._deadline_file(session_id), new_expires_at)
        self._store.update_expiry(session_id, expires_at=new_expires_at, extensions=new_extensions)

        updated = replace(
            instance,
            expires_at=new_expires_at,
            extensions=new_extensions,
            status=(
                self._probe_status(session_id)
                if self._container_running(instance.container_name)
                else instance.status
            ),
        )
        return updated

    def stop(self, session_id: str) -> bool:
        """Remove the container, deadline file, and mongo doc. Returns whether anything existed."""
        self._validate_session_id(session_id)
        container_name = f"meeseeks-ide-{session_id}"
        removed_container = self._safe_remove_container(container_name)
        removed_file = self._safe_remove_file(self._deadline_file(session_id))
        removed_doc = self._store.delete(session_id)
        self._probe_cache.pop(session_id, None)
        return removed_container or removed_file or removed_doc

    def _forget(self, session_id: str) -> None:
        """Drop all local state for a session: Mongo doc, deadline file, probe cache.

        Used by the lazy drift-reconciliation paths in ``ensure()`` and ``get()``.
        """
        self._store.delete(session_id)
        self._safe_remove_file(self._deadline_file(session_id))
        self._probe_cache.pop(session_id, None)

    # -- docker internals --------------------------------------------------

    def _docker(self) -> Any:
        """Lazy-connect to the docker daemon; raise ``DockerUnavailable`` on failure."""
        if self._client is not None:
            return self._client
        try:
            self._client = docker_from_env()
        except DockerException as exc:
            raise DockerUnavailable(str(exc)) from exc
        return self._client

    def _container_running(self, name: str) -> bool:
        """Return True iff a container with ``name`` exists AND is running.

        A container that has self-terminated via the watchdog is in the
        ``exited`` state but still resolvable by ``containers.get`` — we
        treat those as dead so the reconnect path in ``ensure()`` can
        force-remove and respawn instead of handing back stale state.

        Lets ``DockerUnavailable`` propagate so callers can 503 instead of
        silently destroying persistent Mongo state on a transient daemon blip.
        """
        client = self._docker()
        try:
            container = client.containers.get(name)
        except NotFound:
            return False
        except APIError as exc:
            logger.warning("ide: docker API error while checking {}: {}", name, exc)
            return False
        return container.status == "running"

    def _run_container(self, instance: IdeInstance) -> None:
        """Invoke ``containers.run`` with all structured kwargs."""
        client = self._docker()
        labels = {
            "meeseeks.kind": "web-ide",
            "meeseeks.session_id": instance.session_id,
            "meeseeks.project_name": instance.project_name,
            "meeseeks.created_at": instance.created_at.isoformat(),
            "meeseeks.max_deadline": instance.max_deadline.isoformat(),
        }
        volumes = {
            instance.project_path: {"bind": "/home/coder/project", "mode": "rw"},
            self._deadline_file(instance.session_id): {
                "bind": "/meeseeks/deadline",
                "mode": "ro",
            },
        }
        entrypoint = ["sh", "-c", WATCHDOG_CMD.format(sid=instance.session_id)]
        client.containers.run(
            image=self._cfg.image,
            name=instance.container_name,
            entrypoint=entrypoint,
            environment={"PASSWORD": instance.password},
            volumes=volumes,
            labels=labels,
            network=self._cfg.network,
            mem_limit=self._cfg.memory,
            nano_cpus=int(self._cfg.cpus * 1e9),
            pids_limit=self._cfg.pids_limit,
            detach=True,
            auto_remove=False,
        )

    def _safe_remove_container(self, name: str) -> bool:
        """Force-remove a container, swallowing every error.

        Used for best-effort cleanup (rollback after partial spawn, explicit
        ``stop()``). A docker outage here must not prevent us from also
        cleaning up Mongo and the deadline file.
        """
        try:
            client = self._docker()
        except DockerUnavailable:
            return False
        try:
            container = client.containers.get(name)
        except NotFound:
            return False
        except APIError as exc:
            logger.warning("ide: docker API error locating {}: {}", name, exc)
            return False
        try:
            container.remove(force=True)
            return True
        except NotFound:
            return False
        except APIError as exc:
            logger.warning("ide: failed to remove container {}: {}", name, exc)
            return False

    # -- filesystem helpers ------------------------------------------------

    def _deadline_file(self, session_id: str) -> str:
        """Return the absolute path of the deadline bind-mount file."""
        return os.path.join(self._cfg.state_dir, f"{session_id}.deadline")

    @staticmethod
    def _write_deadline(path: str, expires_at: datetime) -> None:
        """Write the epoch seconds for ``expires_at`` to ``path``."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(int(expires_at.timestamp())))

    @staticmethod
    def _safe_remove_file(path: str) -> bool:
        """Unlink a file, swallowing ``FileNotFoundError``. Returns whether it existed."""
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("ide: failed to remove deadline file {}: {}", path, exc)
            return False

    # -- health probe ------------------------------------------------------

    def _probe_status(self, session_id: str) -> IdeStatus:
        """Return ``ready`` if the healthz probe passes, else ``starting``.

        Results are cached for ``_PROBE_TTL_SECONDS`` to avoid issuing a
        blocking HTTP request on every poll from the frontend loader.
        """
        now = time.monotonic()
        cached = self._probe_cache.get(session_id)
        if cached is not None and now - cached[1] < self._PROBE_TTL_SECONDS:
            ready = cached[0]
        else:
            ready = self._probe_ready(session_id)
            self._probe_cache[session_id] = (ready, now)
        return "ready" if ready else "starting"

    @staticmethod
    def _probe_ready(session_id: str) -> bool:
        """HTTP GET the ide-proxy healthz path with a 1s timeout."""
        url = PROBE_URL.format(sid=session_id)
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return False

    # -- validation --------------------------------------------------------

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """Raise ``ValueError`` unless ``session_id`` matches the 32-hex regex."""
        if not SESSION_ID_RE.match(session_id):
            raise ValueError(f"invalid session_id: {session_id!r}")


__all__ = [
    "DockerUnavailable",
    "IdeInstance",
    "IdeManager",
    "IdeStore",
    "MaxLifetimeReached",
    "SESSION_ID_RE",
]
