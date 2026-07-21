"""``TriggerService`` — the reverse-invocation watcher (WP3).

The durable peer of the ``AgentHypervisor``: as ``RunRegistry`` is to runs and
``JobRecovery`` is to wiki jobs, this is to *reverse-invocations*. One daemon
thread watches the persisted :class:`~mewbo_core.triggers.store.TriggerStoreBase`
and, each tick, (a) expires triggers past ``expires_at``, (b) fires due
time/cron triggers, and (c) at a coarser cadence polls the forge REST API for
``ci.workflow`` / ``forge.pr`` triggers and fires the ones whose normalized
state satisfies ``spec.matches()``. Inbound ``webhook`` triggers fire through
the SAME pipeline from the route (:meth:`fire`).

Design invariants:

* **The store IS the schedule.** Nothing in-memory survives a restart by design
  (JobRecovery idiom): boot re-reads ``list_armed`` and every tick re-reads the
  store, so a restart resumes armed triggers with no in-memory reconstruction.
* **No ``if kind ==`` due/match dispatch.** Due-ness (``is_due``/``is_expired``)
  and match logic (``matches``) live on the model. The one place this service
  inspects kind is I/O *routing* — which forge endpoint yields a payload for a
  given trigger — which the model cannot own (models never import I/O clients).
* **One live run per session is load-bearing.** A fire into a busy session
  either enqueues a steering message (``action="message"``) or is re-armed
  silently for the next tick — never a second concurrent run.
* **Failures are logged transitions, never silent spins.** A trigger that errors
  ``max_consecutive_failures`` times in a row transitions to ``failed``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger
from mewbo_core.triggers.spec import CiWorkflowTrigger, ForgePrTrigger, TriggerSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from mewbo_core.config import TriggersConfig
    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.triggers.policy import TriggerPolicy
    from mewbo_core.triggers.store import TriggerStoreBase

    from mewbo_api.triggers.forge import ForgeClient

logging = get_logger(name="api.triggers.service")


@dataclass(frozen=True)
class TriggerFireContext:
    """The structured payload handed to the app-side deliver callback on a fire.

    In-process runtime data — a callback argument that crosses no trust boundary
    — so a frozen dataclass, not Pydantic (the process-boundary rule). It
    replaces the 4-positional ``deliver(session_id, wake, action, trigger_id)``
    signature that later grew a 4th arg, so the next field is additive
    rather than more positional creep.

    ``trigger_id`` lets the app side attribute the fire — the Mewbo Apps
    pipeline-run ledger opens a run keyed on the firing trigger (spec §2.8).
    """

    session_id: str
    wake: str
    action: str
    trigger_id: str


# The app-side re-engage closure. Returns True when a run was started or a
# steering message enqueued; False when the session was busy and the fire must
# re-arm.
DeliverFn = "Callable[[TriggerFireContext], bool]"

_PAYLOAD_SUMMARY_MAX = 1000
_LAST_ERROR_MAX = 500
_MAX_WATCHER_BACKOFF_S = 60.0


class TriggerService:
    """Watches trigger sources and reverse-invokes the sessions that armed them.

    Atomic feature class: injected collaborators are its state, the tick/fire
    pipeline are its methods. The daemon watcher is optional (:meth:`start` /
    :meth:`stop`) — every unit of scheduling work is reachable synchronously via
    :meth:`tick` / :meth:`fire`, so tests drive it with an injected clock and a
    fake forge/deliver, no threads or sleeps.
    """

    def __init__(
        self,
        *,
        runtime: SessionRuntime,
        store: TriggerStoreBase,
        policy: TriggerPolicy,
        config: TriggersConfig,
        forge_client_factory: Callable[[str], ForgeClient | None],
        deliver: Callable[[TriggerFireContext], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Capture collaborators. *clock* is injectable so tests advance time."""
        self._runtime = runtime
        self._store = store
        self._policy = policy
        self._config = config
        self._forge_client_factory = forge_client_factory
        self._deliver = deliver
        self._clock = clock or self._utcnow

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Trigger ids currently firing — guards a webhook fire racing the tick.
        self._in_flight: set[str] = set()
        # Consecutive fire/poll failures per trigger id (in-memory; resets on a
        # clean fire/poll or restart). Bounds a repeatedly-failing trigger.
        self._failures: dict[str, int] = {}
        # Forge-match signatures already fired, per trigger id — stops a
        # persistent match (a PR that stays merged) from re-firing every poll.
        # In-memory by design: a restart may re-fire once, bounded by max_fires.
        self._fired_signatures: dict[str, set[Any]] = {}
        self._last_forge_poll: datetime | None = None

    @staticmethod
    def _utcnow() -> datetime:
        """Default injectable clock — UTC now (tests pass a fixed clock instead)."""
        return datetime.now(timezone.utc)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Boot-rescan the store and launch the watcher daemon (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            armed_count = len(self._store.list_armed())
        except Exception:  # pragma: no cover - defensive boot read
            logging.warning("Trigger boot rescan failed", exc_info=True)
            armed_count = 0
        logging.info(
            "Trigger watcher starting; {} armed trigger(s) resumed from store", armed_count
        )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="trigger-watcher"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the watcher to stop and join it (best-effort)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _watch_loop(self) -> None:
        """Daemon loop: tick every ``tick_interval_seconds`` with error backoff."""
        backoff = self._config.tick_interval_seconds
        while not self._stop_event.is_set():
            try:
                self.tick()
                backoff = self._config.tick_interval_seconds
            except Exception:  # pragma: no cover - the per-trigger paths already guard
                logging.warning(
                    "Trigger watcher tick error (backing off {}s)", backoff, exc_info=True
                )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, _MAX_WATCHER_BACKOFF_S)
                continue
            self._stop_event.wait(self._config.tick_interval_seconds)

    # -- tick --------------------------------------------------------------

    def tick(self) -> None:
        """One scheduling sweep: expire, fire due time triggers, poll forge.

        Re-reads ``list_armed`` from the store (persisted state IS the
        schedule). Every per-trigger action is individually guarded so one bad
        trigger never sinks the sweep.
        """
        now = self._clock()
        try:
            armed = self._store.list_armed()
        except Exception:  # pragma: no cover - defensive store read
            logging.warning("Trigger tick: list_armed failed", exc_info=True)
            return
        forge_due: list[TriggerSpec] = []
        for trigger in armed:
            if trigger.is_expired(now):
                self._transition(trigger, "expired", reason="expires_at passed")
                continue
            if trigger.is_due(now):
                # Clock-driven kinds (time.at / time.cron). ``is_due`` returns
                # False for event-driven kinds (next_fire_at is None).
                self._fire(trigger, payload=None, now=now)
            elif isinstance(trigger, (CiWorkflowTrigger, ForgePrTrigger)):
                forge_due.append(trigger)
        if forge_due and self._forge_poll_due(now):
            self._last_forge_poll = now
            self._poll_forge(forge_due, now)

    def _forge_poll_due(self, now: datetime) -> bool:
        """Whether the coarser forge-poll cadence has elapsed since last poll."""
        if self._last_forge_poll is None:
            return True
        return (now - self._last_forge_poll).total_seconds() >= self._config.poll_interval_seconds

    def _poll_forge(self, triggers: list[TriggerSpec], now: datetime) -> None:
        """Poll forge state for each ci.workflow / forge.pr trigger and fire matches."""
        for trigger in triggers:
            repo = getattr(trigger, "repo", None)
            if not repo:
                continue
            client = self._forge_client_factory(repo)
            if client is None:
                logging.debug(
                    "No forge client for {}; skipping poll of trigger {}", repo, trigger.id
                )
                continue
            try:
                candidates = self._fetch_candidates(client, trigger)
            except Exception as exc:  # noqa: BLE001 - a poll error is bounded, not fatal
                self._note_failure(trigger, f"poll error: {exc}")
                continue
            match = next((p for p in candidates if trigger.matches(p)), None)
            if match is None:
                self._reset_failures(trigger.id)
                continue
            signature = self._match_signature(trigger, match)
            with self._lock:
                seen = self._fired_signatures.setdefault(trigger.id, set())
                if signature in seen:
                    continue
                seen.add(signature)
            self._reset_failures(trigger.id)
            self._fire(trigger, payload=match, now=now)

    @staticmethod
    def _fetch_candidates(client: ForgeClient, trigger: TriggerSpec) -> list[dict[str, Any]]:
        """Route to the right forge endpoint for *trigger*'s kind (I/O routing only).

        This is the sole kind inspection in the service, and it is about WHICH
        REST call yields a payload — NOT due-ness or match logic, both of which
        stay on the model. The model can't own this: it never imports I/O clients.
        """
        if isinstance(trigger, CiWorkflowTrigger):
            return client.poll_ci_runs(trigger.repo, ref=trigger.ref)
        if isinstance(trigger, ForgePrTrigger):
            return client.poll_pr(trigger.repo, trigger.number)
        return []

    @staticmethod
    def _match_signature(trigger: TriggerSpec, match: dict[str, Any]) -> Any:
        """A stable key for an observed match so identical state fires only once."""
        if isinstance(trigger, CiWorkflowTrigger):
            return ("ci", match.get("run_id"), match.get("conclusion"))
        if isinstance(trigger, ForgePrTrigger):
            return ("pr", match.get("event"))
        return ("payload", json.dumps(match, sort_keys=True, default=str))

    # -- fire pipeline -----------------------------------------------------

    def fire(self, trigger: TriggerSpec, *, payload: dict[str, Any] | None = None) -> None:
        """Public entry the webhook route uses; stamps ``now`` from the clock."""
        self._fire(trigger, payload=payload, now=self._clock())

    def _fire(self, trigger: TriggerSpec, *, payload: dict[str, Any] | None, now: datetime) -> None:
        """Dedup-guarded fire: never re-enter for a trigger already firing."""
        with self._lock:
            if trigger.id in self._in_flight:
                logging.debug("Trigger {} already firing; skipping re-entry", trigger.id)
                return
            self._in_flight.add(trigger.id)
        try:
            self._fire_inner(trigger, payload=payload, now=now)
        finally:
            with self._lock:
                self._in_flight.discard(trigger.id)

    def _fire_inner(
        self, trigger: TriggerSpec, *, payload: dict[str, Any] | None, now: datetime
    ) -> None:
        """Terminated-check → deliver → record. Busy delivery re-arms silently."""
        sid = trigger.session_id
        if self._runtime.is_terminated(sid):
            # A trigger must never fire into a permanently dead session.
            self._transition(trigger, "cancelled", reason=f"session {sid} terminated")
            return
        wake = self._compose_wake(trigger, payload)
        try:
            delivered = self._deliver(
                TriggerFireContext(
                    session_id=sid,
                    wake=wake,
                    action=trigger.action,
                    trigger_id=trigger.id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a delivery error is bounded, not fatal
            self._note_failure(trigger, f"deliver error: {exc}")
            return
        if not delivered:
            # One-live-run-per-session: the session was busy and the message
            # could not be enqueued (or action="start" refused). Leave the
            # trigger armed and retry next tick — never a 2nd concurrent run.
            logging.info(
                "Trigger {} not delivered (session {} busy); re-arming", trigger.id, sid
            )
            return
        self._runtime.append_event(
            sid,
            {
                "type": "trigger_fired",
                "payload": {
                    "trigger_id": trigger.id,
                    "kind": trigger.kind,
                    "payload_summary": self._payload_summary(payload) if payload else None,
                },
            },
        )
        trigger.record_fire(now)  # bumps fires; auto-completes at max_fires
        try:
            self._store.update(trigger)
        except KeyError:  # pragma: no cover - trigger removed mid-fire
            pass
        self._reset_failures(trigger.id)
        logging.info(
            "Trigger {} fired for session {} ({}, fire #{})",
            trigger.id,
            sid,
            trigger.status,
            trigger.fires,
        )

    def _compose_wake(self, trigger: TriggerSpec, payload: dict[str, Any] | None) -> str:
        """Build the wake message: the wake_prompt + a bounded payload summary."""
        if not payload:
            return trigger.wake_prompt
        summary = self._payload_summary(payload)
        if not summary:
            return trigger.wake_prompt
        return f"{trigger.wake_prompt}\n\n[trigger fired: {trigger.kind}] {summary}"

    @staticmethod
    def _payload_summary(payload: dict[str, Any] | None) -> str:
        """Bounded, deterministic string form of a fire payload."""
        if not payload:
            return ""
        try:
            text = json.dumps(payload, sort_keys=True, default=str)
        except Exception:  # pragma: no cover - str() of anything succeeds
            text = str(payload)
        return text[:_PAYLOAD_SUMMARY_MAX]

    # -- failure / transition bookkeeping ----------------------------------

    def _transition(self, trigger: TriggerSpec, to: str, *, reason: str) -> None:
        """Move a trigger to a terminal/resting status, persist, log. No-op if terminal."""
        try:
            trigger.transition(to)
        except ValueError:
            return  # already terminal — nothing to do
        logging.info("Trigger {} -> {} ({})", trigger.id, to, reason)
        try:
            self._store.update(trigger)
        except KeyError:  # pragma: no cover - trigger removed concurrently
            pass

    def _note_failure(self, trigger: TriggerSpec, message: str) -> None:
        """Record a bounded fire/poll error; transition to ``failed`` past the cap."""
        with self._lock:
            count = self._failures.get(trigger.id, 0) + 1
            self._failures[trigger.id] = count
        logging.warning(
            "Trigger {} error ({}/{}): {}",
            trigger.id,
            count,
            self._config.max_consecutive_failures,
            message,
        )
        trigger.last_error = message[:_LAST_ERROR_MAX]
        if count >= self._config.max_consecutive_failures and not trigger.is_terminal:
            try:
                trigger.transition("failed")
            except ValueError:  # pragma: no cover - guarded by is_terminal above
                pass
            logging.warning(
                "Trigger {} -> failed after {} consecutive errors", trigger.id, count
            )
        try:
            self._store.update(trigger)
        except KeyError:  # pragma: no cover - trigger removed concurrently
            pass

    def _reset_failures(self, trigger_id: str) -> None:
        """Clear the consecutive-failure counter after a clean fire/poll."""
        with self._lock:
            self._failures.pop(trigger_id, None)


__all__ = ["TriggerService", "TriggerFireContext", "DeliverFn"]
