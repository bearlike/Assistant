"""Client-declared device tools — api-side dispatch glue.

The generic bridge lives split across two layers: ``mewbo_core.tooling.client_tools``
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
is the accepted threat model.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from mewbo_core.common import get_logger
from mewbo_core.contracts.types import EventRecord
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_event_bus import get_session_event_bus
from mewbo_core.tooling.client_tools import ClientToolSpec
from pydantic import ValidationError

logging = get_logger(name="api.device_tools")

DEVICE_TOOL_TIMEOUT_S = 30.0
"""Default wait budget for a client to fulfil a device-tool call.

A module-level constant (not a default arg) so tests can monkeypatch it —
``dispatch`` re-reads the module global on every call.
"""

DEVICE_EXECUTOR_GRACE_S = 5.0
"""How long a DETACHED executor still counts as reachable.

The subscription dies with the SSE request that carried the flag, and that
request is short-lived by design: the stream generator picks a blocking timeout
of ``0.0`` whenever the session is not running, so it closes in milliseconds and
the client reconnects. Between turns — and for the first moments of a new one —
the bus therefore reads zero executors while the phone is sitting right there.
Refusing on that reading is a false negative of exactly the shape the ask-user
dispatcher declines to risk at all (``apps/mewbo_api/CLAUDE.md``).

**Five seconds is derived from the two clients' own reconnect ladders, not
picked.** Aura re-opens after ``INITIAL_BACKOFF_MS = 500`` on a healthy close
and doubles from there; the console waits 3 s to re-subscribe and 1 s before its
first retry. Five seconds covers a healthy reconnect on both, plus Aura's first
three failed-connect steps (0.5 + 1 + 2), and stops short of the deep backoff
(8 s, 15 s) where the client has bigger problems than one tool call.

The window is bounded on both sides, which is what keeps the fast-fail property
the error message depends on:

- A client that NEVER attached gets no window at all — the bus only remembers an
  executor it actually saw — so a headless re-engage is still refused at entry
  with nothing appended.
- A wrong "yes" now costs the window plus one poll tick (~5.2 s), not the 30 s
  call budget: presence is re-checked every tick, so the refusal lands as soon as
  the window closes.

