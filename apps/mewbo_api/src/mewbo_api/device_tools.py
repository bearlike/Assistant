"""Client-declared device tools — api-side dispatch glue (Gitea #179, Phase 1).

The generic bridge lives split across two layers: ``mewbo_core.client_tools``
owns the ``SessionTool`` (``ClientDeclaredTool``) and the down-only DI seam
(``DeviceToolDispatcher``) it dispatches through; this module owns the
CONCRETE dispatcher — the transport/persistence half that can't live in core
(it appends session events and blocks on a client's HTTP-delivered result).

``DevicePendingCalls`` is the in-process registry of in-flight calls awaiting
a client-POSTed result, keyed by ``(session_id, call_id)``. ``dispatch()``
appends a ``device_tool_call`` event (delivered to the client over the
session's existing SSE stream) then blocks the calling coroutine on a
``threading.Event`` — NEVER an ``asyncio.Future`` — because the session loop
(its own thread + event loop) and the Flask route handling the client's
result (a WSGI worker thread) are different threads; a ``Future`` created on
one loop cannot be resolved from another.

**Honest threat model for ``call_token``.** It proves the responder had
SESSION-STREAM READ ACCESS (received the ``device_tool_call`` SSE event) and
prevents replay (single-use, consumed-once) — it does NOT prove the response
came from the physical device the call was dispatched to. Any concurrent
viewer of the same session's SSE stream (e.g. a read-only console tab)
receives the identical token and could answer on the device's behalf; that
is the accepted Phase 1 threat model. Verified per-device identity is a
future phase.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from mewbo_core.common import get_logger
from mewbo_core.session_event_bus import get_session_event_bus
from mewbo_core.session_runtime import SessionRuntime

logging = get_logger(name="api.device_tools")

DEVICE_TOOL_TIMEOUT_S = 30.0
"""Default wait budget for a client to fulfil a device-tool call.

A module-level constant (not a default arg) so tests can monkeypatch it —
``dispatch`` re-reads the module global on every call.
"""

_POLL_INTERVAL_S = 0.2

ResolveOutcome = Literal["ok", "not_found", "bad_token", "conflict"]

DEVICE_TOOL_CALL_EVENT = "device_tool_call"

_CONSUMED_UNREAD_GRACE_S = 60.0
"""Extra time a CONSUMED-but-never-read entry survives past its own deadline.