It also SHRINKS the pre-existing window in which we report a call undeliverable
that the client later executes anyway: a reconnect resumes from an INCLUSIVE
``?after=`` cursor, and a call appended during the gap is by definition newer
than the cursor the client left with, so the ``device_tool_call`` event still
reaches it (the client's own ledger de-dupes the replay). Waiting out the
reconnect converts most of those into an answer instead of a false refusal.

A module-level constant for the same reason as the timeout above: ``dispatch``
re-reads it on every call, so a test can shorten it without sleeping.
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


class DeviceToolBinding:
    """Which client-declared device tools are in force for a run.

    ``device_tools`` is not a ``SessionSpec`` field: it rides the request-context
    merge, so the only durable record of it is a ``context`` event, and every
    later run has to read that record back. The question a re-drive asks is *"what
    is this session's device-tool declaration"* — NOT *"what does its newest
    context event happen to say"*. Answering the first with the second makes every
    writer of a context event a silent de-registration, and none of the three in
    the tree (``approve_plan``'s ``{"mode": "act"}``, ``reinject_recovery_context``'s
    gating keys, the fork route's provenance stamp) has any reason to know device
    tools exist.

    So the read is narrowed with ``payload_key=``, the same protection ``project``
    already carries — see ``latest_event_of_type``'s docstring for the measured
    hazard (15 of 204 sessions with a ``project`` had a NEWER context event
    without one). Silence about the key is silence, not a revocation; an EXPLICIT
    declaration — including an empty list — still decides.

    Cost: ``O(1)`` when the payload in hand carries the key (the ordinary
    ``/query``, which re-advertises), else one narrowed store read — ``O(1)`` on
    Mongo, ``O(one session)`` on the JSON driver. Never a transcript fold.

    The store read is injected as a FIELD, late-bound by the caller for the same
    reason ``SessionSpecStore``'s collaborators are: the suite swaps the runtime
    wholesale, and a bound method captured at import would keep answering from the
    store that existed then.
    """

    CONTEXT_KEY = "device_tools"

    def __init__(
        self,
        *,
        latest_event_of_type: Callable[[str, str, str], EventRecord | None],
    ) -> None:
        """Bind the narrowed ``(session_id, event_type, payload_key)`` store read."""
        self._latest_event_of_type = latest_event_of_type

    def specs_for(
        self, session_id: str, context_payload: Mapping[str, object]
    ) -> list[ClientToolSpec]:
        """Validate the declaration in force for *session_id* into specs.

        Raises ``ValueError`` (a request-path caller maps it to 400) on a
        malformed entry, or on a duplicate ``tool_id`` within one declaration — a
        client-declared tool that fails validation must never silently vanish from
        the run, and ``SessionToolRegistry.build_for`` resolves session tools by id
        (first match wins), so a silent duplicate would leave the second
        declaration invisible rather than rejected.
        """
        raw = self.declaration_for(session_id, context_payload)
        if not isinstance(raw, list) or not raw:
            return []
        specs: list[ClientToolSpec] = []
        seen_ids: set[str] = set()
        for entry in raw:
            try:
                spec = ClientToolSpec.model_validate(entry)
            except ValidationError as exc:
                raise ValueError(f"Invalid device tool declaration: {exc}") from exc
            if spec.tool_id in seen_ids:
                raise ValueError(f"Duplicate device tool_id declared: {spec.tool_id!r}")
            seen_ids.add(spec.tool_id)
            specs.append(spec)
        return specs

    def declaration_for(
        self, session_id: str, context_payload: Mapping[str, object]
    ) -> object | None:
        """The raw declaration in force: the payload in hand, else the record.

        A payload that CARRIES the key answers on its own — the request declaring
        the tools is the common path and must not pay a store read to re-answer
        what it is holding. A payload silent on the key falls through to the
        session's newest context event that carries one.
        """
        if self.CONTEXT_KEY in context_payload:
            return context_payload[self.CONTEXT_KEY]
        event = self._latest_event_of_type(session_id, "context", self.CONTEXT_KEY)
        payload = event.get("payload") if event else None
        if isinstance(payload, dict):
            return payload.get(self.CONTEXT_KEY)
        return None


def _unavailable(tool_id: str) -> dict[str, Any]:
    """The ``device_unavailable`` envelope, naming the cause and the cure.

    The message is the model's ONLY signal here, and it is relayed to a person
    who is holding the phone. "No client is attached to the event stream"
    describes our transport to someone who never agreed to know we have one;
    worse, the usual cause is mundane and fixable — the app lost foreground
    because the agent itself launched another app. Say that, and say what to do
    about it, or every recovery has to be guessed.
    """
    return {
        "status": "error",
        "error": {
            "code": "device_unavailable",
            "message": (
                f"The Mewbo Aura app is not currently reachable, so device tool "
                f"'{tool_id}' could not be delivered. This usually means Aura lost "
                "foreground — opening another app can do it. Ask the user to reopen "
                "Aura, then retry."
            ),
        },
    }


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

    Registered into the core seam (``mewbo_core.tooling.client_tools.DeviceToolDispatcher``)
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
        event append, no wait — when no executor is attached to the session's
        SSE stream and none was attached within ``DEVICE_EXECUTOR_GRACE_S``
        (``SessionEventBus.has_executor``): the common case of a
        re-engage/recover with no client attached would otherwise burn the
        full ``DEVICE_TOOL_TIMEOUT_S`` for a call that was never deliverable.
        The window is what keeps a mid-reconnect client — the ordinary state
        BETWEEN turns, since the stream self-closes the moment a session stops
        running — from reading as an absent one; see the constant's docstring
        for where five seconds comes from.

        Otherwise appends one ``device_tool_call`` event carrying a fresh
        single-use ``call_token``, then polls a ``threading.Event`` (the
        repo's verified cross-thread idiom — see module docstring) until the
        client resolves it or ``DEVICE_TOOL_TIMEOUT_S`` elapses.
        """
        bus = get_session_event_bus()
        if not bus.has_executor(session_id, grace_s=DEVICE_EXECUTOR_GRACE_S):
            return _unavailable(tool_id)

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
            # Re-check presence EVERY tick, not just at entry. The client can
            # go away mid-wait — which is exactly what happens when the tool
            # being dispatched launches another app and backgrounds ours — and
            # without this the dispatcher waits out its whole budget for a
            # result nobody is left to send. The grace window is re-applied
            # here rather than consumed at entry, so a client that drops mid-
            # wait gets the same seconds to come back that one dropping between
            # turns does, and the refusal still lands an order of magnitude
            # inside the 30s budget.
            if not event.is_set() and not bus.has_executor(
                session_id, grace_s=DEVICE_EXECUTOR_GRACE_S
            ):
                return _unavailable(tool_id)

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
    "DEVICE_EXECUTOR_GRACE_S",
    "DEVICE_TOOL_CALL_EVENT",
    "DEVICE_TOOL_TIMEOUT_S",
    "ApiDeviceToolDispatcher",
    "DevicePendingCalls",
    "DeviceToolBinding",
    "ResolveOutcome",
    "get_pending_calls",
    "reset_pending_calls_for_tests",
]