Protects a legitimate race: a client can POST a result in the narrow window
right at ``expires_at`` (accepted, ``consumed=True``) before the dispatcher's
poll loop (≤0.2s granularity) has had a chance to read it. Without this grace
an unrelated concurrent ``create``/``resolve`` call's opportunistic reap could
delete the entry in that window, and the dispatcher would see a vanished
result and report a spurious internal error — the model might then retry the
tool call, duplicating a real-world side effect (e.g. a second SMS send).
"""


@dataclass
class _PendingCall:
    """One in-flight device-tool call awaiting delivery."""

    call_token: str
    event: threading.Event
    expires_at: float
    result: dict[str, Any] | None = field(default=None)
    consumed: bool = field(default=False)
    read: bool = field(default=False)


class DevicePendingCalls:
    """Registry of in-flight device-tool calls awaiting a client-POSTed result.

    One instance per process (see :func:`get_pending_calls`), guarded by a
    single lock. Consumed-once: the first :meth:`resolve` for a call wins; a
    duplicate/late POST for the same call returns ``"conflict"`` rather than
    silently overwriting the delivered result.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingCall] = {}

    def create(
        self, session_id: str, call_id: str, call_token: str, expires_at: float
    ) -> threading.Event:
        """Register a new pending call; return the event the caller waits on."""
        event = threading.Event()
        with self._lock:
            self._reap_expired_locked()
            self._pending[(session_id, call_id)] = _PendingCall(
                call_token=call_token, event=event, expires_at=expires_at
            )
        return event

    def resolve(
        self, session_id: str, call_id: str, token: str, payload: dict[str, Any]
    ) -> ResolveOutcome:
        """Deliver *payload* for a pending call.

        Token compared via :func:`hmac.compare_digest` (constant-time — the
        token is a bearer secret, not a lookup key). Atomic under the lock:
        an unknown/expired call → ``"not_found"``, a token mismatch →
        ``"bad_token"``, an already-consumed call → ``"conflict"``, else the
        result is stored, the waiter's event is set, and ``"ok"`` returns.

        The opportunistic reap sweep EXCLUDES this call's own key: a
        consumed-and-read entry is reap-eligible (see
        :meth:`_reap_expired_locked`), but that must never let THIS call's
        own sweep delete the entry out from under the ``"conflict"`` check
        two lines below — a duplicate POST arriving right after the
        dispatcher reads the result must still see it and answer
        ``"conflict"``, not ``"not_found"``. An entry that is unconsumed and
        genuinely past its deadline is still rejected here explicitly
        (``"not_found"``) even though the sweep didn't touch it.
        """
        with self._lock:
            key = (session_id, call_id)
            self._reap_expired_locked(exclude=key)
            entry = self._pending.get(key)
            if entry is None:
                return "not_found"
            if not entry.consumed and entry.expires_at < time.time():
                return "not_found"
            if not hmac.compare_digest(entry.call_token, token):
                return "bad_token"
            if entry.consumed:
                return "conflict"
            entry.consumed = True
            entry.result = payload
            entry.event.set()
            return "ok"

    def result_for(self, session_id: str, call_id: str) -> dict[str, Any] | None:
        """Return the delivered result for a resolved call, else ``None``.

        Marks the entry ``read`` as a side effect of a successful read — this
        is what makes it eligible for opportunistic reaping afterward (see
        :meth:`_reap_expired_locked`). Non-destructive otherwise: repeated
        reads keep returning the same result until some later call reaps it.
        """
        with self._lock:
            entry = self._pending.get((session_id, call_id))
            if entry is None or not entry.consumed:
                return None
            entry.read = True
            return entry.result

    def _reap_expired_locked(self, *, exclude: tuple[str, str] | None = None) -> None:
        """Drop entries nobody can still need. Caller MUST hold ``_lock``.

        Three independent conditions (a consumed-but-UNREAD entry matches
        NONE of them, however long past ``expires_at`` it is, short of the
        grace window — see :data:`_CONSUMED_UNREAD_GRACE_S`):

        - unconsumed AND past its deadline — genuinely abandoned, nobody will
          ever deliver a result for it.
        - consumed AND read — the dispatcher has already retrieved the
          result; nothing further needs the entry.
        - consumed AND unread but past deadline + the grace window — a
          dispatcher that crashed mid-read must not leak the entry forever.

        *exclude* skips one key from this sweep (see :meth:`resolve`'s
        docstring for why).
        """
        now = time.time()
        expired = [
            key
            for key, entry in self._pending.items()
            if key != exclude
            and (
                (not entry.consumed and entry.expires_at < now)
                or (entry.consumed and entry.read)
                or (entry.consumed and entry.expires_at < now - _CONSUMED_UNREAD_GRACE_S)
            )
        ]
        for key in expired:
            del self._pending[key]


class ApiDeviceToolDispatcher:
    """Concrete ``DeviceToolDispatcherImpl`` — session-event delivery + bounded wait.

    Registered into the core seam (``mewbo_core.client_tools.DeviceToolDispatcher``)
    at api startup, mirroring ``RunStoreSearchLauncher``. DI'd with the
    session runtime (to append the ``device_tool_call`` event through the
    SAME choke point the ``todos`` event uses) and the pending-call registry
    (defaults to the process-wide singleton).
    """

    def __init__(
        self, *, runtime: SessionRuntime, pending: DevicePendingCalls | None = None
    ) -> None:
        """Bind the session runtime and pending-call registry."""
        self._runtime = runtime
        self._pending = pending if pending is not None else get_pending_calls()

    async def dispatch(
        self, session_id: str, tool_id: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Deliver a device-tool call to the client and await its result.

        Short-circuits to a ``device_unavailable`` error IMMEDIATELY — no
        event append, no wait — when nobody is subscribed to the session's
        SSE stream (``SessionEventBus.has_subscribers``): the common case of
        a re-engage/recover with no client attached would otherwise burn the
        full ``DEVICE_TOOL_TIMEOUT_S`` for a call that was never
        deliverable. See ``has_subscribers``'s docstring for the known
        limitation (a subscriber is not necessarily an executor).

        Otherwise appends one ``device_tool_call`` event carrying a fresh
        single-use ``call_token``, then polls a ``threading.Event`` (the
        repo's verified cross-thread idiom — see module docstring) until the
        client resolves it or ``DEVICE_TOOL_TIMEOUT_S`` elapses.
        """
        if not get_session_event_bus().has_subscribers(session_id):
            return {
                "status": "error",
                "error": {
                    "code": "device_unavailable",
                    "message": (
                        f"No client is attached to session {session_id}'s "
                        f"event stream; device tool '{tool_id}' cannot be "
                        "delivered."
                    ),
                },
            }

        call_id = uuid.uuid4().hex
        call_token = secrets.token_urlsafe(24)
        expires_at = time.time() + DEVICE_TOOL_TIMEOUT_S
        event = self._pending.create(session_id, call_id, call_token, expires_at)

        self._runtime.append_event(
            session_id,
            {
                "type": DEVICE_TOOL_CALL_EVENT,
                "payload": {
                    "call_id": call_id,
                    "call_token": call_token,
                    "tool_id": tool_id,
                    "args": tool_input,
                    "expires_at": expires_at,
                },
            },
        )

        while not event.is_set() and time.time() < expires_at:
            await asyncio.sleep(_POLL_INTERVAL_S)

        if not event.is_set():
            # Leave the entry for opportunistic reaping (it is already past
            # its own `expires_at`) rather than removing it here — a result
            # that arrives in the exact race window between our deadline
            # check and now must still see a live entry to resolve against.
            return {
                "status": "error",
                "error": {
                    "code": "device_timeout",
                    "message": (
                        f"No result received for device tool '{tool_id}' "
                        f"within {DEVICE_TOOL_TIMEOUT_S}s."
                    ),
                },
            }

        # Non-destructive read: the entry lingers (consumed) until it ages
        # out, so a duplicate client POST still 409s instead of 404ing.
        result = self._pending.result_for(session_id, call_id)
        if result is None:  # pragma: no cover - defensive, should be unreachable
            return {
                "status": "error",
                "error": {
                    "code": "device_tool_internal",
                    "message": "Delivered result vanished before it could be read.",
                },
            }
        return result


# ---------------------------------------------------------------------------
# Process-wide singleton (DI seam shared by the dispatcher + the result route)
# ---------------------------------------------------------------------------

_PENDING_CALLS: DevicePendingCalls | None = None


def get_pending_calls() -> DevicePendingCalls:
    """Return the process-wide pending-calls registry, creating it on first use.

    Mirrors ``get_session_event_bus()`` / ``get_wiki_store()``: a
    singleton+factory+``reset_for_tests`` seam shared by the dispatcher
    (writer) and the ``POST .../device_tools/<call_id>/result`` route
    (resolver).
    """
    global _PENDING_CALLS
    if _PENDING_CALLS is None:
        _PENDING_CALLS = DevicePendingCalls()
    return _PENDING_CALLS


def reset_pending_calls_for_tests() -> DevicePendingCalls:
    """Swap in a fresh registry for test isolation and return it."""
    global _PENDING_CALLS
    _PENDING_CALLS = DevicePendingCalls()
    return _PENDING_CALLS


__all__ = [
    "DEVICE_TOOL_CALL_EVENT",
    "DEVICE_TOOL_TIMEOUT_S",
    "ApiDeviceToolDispatcher",
    "DevicePendingCalls",
    "ResolveOutcome",
    "get_pending_calls",
    "reset_pending_calls_for_tests",
]
