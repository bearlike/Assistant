#!/usr/bin/env python3
"""Shared session runtime utilities for CLI and API."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from pydantic import ValidationError

from mewbo_core.classes import Plan, TaskQueue
from mewbo_core.common import get_logger
from mewbo_core.contracts.diff_stat import DiffStat
from mewbo_core.contracts.types import BLOCKED_CODES, EventRecord
from mewbo_core.hooks import OutcomeAssertion
from mewbo_core.llm.prompt_registry import get_prompt_registry
from mewbo_core.loop.task_master import orchestrate_session, orchestrate_session_async
from mewbo_core.session.event_cursor import EventCursor
from mewbo_core.session.session_digest import RESILIENCE_EVENT_TYPES
from mewbo_core.session.session_provenance import (
    CapabilityEvidence,
    SessionOrigin,
    SessionTag,
)
from mewbo_core.session.session_query import SessionQuery
from mewbo_core.session.session_store import (
    SessionRecord,
    SessionStoreBase,
    create_session_store,
)
from mewbo_core.tooling.session_tools import SessionTool

logging = get_logger(name="core.session_runtime")


def _derive_core_commands() -> set[str]:
    """Build CORE_COMMANDS from the command registry.

    Transcript-render commands route through the orchestrator's marker path.
    ``/terminate`` and ``/status`` are not in the registry and are added here.
    """
    from mewbo_core.session.commands import COMMANDS, CommandRender

    transcript = {
        f"/{name}"
        for name, cmd in COMMANDS.items()
        if cmd.render is CommandRender.TRANSCRIPT
    }
    return transcript | {"/terminate", "/status"}


CORE_COMMANDS = _derive_core_commands()

RecoveryAction = Literal["retry", "continue"]

# WHO asked for a recovery, stamped onto the ``recovery`` transcript event.
# Absent means a human asked — every recovery driven from the console/CLI — so
# the marker carries no trigger for that path. A named trigger is what makes an
# AUTOMATIC re-drive idempotent:
# the event is the ledger, so "has this trigger already fired on this session?"
# is answerable from the transcript alone — durable across restarts, replayed by
# every timeline consumer, and needing no new store field or attempt counter.
RecoveryTrigger = Literal["auto_goal_unmet"]

# The CLOSED session-status vocabulary. Status is DERIVED at read time from the
# last completion event and never stored, so teaching this map a new arm
# reclassifies every existing session with no migration. Kept a ``Literal``
# rather than bare strings because three surfaces mirror it (the console
# StatusBadge, the CLI, Aura): an arm spelled only here renders as nothing
# there, which is exactly how a laundered status stays invisible.
SessionStatus = Literal[
    "idle",
    "running",
    "completed",
    "incomplete",
    "canceled",
    "failed",
    "awaiting_approval",
    "terminated",
    "unmet_goal",
    "blocked",
]

# ``done_reason`` -> derived status. A TABLE, not a dispatch chain: the mapping
# is data, so a new reason is one row and cannot drift from a second reader.
#
# The two ``unmet_goal`` arms are the launder this closes. A halt sets
# ``done=True``, so ``halted_no_progress`` and ``verification_failed`` need an
# explicit row: the run stopped WITHOUT reaching its goal, and without one it
# presents as success and loses its recovery affordance. Absent from the table
# means "trust ``done``".
_STATUS_BY_DONE_REASON: dict[str, SessionStatus] = {
    "canceled": "canceled",
    "error": "failed",
    # Slash-command failure paths — parity with ``error`` so the FE renders the
    # red pill rather than the green one.
    "compact_failed": "failed",
    "max_steps_reached": "incomplete",
    # Graduated exhaustion: the loop forced a wrap-up turn and finished cleanly
    # (``done=True``) but never reached natural completion — recoverable, same
    # as ``max_steps_reached``. ``halted_agent_budget`` is the per-agent
    # contract wave's spelling of the same outcome.
    "budget_exhausted": "incomplete",
    "halted_agent_budget": "incomplete",
    "awaiting_approval": "awaiting_approval",
    "halted_no_progress": "unmet_goal",
    "verification_failed": "unmet_goal",
    # The session-budget observer stopped the run. Honest as "did not reach
    # its goal", not masked as a clean completion and not conflated with
    # ``BLOCKED_CODES`` (an infra-reachability wall, a different mechanism).
    "safety_blocked": "unmet_goal",
    # Stamped directly by an outcome assertion upstream (e.g. a product hook
    # that owns the job the session was driving and can see it never reached
    # its terminal tool). Mapped here so the assertion needs no second reader.
    "unmet_goal": "unmet_goal",
}

# Tool-envelope error codes that make a stopped run USER-ACTIONABLE rather than
# merely failed — a credential, a network path, a permission or a quota someone
# can actually go fix. The loop carries the last UNRECOVERED one onto the
# completion payload as ``blocked_code``; a code outside this set is ignored
# here, so an unrecognised value can never widen the status vocabulary.
# (``BLOCKED_CODES`` itself is imported above, as is ``RESILIENCE_EVENT_TYPES``
# — the two events carrying the evidence behind a model-attributable failure.
# That one comes from ``session_digest`` rather than being declared here because
# a listing now SELECTS the events a summary folds instead of loading whole
# transcripts, so the set the projection picks and the set this fold reads have
# to be ONE set.)


class SessionTerminatedError(ValueError):
    """Raised when an operation targets a permanently terminated session.

    Termination is a kill switch: a dead session must not be runnable,
    steerable, recoverable, *or fork-resurrectable* — copying a terminated
    transcript into a fresh session would let an agent launder its way around
    the kill. Raised at the core seam so every caller inherits enforcement;
    HTTP surfaces map it to 410 Gone.
    """

# Context-event keys that gate capability-scoped behaviour (e.g. wiki/QA
# AgentDef visibility, structured-workspace grounding). On recovery the
# orchestrator reads the MOST-RECENT context event, so these must be carried
# forward or a recovered run silently loses its capability. Kept generic — we
# preserve whatever the session already had, never an origin→capability map.
_RECOVERY_GATING_KEYS: tuple[str, ...] = ("client_capabilities", "structured_workspace")


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def parse_core_command(text: str) -> str | None:
    """Return the core command token if present."""
    if not text:
        return None
    command = text.strip().lower().split()[0]
    return command if command in CORE_COMMANDS else None


@dataclass
class RunHandle:
    """Active orchestration tracking.

    A run is backed by EITHER a daemon thread (CPython sync-wrapper path) or an
    event-loop task (Pyodide WebLoop / async harnesses). Exactly one of
    ``thread`` / ``loop_active`` reflects liveness; every other field applies
    uniformly. ``is_alive()`` is the backend-agnostic liveness check.

    ``task`` holds the loop-backed run's ``asyncio.Task`` (mirrors
    ``AgentHandle.asyncio_task`` in ``hypervisor.py`` / ``_lifecycle_tasks`` in
    ``spawn_agent.py``): ``register_loop_run`` mints the handle before the
    coroutine exists, so ``task`` starts ``None`` and is attached via
    :meth:`RunRegistry.attach_loop_task` right after ``loop.create_task(...)``.
    Holding this strong reference on the registry-owned handle is what keeps
    asyncio from garbage-collecting the fire-and-forget task mid-run — a bare
    local ``task = loop.create_task(...)`` with no held reference is eligible
    for GC as soon as the enclosing function returns.
    """

    cancel_event: threading.Event
    started_at: str
    thread: threading.Thread | None = field(default=None)
    loop_active: bool = field(default=False)
    message_queue: queue.Queue[str] | None = field(default=None)
    interrupt_step: threading.Event | None = field(default=None)
    task: asyncio.Task | None = field(default=None)

    def is_alive(self) -> bool:
        """Return whether the underlying run is still in progress."""
        if self.thread is not None:
            return self.thread.is_alive()
        return self.loop_active


class RunRegistry:
    """Track active orchestration runs (thread- or loop-backed) per session."""

    def __init__(self) -> None:
        """Initialize the run registry."""
        self._lock = threading.Lock()
        self._runs: dict[str, RunHandle] = {}

    def start(
        self,
        session_id: str,
        target: Callable[[threading.Event], None],
        *,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> bool:
        """Start a new thread-backed run for the session if not already active.

        *on_release* fires once, on the run's own thread, AFTER the handle has
        been dropped — so the session's slot is genuinely free and a callback
        may start a follow-up run on it. Firing it any earlier (from the run's
        own ``finally``, say) cannot work: this registry still holds the slot
        there, so a same-session ``start_async`` refuses and returns ``""``.
        """
        with self._lock:
            existing = self._runs.get(session_id)
            if existing and existing.is_alive():
                return False
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._wrap_run,
                args=(session_id, cancel_event, target, on_release),
                daemon=True,
            )
            self._runs[session_id] = RunHandle(
                thread=thread,
                cancel_event=cancel_event,
                started_at=_utc_now(),
                message_queue=message_queue,
                interrupt_step=interrupt_step,
            )
            thread.start()
            return True

    def register_loop_run(
        self,
        session_id: str,
        *,
        cancel_event: threading.Event,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
    ) -> bool:
        """Register a loop-backed run (no thread). Mirror of :meth:`start`.

        Used when the caller is already inside a running event loop (Pyodide's
        WebLoop) and drives the orchestration as an ``asyncio`` task rather than
        a daemon thread. Returns ``False`` if a live run already exists for the
        session. The caller must invoke :meth:`finalize_loop_run` from the
        task's done-callback so the registry stays consistent.
        """
        with self._lock:
            existing = self._runs.get(session_id)
            if existing and existing.is_alive():
                return False
            self._runs[session_id] = RunHandle(
                cancel_event=cancel_event,
                started_at=_utc_now(),
                loop_active=True,
                message_queue=message_queue,
                interrupt_step=interrupt_step,
            )
            return True

    def attach_loop_task(self, session_id: str, task: asyncio.Task) -> None:
        """Attach a loop-backed run's ``asyncio.Task`` to its handle.

        ``register_loop_run`` reserves the run's slot before the coroutine —
        and thus the task — exists, so the task is attached here right after
        the caller's ``loop.create_task(...)``. From that point on the
        registry (via the handle) holds a strong reference for the run's
        lifetime, preventing the fire-and-forget task from being garbage
        collected mid-run. A no-op if the handle is already gone (e.g. the
        task finished and its done-callback finalized the run before this
        call — not expected in practice but harmless).
        """
        with self._lock:
            handle = self._runs.get(session_id)
            if handle is not None:
                handle.task = task

    def finalize_loop_run(self, session_id: str) -> None:
        """Mark a loop-backed run as completed and drop its handle."""
        with self._lock:
            handle = self._runs.get(session_id)
            if handle is not None and handle.thread is None:
                self._runs.pop(session_id, None)

    def _wrap_run(
        self,
        session_id: str,
        cancel_event: threading.Event,
        target: Callable[[threading.Event], None],
        on_release: Callable[[], None] | None = None,
    ) -> None:
        try:
            target(cancel_event)
        finally:
            with self._lock:
                handle = self._runs.get(session_id)
                current_ident = threading.current_thread().ident
                if (
                    handle is not None
                    and handle.thread is not None
                    and handle.thread.ident == current_ident
                ):
                    self._runs.pop(session_id, None)
            # Post-release, and INSIDE the finally so a run that raised still
            # reaches it — the callback's whole job is to look at the terminal
            # the run left behind, and a crash is a terminal too. It owns its
            # own failure isolation; a raise here would replace the run's
            # exception with the callback's, which is why nothing above depends
            # on it returning.
            if on_release is not None:
                on_release()

    def cancel(self, session_id: str) -> bool:
        """Request cancellation for an active session run."""
        with self._lock:
            handle = self._runs.get(session_id)
            if not handle:
                return False
            handle.cancel_event.set()
            return True

    def is_running(self, session_id: str) -> bool:
        """Return True if the session has an active run."""
        with self._lock:
            handle = self._runs.get(session_id)
            return bool(handle and handle.is_alive())

    def get_cancel_event(self, session_id: str) -> threading.Event | None:
        """Return the cancel event for a session, if present."""
        with self._lock:
            handle = self._runs.get(session_id)
            return handle.cancel_event if handle else None

    def get_handle(self, session_id: str) -> RunHandle | None:
        """Return the run handle for a session, if present."""
        with self._lock:
            return self._runs.get(session_id)


class GoalRetryGate:
    """Re-invoke a session ONCE when it ended without meeting its stated goal.

    The escalation that begins where the in-band gates stop. A run that claims a
    clean terminal while a declared obligation is unmet is nudged inside the
    turn, with the context still warm and bounded to three attempts; that is
    strictly the cheaper correction and it stays the first line. This gate is
    what remains when the nudges are spent and the session has already ended
    ``unmet_goal`` — the case a human otherwise fixes by clicking Continue,
    which routinely produces the missing result in ONE further step.

    **Exactly one automatic attempt, ever.** The ledger is the ``recovery``
    transcript event the recovery path already writes: this gate stamps it with
    ``trigger="auto_goal_unmet"``, and refuses whenever a marker carrying that
    trigger is already present. Durable, restart-proof, and no new store field.

    Cost ceiling — state it plainly, because nothing else in the system bounds a
    session's token spend (``TokenBudget`` is compaction accounting, not a wallet):
    **one additional run per session, whose own cost is bounded only by that
    run's step/wall budget.** Worst case a session costs twice what it otherwise
    would. That is the entire price, and it cannot compound: the second run's
    terminal reaches this gate too, finds its own marker, and refuses.

    ``action="continue"`` is load-bearing, not a preference. ``retry`` truncates
    the transcript back past the failed turn, which would delete the marker and
    make the one-shot guard structurally impossible to enforce.

    Cost class: ``O(one record)`` — one digest fold plus one transcript read for
    the marker scan, on a terminal path, once per run.
    """

    #: Stamped on the marker this gate writes and matched when refusing.
    TRIGGER: ClassVar[RecoveryTrigger] = "auto_goal_unmet"

    #: Session types this gate REFUSES, by :class:`SessionTag` session type.
    #:
    #: A wiki INDEXING job already carries three independent correction layers —
    #: finalize fails a job that produced no pages and again one that produced an
    #: empty graph, and boot-time job recovery re-runs a failed job up to its own
    #: cap. A generic re-invocation would stack a fourth attempt on the most
    #: expensive workload in the product, and it would be invisible to that cap,
    #: which counts only its own retries. The uncovered case this gate exists for
    #: is wiki QA, which shares the ``wiki`` origin but not the session type —
    #: hence the exclusion keys off the type, and is a named table so a second
    #: self-correcting job kind is one row rather than a new branch.
    #:
    #: ``wiki_act`` (the scoped-refresh act session, ``session_provenance.py``'s
    #: ``wiki:act:`` sub-kind) joins it for the identical reason: a scoped
    #: refresh already checkpoints its own stage-2 retry — ``ScopedRefreshRunner``
    #: resumes from ``stage="act"`` under the same slug-keyed ``JobRecovery`` cap
    #: wiki_index uses — and ``WikiIndexingSessionEndHook`` deliberately leaves a
    #: scoped job's non-terminal status alone (``_is_scoped_refresh`` in
    #: ``apps/mewbo_api/src/mewbo_api/wiki/jobs.py``) precisely so the RUNNER
    #: owns that job's terminality. A generic re-drive here would race that
    #: runner with a second, uncapped correction attempt on the same session.
    EXCLUDED_SESSION_TYPES: ClassVar[frozenset[str]] = frozenset({"wiki_index", "wiki_act"})

    def __init__(self, *, runtime: SessionRuntime) -> None:
        """Bind the gate to the runtime that owns the session it re-drives."""
        self._runtime = runtime

    def maybe_retry(self, session_id: str, relaunch: Callable[[str], str]) -> bool:
        """Re-drive *session_id* once if its run ended with an unmet goal.

        *relaunch* starts a fresh run on the session with the given query,
        carrying the ORIGINAL run's collaborators (tool registry, hooks,
        capability mode, cwd …) unchanged, and returns its run id — ``""`` when
        the registry refused. Returns True only when a retry was actually
        started.

        Total by construction: a best-effort correction must never break the
        completion of the run that triggered it, so every failure is logged and
        swallowed. It is called from the run's own thread after that run's
        registry slot has been released.
        """
        try:
            return self._retry(session_id, relaunch)
        except Exception as exc:  # noqa: BLE001 — a side effect at the edge
            logging.warning(
                "Automatic goal retry skipped for session {}: {}: {}",
                session_id,
                type(exc).__name__,
                exc,
            )
            return False

    def _retry(self, session_id: str, relaunch: Callable[[str], str]) -> bool:
        """Run the four checks, then drive the one re-invocation."""
        store = self._runtime.session_store
        record = store.load_session_records([session_id])[session_id]
        summary = self._runtime.summarize_session(session_id, record=record)
        if summary.get("status") != "unmet_goal" or not summary.get("recoverable"):
            return False
        excluded = self._excluded_type(record.tags)
        if excluded is not None:
            logging.info(
                "Automatic goal retry declined for session {}: {} sessions are "
                "excluded — that workload owns its own retry policy",
                session_id,
                excluded,
            )
            return False
        if self._already_triggered(session_id):
            logging.info(
                "Automatic goal retry already spent for session {}; not retrying",
                session_id,
            )
            return False

        goal = self._goal_text(summary)
        query = get_prompt_registry().render("loop.goal_unmet_retry", goal=goal)
        # Writes the marker AND yields the query in one call, so the ledger this
        # gate reads back is the same event every other recovery writes.
        query = self._runtime.resolve_recovery_query(
            session_id,
            "continue",
            replacement_text=query,
            trigger=self.TRIGGER,
        )
        self._runtime.reinject_recovery_context(session_id)
        run_id = relaunch(query)
        if not run_id:
            logging.warning(
                "Automatic goal retry for session {} was refused by the run "
                "registry; the one-shot budget is spent",
                session_id,
            )
            return False
        logging.info(
            "Automatic goal retry started for session {} as run {} (goal: {})",
            session_id,
            run_id,
            goal,
        )
        return True

    @classmethod
    def _excluded_type(cls, tags: list[str]) -> str | None:
        """Return the excluded session type *tags* names, or ``None``."""
        for tag in tags:
            parsed = SessionTag.parse(tag)
            if parsed is not None and parsed.session_type in cls.EXCLUDED_SESSION_TYPES:
                return parsed.session_type
        return None

    def _already_triggered(self, session_id: str) -> bool:
        """True when this gate's marker is already in the transcript.

        Reads the FULL transcript rather than the digest: the digest projects the
        events a summary folds, and this one is not among them, so a shorter read
        would report "never retried" for a session that had been.
        """
        for event in self._runtime.load_events(session_id):
            if event.get("type") != "recovery":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("trigger") == self.TRIGGER:
                return True
        return False

    @staticmethod
    def _goal_text(summary: Mapping[str, object]) -> str:
        """Name the goal that went unmet, in the most specific terms available.

        An outcome assertion's one-line ``detail`` is written by the component
        that OWNS the goal, so it is the most specific statement of it; its
        ``reason`` token is the same fact, coarser. A loop-derived terminal
        (a halt, a failed verification) has neither, and the ``done_reason``
        token is then the only honest thing to name.
        """
        for key in ("unmet_goal_detail", "unmet_goal_reason", "done_reason"):
            value = summary.get(key)
            if isinstance(value, str) and value:
                return value
        return "the task this session was started to complete"


class SessionRuntime:
    """Shared orchestration runtime surface for CLI and API."""

    def __init__(
        self,
        *,
        session_store: SessionStoreBase | None = None,
        run_registry: RunRegistry | None = None,
        goal_retry_gate: GoalRetryGate | None = None,
    ) -> None:
        """Initialize the runtime with session storage and optional run registry."""
        self._session_store = session_store or create_session_store()
        self._run_registry = run_registry or RunRegistry()
        # The one-shot re-invocation of a session that ended without meeting its
        # goal. Injected so a caller can substitute or disable it; the default
        # binds to this runtime, which owns every seam the gate needs.
        self._goal_retry_gate = goal_retry_gate or GoalRetryGate(runtime=self)
        # Callbacks fired once when a session is permanently terminated. The
        # seam a Wave-2 trigger service registers on to cascade-cancel a
        # session's scheduled triggers; empty by default so termination has no
        # extra side effects until something opts in.
        self._on_terminate: list[Callable[[str], int | None]] = []

    @property
    def session_store(self) -> SessionStoreBase:
        """Expose the underlying session store."""
        return self._session_store

    def resolve_session(
        self,
        *,
        session_id: str | None = None,
        session_tag: str | None = None,
        fork_from: str | None = None,
        fork_at_ts: str | None = None,
        owner: str | None = None,
    ) -> str:
        """Resolve session identifiers, tags, and forks to a session id.

        When *fork_at_ts* is provided alongside *fork_from*, only events up to
        (and including) that timestamp are copied into the new session.

        *owner* stamps whichever NEW session this call mints — a fresh one or a
        fork. It is an opaque subject string; core never learns what a principal
        is (see ``SessionStoreBase.create_session``). Resolving to an EXISTING
        session ignores it: ownership is established once, at creation, so
        re-engaging someone else's session can never quietly re-stamp it.
        """
        if fork_from:
            source_session_id = self._session_store.resolve_tag(fork_from) or fork_from
            if self.is_terminated(source_session_id):
                raise SessionTerminatedError(
                    f"session {source_session_id} is permanently terminated; "
                    "a terminated session cannot be forked"
                )
            if fork_at_ts:
                session_id = self._session_store.fork_session_at(
                    source_session_id, fork_at_ts, owner
                )
            else:
                session_id = self._session_store.fork_session(source_session_id, owner)
        if session_tag and not session_id:
            resolved = self._session_store.resolve_tag(session_tag)
            session_id = resolved if resolved else None
        if not session_id:
            session_id = self._session_store.create_session(owner)
        if session_tag:
            self._session_store.tag_session(session_id, session_tag)
        assert session_id is not None
        return session_id

    def ensure_session(self, session_id: str) -> None:
        """Idempotently materialise a session record for a pre-minted id.

        Thin delegate to the store's ``ensure_session``. A caller that minted a
        ``session_id`` outside ``resolve_session`` (e.g. the realtime recorder,
        which pre-mints to open a Langfuse trace before any store write) calls
        this so the session is a real RECORD — visible to ``list_sessions`` and
        every read surface — not an orphan transcript.
        """
        self._session_store.ensure_session(session_id)

    def append_context_event(self, session_id: str, context: dict[str, object]) -> None:
        """Append a context event to the session transcript."""
        if not context:
            return
        self._session_store.append_event(session_id, {"type": "context", "payload": context})

    def tag_session(self, session_id: str, tag: str) -> None:
        """Associate a provenance/lookup tag with a session.

        Thin delegate to the store so callers that already hold a resolved
        ``session_id`` (e.g. a structured/realtime run stamping its origin tag)
        don't reach into ``session_store`` directly. ``resolve_session`` remains
        the seam for tag-keyed *resolution*; this is the write-only sibling for
        tagging a session you've already created.
        """
        self._session_store.tag_session(session_id, tag)

    def append_event(self, session_id: str, event: dict[str, object]) -> None:
        """Append a raw transcript event verbatim.

        Unlike :meth:`append_context_event` (which wraps payloads as
        ``{"type": "context", ...}``), this writes the event as-is, so a
        ``completion`` event reaches :meth:`summarize_session` — the single
        status authority — instead of being hidden inside a context payload.
        """
        self._session_store.append_event(session_id, event)

    def summarize_session(
        self,
        session_id: str,
        *,
        events: list[EventRecord] | None = None,
        record: SessionRecord | None = None,
    ) -> dict[str, object]:
        """Return a summarized view of a session.

        *record* supplies the session's stored metadata (title, archived,
        terminated, owner, tags) when the caller has already batch-loaded it for
        a whole page — see :meth:`list_sessions`. Omitting it reads the same
        five facts one at a time, which is what every single-session caller
        does and what this method has always done, so the derived summary is
        identical either way; only the number of store reads differs.

        With no *events*, the fold's input is the store's DIGEST of the session
        rather than its whole transcript — the same projection a listing row
        gets, and identical in result. It
        matters most on the poll path, which re-derives status once a second per
        open client: on the largest live session that read was 0.211 s of a
        0.470 s poll, for a status that had not changed.
        """
        if events is None:
            events = self._session_store.session_digest(session_id).events
        if record is None:
            record = self._session_store.load_session_records([session_id])[session_id]
        created_at = events[0]["ts"] if events else None
        stored_title = record.title
        title = stored_title
        status: SessionStatus = "idle"
        done_reason = None
        blocked_code: str | None = None
        has_user_event = False
        # Evidence for the model-attributable-failure projection, gathered in
        # this SAME pass — ``list_sessions`` calls this per session, so a second
        # walk of every transcript is a cost with no new information.
        failure_reason: str | None = None
        models_tried: list[str] = []
        outcome_assertion: OutcomeAssertion | None = None
        # Two more facts gathered in that same single pass, for the same reason:
        # how many lines the session changed, and which gated capabilities it
        # actually exercised (as opposed to which ones its client advertised).
        diff_stat = DiffStat()
        evidence = CapabilityEvidence()
        for event in events:
            evidence.observe(event)
            if event.get("type") == "tool_result":
                result_payload = event.get("payload", {})
                if isinstance(result_payload, dict):
                    diff_stat = diff_stat + DiffStat.from_tool_result(result_payload)
            if event.get("type") == "user":
                has_user_event = True
                if title is None:
                    payload = event.get("payload", {})
                    if isinstance(payload, dict):
                        raw = payload.get("text")
                        if isinstance(raw, str):
                            title = raw[:120]
            if event.get("type") in RESILIENCE_EVENT_TYPES:
                failure_reason = self._read_resilience_event(
                    event, models_tried, failure_reason
                )
            if event.get("type") == "outcome_assertion":
                assertion = self._read_outcome_assertion(event)
                if assertion is not None:
                    outcome_assertion = assertion
            if event.get("type") == "completion":
                payload = event.get("payload", {})
                if isinstance(payload, dict):
                    # A NEW terminal supersedes any assertion made about the
                    # PREVIOUS one. A session hosts many turns, and turn 5's
                    # status must not inherit turn 1's unmet purpose — the
                    # assertion is appended right after the terminal it
                    # describes, so append order alone scopes it correctly.
                    outcome_assertion = None
                    # EXACTLY ONE terminal event governs the derived state, and
                    # every field below is re-read from THIS payload. A session
                    # can carry two contradictory terminals — a boot sweep
                    # stamps a synthetic "interrupted" completion for a run it
                    # judged dead, and the run, still alive, appends its real
                    # one minutes later — so deriving field-by-field across
                    # events would blend the sweep's reason into the live run's
                    # status. Last terminal wins, whole.
                    done_reason = payload.get("done_reason")
                    status, blocked_code = self._completion_status(payload)
                    self._read_models_tried(payload, models_tried)
        # An outcome assertion is the ONLY signal that can contradict a terminal
        # the loop itself considers clean, so it is applied against the derived
        # status rather than folded into the reason table. It promotes ONLY a
        # claim of success: `failed`/`canceled` are already honest, and `blocked`
        # is both more specific and more actionable, so none of them is
        # overwritten by the coarser "purpose not met".
        if outcome_assertion is not None and status == "completed":
            status = "unmet_goal"
        running = self.is_running(session_id)
        if running:
            status = "running"
        # Permanent termination is the terminal-most state — it wins over
        # EVERYTHING, including a still-unwinding live run (cancellation is
        # cooperative, so ``is_running`` can briefly lag a terminate). Checked
        # AFTER the running override so ``terminated`` is never masked.
        terminated_at = record.terminated_at
        terminated = terminated_at is not None
        if terminated:
            status = "terminated"
        if not has_user_event and not running:
            created_at = None
        if not title:
            title = f"Session {session_id[:8]}"
        merged_context = self._session_store.merge_context_events(events)
        origin = SessionOrigin.classify(
            record.tags, merged_context
        )
        # ``recoverable`` = the FE/CLI may offer a Continue/Restart affordance.
        # True when the session is not running, did not complete successfully,
        # and has a prior user turn (so ``resolve_recovery_query`` won't raise).
        # The crucial case is a session that died mid-call with NO ``completion``
        # event at all (process killed) — status stays ``idle`` but a user turn
        # exists, so it must be recoverable.
        # ``awaiting_approval`` is recoverable: a plan-mode proposal (or a wiki QA
        # answer) parks here with no active run and no auto-exit. Recovery
        # ``continue`` re-engages it through the one loop, mirroring send_followup —
        # the only other way out. Only ``completed`` is genuinely terminal.
        # This is why the status vocabulary is where honesty has to be fixed
        # rather than the recovery rule: ``unmet_goal`` and ``blocked`` earn the
        # affordance purely by not being ``completed``, so every run the old
        # table laundered into success had lost it silently.
        recoverable = not running and status != "completed" and has_user_event
        # A terminated session is a hard dead-end — never offer Continue/Restart.
        if terminated:
            recoverable = False
        # Surface the EXERCISED capabilities + the workspace so the landing page
        # can show what a session actually did without re-reading the transcript.
        # Copying ``context.client_capabilities`` verbatim would be wrong: that
        # field is an ADVERTISEMENT, and the console sends the same fixed header
        # on every request, so every chat row would be chipped with four
        # capabilities it never touched.
        # ``CapabilityEvidence`` (fed above, in the one pass) holds each earnable
        # capability to a durable artifact event or a successful invocation of a
        # tool it gates, and passes the server-written scopes (``wiki``/``scg``)
        # through untouched — see its docstring for why the split falls there. A
        # runtime-granted capability is still intentionally NOT probed: it's a
        # live predicate, not a durable signal, already surfaced where it matters
        # (the run's Langfuse trace facet), and a per-row store probe in a session
        # LIST would be the wrong tradeoff.
        capabilities = evidence.resolve(merged_context.get("client_capabilities"))
        workspace = merged_context.get("structured_workspace") or merged_context.get(
            "workspace"
        )
        summary: dict[str, object] = {
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "status": status,
            "done_reason": done_reason,
            "running": running,
            "recoverable": recoverable,
            "context": merged_context,
            "origin": origin.value,
            "capabilities": capabilities,
            "workspace": str(workspace) if workspace else None,
            "archived": record.archived,
            "terminated": terminated,
            "terminated_at": terminated_at,
        }
        # ``owner`` is APPENDED, and only when there IS one. An unowned session
        # is genuinely different from one owned by nobody, and reporting the
        # difference as an absent key rather than an explicit null is what keeps
        # this summary key-free for a deployment running without identity: no
        # principal exists there, so nothing is ever stamped, so the key never
        # appears. Appending (never inserting mid-dict) preserves key order too.
        owner = record.owner
        if owner is not None:
            summary["owner"] = owner
        # ``pinned`` follows the same append-when-present rule, and for a third
        # reason on top of the two above: pinning is a minority state, so an
        # unpinned session's summary carries neither key. Both keys travel
        # together — the boolean is what
        # a surface renders, the stamp is what it orders by — so a client never
        # has to infer one from the other. Read off *record* rather than a
        # direct store call — the whole point of batch-loading it is that a
        # listing's per-row cost stops scaling with row count; calling back into
        # the store here would reopen exactly that per-row round trip for these
        # two fields alone while every other field on the row stayed batched.
        if record.pinned_at is not None:
            summary["pinned"] = True
            summary["pinned_at"] = record.pinned_at
        # ``projects`` is the accumulated SET a project filter needs — never just
        # the current ``context.project`` — because an auto-select session may
        # have switched mid-task, and a filter reading only the latest binding
        # would miss every project it moved out of. Same append-when-present
        # rule: a session bound to nothing (a bare temp dir, or still on the
        # ``auto`` sentinel) carries no key at all.
        if record.projects:
            summary["projects"] = record.projects
        # The three failure facets follow the same append-when-present rule, for
        # the same reason: a session that never blocked and never re-tried a
        # model carries none of them.
        #
        # ``blocked_code`` says WHICH wall the run hit — the status alone says a
        # user can act, not what to act on. ``failure_reason`` + ``models_tried``
        # are what let a recovery affordance default AWAY from the model that
        # just failed instead of re-running the same one, which is the whole
        # point of recording them: retrying on a model whose failure was
        # model-specific amplifies the failure.
        if blocked_code is not None:
            summary["blocked_code"] = blocked_code
        if outcome_assertion is not None:
            # Surfaced whenever an assertion was made, even where it did not
            # change the status: a run that failed AND missed its purpose is
            # two facts, and dropping the second would hide the one a product
            # owner can act on.
            summary["unmet_goal_reason"] = outcome_assertion.reason
            if outcome_assertion.detail:
                summary["unmet_goal_detail"] = outcome_assertion.detail
        if failure_reason is not None:
            summary["failure_reason"] = failure_reason
        if models_tried:
            summary["models_tried"] = models_tried
        # Same append-when-present rule: a session that changed no lines carries
        # no ``diff_stat`` at all, so a consumer can treat the key's presence as
        # "this session edited something" without comparing against zero.
        if not diff_stat.is_empty:
            summary["diff_stat"] = {
                "additions": diff_stat.additions,
                "deletions": diff_stat.deletions,
            }
        return summary

    def _completion_status(
        self, payload: Mapping[str, object]
    ) -> tuple[SessionStatus, str | None]:
        """Derive ``(status, blocked_code)`` from ONE completion payload.

        Takes a ``Mapping`` because the caller holds an ``EventPayload`` — a
        union of the payload TypedDicts and the generic JSON dict. Every member
        satisfies ``Mapping[str, object]``, so the contract is accepted whole
        rather than the caller having to widen (or cast) at the call site.

        ``blocked`` outranks every reason-derived status. An unrecovered
        repo-access / network / permission / quota envelope is both the more
        SPECIFIC fact about why the run stopped and the only one a user can act
        on, so it must not be masked by the coarser reason underneath it.

        A reason absent from :data:`_STATUS_BY_DONE_REASON` falls back to
        trusting ``done``.
        """
        raw_code = payload.get("blocked_code")
        if isinstance(raw_code, str) and raw_code in BLOCKED_CODES:
            return "blocked", raw_code
        done_reason = payload.get("done_reason")
        if isinstance(done_reason, str):
            mapped = _STATUS_BY_DONE_REASON.get(done_reason)
            if mapped is not None:
                return mapped, None
            if done_reason.startswith("command_failed:"):
                return "failed", None
        return ("completed" if payload.get("done") else "incomplete"), None

    @staticmethod
    def _read_resilience_event(
        event: EventRecord, models_tried: list[str], current_reason: str | None
    ) -> str | None:
        """Fold one ``llm_retry``/``llm_fallback`` event into the failure facets.

        Appends any newly-named model to *models_tried* in attempt order and
        returns the reason token to carry forward.

        Precedence is deliberate: ``llm_fallback.reason`` IS the
        ``RetryStrategy.classify`` reason token (or ``retries_exhausted`` when a
        transient error simply spent the per-model cap), so it is preferred
        whenever a switch occurred. ``llm_retry`` carries only the exception
        CLASS — but a run that exhausted its retries on a single model never
        emits a fallback at all, which is precisely the shape that burned five
        attempts on one model, so dropping that case would blind the projection
        to the failure it most needs to describe.
        """
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return current_reason
        if event.get("type") == "llm_fallback":
            for key in ("from_model", "to_model"):
                name = payload.get(key)
                if isinstance(name, str) and name and name not in models_tried:
                    models_tried.append(name)
            candidate = payload.get("reason")
        else:
            name = payload.get("model")
            if isinstance(name, str) and name and name not in models_tried:
                models_tried.append(name)
            candidate = payload.get("error_type")
        if isinstance(candidate, str) and candidate:
            return candidate
        return current_reason

    @staticmethod
    def _read_outcome_assertion(event: EventRecord) -> OutcomeAssertion | None:
        """Validate a persisted outcome-assertion event, or ignore it.

        A stored document is a trust boundary, so it is parsed through the model
        rather than read key-by-key. Total by design: a malformed record must not
        break the status derivation for the whole session — a status that cannot
        be computed is strictly worse than one missing an assertion.
        """
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            return OutcomeAssertion.model_validate(payload)
        except ValidationError:
            logging.warning("Ignoring malformed outcome_assertion event payload")
            return None

    @staticmethod
    def _read_models_tried(payload: Mapping[str, object], models_tried: list[str]) -> None:
        """Fold a completion's recorded provider list into *models_tried*.

        ``RunError.provider`` is populated from ``LlmResilienceExhausted``'s own
        ``models_tried`` (comma-joined), so this is the ONLY evidence for a run
        that died without ever emitting a retry or fallback event — a fatal
        first attempt names no model anywhere else.
        """
        detail = payload.get("error_detail")
        if not isinstance(detail, dict):
            return
        provider = detail.get("provider")
        if not isinstance(provider, str):
            return
        for name in provider.split(","):
            cleaned = name.strip()
            if cleaned and cleaned not in models_tried:
                models_tried.append(cleaned)

    def list_sessions(
        self,
        query: SessionQuery | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        """List sessions with summary metadata, narrowed by *query*.

        ``O(collection)`` in rows, and never ``O(all history)`` in reads. TWO
        batched store calls serve the whole page, and neither scales with how
        much any session has recorded:

        * :meth:`~SessionStoreBase.list_session_digests` returns each session's
          transcript reduced to the events a summary folds. A per-id
          ``load_transcript`` here would make a listing read every event ever
          stored, and degrade monotonically forever.
        * ``load_session_records`` returns the metadata that is stored ON the
          session rather than folded from events (title, archived, terminated,
          owner, tags, ``pinned_at``, ``projects``) in one call for the page
          instead of per-row reads.

        **`query` narrows the digest fetch itself, and that ORDER is what makes
        it compose with a page.** The whole query goes to
        ``list_session_digests`` (still called EXACTLY ONCE per listing, which a
        test pins), so every predicate answerable from the session record —
        ``owner``/``archived``/``pinned``/``projects`` — is applied by the store
        before it decides which candidates to page, and a session the query
        rejects is never opened. Filtering afterwards instead would be worse
        than merely slower once ``limit`` exists: paging first and filtering the
        page would make ``pinned=True`` return only the pinned sessions inside
        the newest N candidates, which is usually none of them.

        **The fold itself is unchanged and still single-sourced.**
        ``summarize_session`` sees a smaller event list, not a different
        derivation — re-deriving a row's fields with a store-side aggregation
        would be a second implementation, free to drift until a row disagrees
        with the session it names.

        **``limit``/``offset`` bound how many CANDIDATES this call examines, not
        how many rows it is guaranteed to return.** They page
        ``list_session_digests`` after *query* has narrowed the candidate set but
        before the two VISIBILITY rules below run, which is what lets a
        Mongo-backed store scope its expensive read to the page instead of the
        whole collection (see that method's docstring for the measured saving) —
        but a candidate dropped below (no visible event and not running, or no
        ``created_at``) shrinks the page rather than being backfilled from the
        next one. Those two rules stay here because they are the only ones that
        need the transcript's EVENTS, so no store query can decide them; they
        are hygiene rather than user filters, which is why shrinkage is
        acceptable for them and would not have been for ``pinned``.
        ``limit=None`` (the default) is an unpaginated call.

        Ordering is **pinned first, then newest first.** Two stable sorts
        rather than one composite key: the second pass only has to move pinned
        rows to the front, and stability preserves the recency order the first
        pass established within each group. Pinning is applied HERE, as an
        ordering over what the query already admitted — never as a way around
        it. A caller that hides an origin keeps hiding it when the row is
        pinned, because filtering a sorted list preserves relative order. That
        is what makes "a mobile surface shows only its own pinned sessions"
        true by construction, with no surface-specific branch anywhere in this
        method.
        """
        # Resolved HERE rather than left to the store: an omitted query means
        # "the default listing" (archived hidden), while the store's own
        # ``None`` means the unnarrowed read a self-filtering caller wants.
        query = query or SessionQuery()
        summaries: list[dict[str, object]] = []
        digests = self._session_store.list_session_digests(
            query, limit=limit, offset=offset
        )
        records = self._session_store.load_session_records(
            [digest.session_id for digest in digests]
        )
        for digest in digests:
            session_id = digest.session_id
            events = digest.events
            record = records[session_id]
            summary = self.summarize_session(session_id, events=events, record=record)
            # Evaluated over the DIGEST, and that is exact rather than merely
            # close: a session with a visible event has a ``user`` event, which
            # the digest always carries, so this can only read False for a
            # session the next check drops anyway (no user turn and not running
            # ⇒ ``created_at`` is None). The two filters are kept separate
            # regardless — the digest's contents are a store concern, and a
            # listing rule that silently depended on them would be one
            # projection change away from dropping rows.
            has_visible_event = any(
                event.get("type") not in {"session", "context"} for event in events
            )
            if not has_visible_event and not summary.get("running"):
                continue
            if summary.get("created_at") is None and not summary.get("running"):
                continue
            summaries.append(summary)
        summaries.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)
        summaries.sort(key=lambda s: str(s.get("pinned_at") or ""), reverse=True)
        return summaries

    def set_session_pinned(self, session_id: str, pinned: bool) -> str | None:
        """Pin or unpin a session, returning the resulting ``pinned_at`` stamp.

        Lives on the runtime for the same reason archive/rename/fork do: it is
        the ONE place a session's record is mutated, so every surface reaches the
        store through it rather than around it.
        """
        self._session_store.set_pinned(session_id, pinned)
        return self._session_store.get_pinned_at(session_id)

    def load_events(self, session_id: str, after: str | None = None) -> list[EventRecord]:
        """Load a session's events, narrowed to those newer than *after*.

        ``O(matched events)`` on a driver that can range-read, ``O(one session's
        events)`` on one that cannot — the store decides, which is the point.
        Materialising the whole transcript and filtering it in Python would
        make ``after`` narrow the RESPONSE while the read stayed the record's
        entire history: a cursor in name only. Measure the TIME, not the
        payload — a shrinking response hides constant work.

        An unparseable *after* returns everything, unchanged: an in-process
        caller has no channel to be refused on, and most of them pass no cursor
        at all. That widening is a DEFENSIVE default and nothing may rely on it
        — the refusal belongs to the surface that accepted the value from a
        client, and the ``/events`` route 400s before reaching here.
        """
        cursor = EventCursor.parse(after) if after else None
        return self._session_store.load_events_after(session_id, cursor)

    def start_async(
        self,
        *,
        session_id: str,
        user_query: str,
        model_name: str | None = None,
        fallback_models: tuple[str, ...] | None = None,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        tool_registry=None,
        permission_policy=None,
        approval_callback=None,
        hook_manager=None,
        mode: str | None = None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        cwd: str | None = None,
        session_step_budget: int = 0,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        project_autoselect: bool = False,
        attachments: list[dict] | None = None,
        session_mcp_servers: dict[str, dict] | None = None,
    ) -> str:
        """Start an asynchronous orchestration run for the session.

        ``capability_mode`` is the ROOT delegation-privilege ceiling (default
        ``"all"`` — no filtering). A caller that resolved a
        principal's role into a narrower tier (``read_only`` for a viewer)
        passes it here so the session's own tools — not only spawned children —
        are capped; it travels unchanged into ``AgentContext.root`` alongside
        ``workspace_mode``.

        ``fallback_models`` (when provided) opts this run into cross-model
        fallback; ``None`` defers to the resolved config policy.

        Returns a storeless per-run handle ``run_id`` of the form
        ``"<session_id>:r<seq>"`` where *seq* is the 1-based count of runs
        started on this session so far (so one session can host many runs).
        The run_id is resolvable back to the session id by splitting on the
        first ``:`` — no new store or index is required. When the run
        registry refuses the start (a run is already active for this
        session), an empty string is returned so existing ``if not started``
        callers still detect the refusal.

        Accepting a turn PERSISTS it: the ``user`` event is written here, before
        the run is handed off, so a caller that got a run_id can rely on the
        transcript already holding the turn. See the block below for why the
        executor's own append is suppressed rather than deduplicated.

        When the run ends without meeting an explicit goal, :class:`GoalRetryGate`
        re-drives the session ONCE from the post-release seam below. That retry
        replays THIS call's arguments unchanged, so it inherits the same tool
        registry, hooks, capability ceiling and workspace the operator's run had.
        """
        # Snapshot the accepted arguments BEFORE any local is bound — at this
        # point ``locals()`` is exactly this call's parameters. A one-shot goal
        # retry has to replay all of them, and re-listing thirty names here
        # would be a second home for the same contract, drifting silently the
        # day a parameter is added.
        run_arguments: dict[str, Any] = dict(locals())
        run_arguments.pop("self", None)
        run_id = self._mint_run_id(session_id)
        # A caller-supplied invocation_id wins; otherwise seed the Langfuse
        # trace id from the run id we just minted, so each run gets its own
        # trace instead of every run of a session collapsing onto one. The
        # snapshot above already captured the unresolved (possibly None)
        # value, so a goal-retry replay mints its OWN distinct run/trace id
        # rather than inheriting this one.
        if invocation_id is None:
            invocation_id = run_id
        msg_queue: queue.Queue[str] = queue.Queue()
        interrupt_event = threading.Event()

        # Emit an instant ``run_accepted`` lifecycle marker BEFORE the background
        # run begins its heavy synchronous setup (``Orchestrator.__init__``
        # → tool-registry build + project-instruction discovery, which precede the
        # first run-phase ``append_event``). Without it the session page sits blank
        # for that whole window; with it the FE renders the session shell + a
        # "starting…" state immediately. It rides the same
        # ``append_event`` → ``SessionEventBus`` → SSE choke-point as every other
        # event, so no new transport is needed — additive only. Skipped when a run
        # is already active (the registry would refuse the start) so we never emit
        # a marker for a run that does not begin. Shared by both backends below.
        #
        # THE ACCEPTANCE SEAM PERSISTS WHAT IT ACCEPTED. The ``user`` event — the
        # record of the text the operator typed — is written HERE, adjacent to the
        # marker, not by the EXECUTOR: the orchestration body runs only after that
        # same heavy setup, so a turn appended there is invisible for the whole
        # cold-start window and every client renders an empty session. Writing it
        # at acceptance is what makes a 202 mean the turn is durable.
        # ``user_turn_persisted`` below then tells the body to skip its own
        # append, so the turn is written exactly once.
        #
        # The flag is the RESULT of the guard, never a hardcoded ``True``: this
        # pre-check is not atomic with the registry's own locked accept, so an
        # active run that finishes in between lets the start succeed after we
        # skipped the write. Deriving the flag means the body writes the turn in
        # exactly that case instead of the turn being lost outright. The opposite
        # skew — we wrote, then the registry refused — costs a turn record for a
        # run that never began, which is the same benign shape the marker has
        # always had, and strictly better than dropping a real turn.
        user_turn_persisted = not self._run_registry.is_running(session_id)
        if user_turn_persisted:
            self.append_event(
                session_id,
                {
                    "type": "run_accepted",
                    "payload": {
                        "session_id": session_id,
                        "run_id": run_id,
                        "ts": _utc_now(),
                    },
                },
            )
            self._session_store.append_user_turn(session_id, user_query, attachments)

        def _relaunch(query: str) -> str:
            # Attachments are deliberately dropped: they were persisted onto the
            # turn this session already holds, and replaying them would attach
            # the same files to a second turn. Everything else is replayed as-is.
            return self.start_async(
                **{**run_arguments, "user_query": query, "attachments": None}
            )

        def _on_release() -> None:
            self._goal_retry_gate.maybe_retry(session_id, _relaunch)

        # Backend selection: ONLY a single-threaded async host (Pyodide's
        # WebLoop, ``sys.platform == "emscripten"``) with an already-running
        # event loop drives the orchestration as an asyncio task via
        # ``orchestrate_session_async`` — no daemon thread, no nested
        # ``asyncio.run``. Gating on the platform (not merely "is a loop
        # running") is deliberate: CPython always takes the daemon-thread path
        # below even when called from within a running loop (e.g. a future
        # async CPython caller), so blocking orchestration never runs on that
        # caller's own loop. Cancellation is surfaced through the same
        # ``RunRegistry`` cancel_event either way.
        running_loop: asyncio.AbstractEventLoop | None = None
        if sys.platform == "emscripten":
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

        if running_loop is not None:
            cancel_event = threading.Event()
            if not self._run_registry.register_loop_run(
                session_id,
                cancel_event=cancel_event,
                message_queue=msg_queue,
                interrupt_step=interrupt_event,
            ):
                return ""
            coro = orchestrate_session_async(
                user_query=user_query,
                model_name=model_name,
                fallback_models=fallback_models,
                max_iters=max_iters,
                initial_plan=initial_plan,
                session_id=session_id,
                session_store=self._session_store,
                tool_registry=tool_registry,
                permission_policy=permission_policy,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                mode=mode,
                should_cancel=cancel_event.is_set,
                allowed_tools=allowed_tools,
                denied_tools=denied_tools,
                strict_tool_scope=strict_tool_scope,
                capability_mode=capability_mode,
                skill_instructions=skill_instructions,
                message_queue=msg_queue,
                interrupt_step=interrupt_event,
                cwd=cwd,
                session_step_budget=session_step_budget,
                user_id=user_id,
                source_platform=source_platform,
                invocation_id=invocation_id,
                extra_session_tools=extra_session_tools,
                enable_skills=enable_skills,
                project_autoselect=project_autoselect,
                attachments=attachments,
                user_turn_persisted=user_turn_persisted,
                session_mcp_servers=session_mcp_servers,
            )
            task = running_loop.create_task(coro)
            # Hold a strong reference on the registry-owned handle so asyncio
            # can't GC this fire-and-forget task mid-run (mirrors
            # AgentHandle.asyncio_task / SpawnAgentTool._lifecycle_tasks).
            self._run_registry.attach_loop_task(session_id, task)
            task.add_done_callback(
                lambda t: self._on_loop_run_done(session_id, t, _on_release)
            )
            return run_id

        def _run(cancel_event: threading.Event) -> None:
            self.run_sync(
                user_query=user_query,
                session_id=session_id,
                model_name=model_name,
                fallback_models=fallback_models,
                max_iters=max_iters,
                initial_plan=initial_plan,
                tool_registry=tool_registry,
                permission_policy=permission_policy,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                mode=mode,
                should_cancel=cancel_event.is_set,
                allowed_tools=allowed_tools,
                denied_tools=denied_tools,
                strict_tool_scope=strict_tool_scope,
                capability_mode=capability_mode,
                skill_instructions=skill_instructions,
                message_queue=msg_queue,
                interrupt_step=interrupt_event,
                cwd=cwd,
                session_step_budget=session_step_budget,
                user_id=user_id,
                source_platform=source_platform,
                invocation_id=invocation_id,
                extra_session_tools=extra_session_tools,
                enable_skills=enable_skills,
                project_autoselect=project_autoselect,
                attachments=attachments,
                user_turn_persisted=user_turn_persisted,
                session_mcp_servers=session_mcp_servers,
            )

        started = self._run_registry.start(
            session_id,
            target=_run,
            message_queue=msg_queue,
            interrupt_step=interrupt_event,
            on_release=_on_release,
        )
        return run_id if started else ""

    def _on_loop_run_done(
        self,
        session_id: str,
        task: asyncio.Task,
        on_release: Callable[[], None] | None = None,
    ) -> None:
        """Done-callback for a loop-backed run: finalize + surface exceptions.

        *on_release* is the loop-backed twin of the thread path's post-release
        callback and fires for the same reason and in the same order — after
        ``finalize_loop_run`` has dropped the handle, so the session's slot is
        free for a follow-up run.

        ``Orchestrator``/``orchestrate_session_async`` already catch and log
        everything that happens INSIDE the orchestration body, so an
        exception surfacing here means something failed BEFORE that
        try/except (task construction, an unawaited-setup bug). Without
        reading it here, asyncio only emits a bare "Task exception was never
        retrieved" warning with no session context — read + log it via the
        module logger instead.
        """
        self._run_registry.finalize_loop_run(session_id)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logging.error(
                "Loop-backed orchestration run failed for session {}: {}: {}",
                session_id,
                type(exc).__name__,
                exc,
            )

    def _mint_run_id(self, session_id: str) -> str:
        """Mint ``"<session_id>:r<seq>"`` for a run about to start.

        *seq* is 1-based and counts this run: it is one more than the number
        of ``user`` events already in the transcript (each prior run appended
        exactly one, and runs are serialized — the registry refuses a
        concurrent start). A fresh session has zero prior user events → ``r1``.
        Storeless: derived from the transcript, no separate counter to persist.
        """
        try:
            events = self._session_store.load_transcript(session_id)
            prior_runs = sum(1 for e in events if e.get("type") == "user")
        except Exception:  # pragma: no cover - defensive; never block a start
            prior_runs = 0
        return f"{session_id}:r{prior_runs + 1}"

    def run_sync(
        self,
        *,
        user_query: str,
        session_id: str,
        model_name: str | None = None,
        fallback_models: tuple[str, ...] | None = None,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        tool_registry=None,
        permission_policy=None,
        approval_callback=None,
        hook_manager=None,
        mode: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        cwd: str | None = None,
        session_step_budget: int = 0,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        project_autoselect: bool = False,
        attachments: list[dict] | None = None,
        user_turn_persisted: bool = False,
        session_mcp_servers: dict[str, dict] | None = None,
    ) -> TaskQueue:
        """Run an orchestration request synchronously.

        ``enable_skills=False`` opts a headless product drive (search/wiki) out
        of auto-skill injection so the agent never burns its first step
        activating a host ``~/.claude`` skill (default ``True`` — CLI/channel
        behavior unchanged). ``capability_mode`` is the root delegation-privilege
        ceiling (default ``"all"`` — no filtering); see :meth:`start_async`.

        ``project_autoselect=True`` binds ``list_projects`` / ``switch_project``
        on the ROOT agent so the run chooses its own workspace; default
        ``False`` binds neither.

        ``user_turn_persisted=True`` says an upstream seam already wrote this
        turn's ``user`` event, so the orchestration body must not write a second
        one. Default ``False`` — a direct caller (the CLI turn engine, the
        structured runners) still owns the append.
        """
        return orchestrate_session(
            user_query=user_query,
            model_name=model_name,
            fallback_models=fallback_models,
            max_iters=max_iters,
            initial_plan=initial_plan,
            session_id=session_id,
            session_store=self._session_store,
            tool_registry=tool_registry,
            permission_policy=permission_policy,
            approval_callback=approval_callback,
            hook_manager=hook_manager,
            mode=mode,
            should_cancel=should_cancel,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            strict_tool_scope=strict_tool_scope,
            capability_mode=capability_mode,
            skill_instructions=skill_instructions,
            message_queue=message_queue,
            interrupt_step=interrupt_step,
            cwd=cwd,
            session_step_budget=session_step_budget,
            user_id=user_id,
            source_platform=source_platform,
            invocation_id=invocation_id,
            extra_session_tools=extra_session_tools,
            enable_skills=enable_skills,
            project_autoselect=project_autoselect,
            attachments=attachments,
            user_turn_persisted=user_turn_persisted,
            session_mcp_servers=session_mcp_servers,
        )

    async def arun(
        self,
        *,
        user_query: str,
        session_id: str,
        model_name: str | None = None,
        fallback_models: tuple[str, ...] | None = None,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        tool_registry=None,
        permission_policy=None,
        approval_callback=None,
        hook_manager=None,
        mode: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        cwd: str | None = None,
        session_step_budget: int = 0,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        project_autoselect: bool = False,
        attachments: list[dict] | None = None,
        user_turn_persisted: bool = False,
        session_mcp_servers: dict[str, dict] | None = None,
    ) -> TaskQueue:
        """Run an orchestration request inside an existing event loop.

        Async counterpart of :meth:`run_sync`. Use this when the caller is
        itself a coroutine (e.g. an in-browser Flask handler running on
        Pyodide's WebLoop) so the orchestration can ``await`` without
        ``asyncio.run``. Same signature and semantics as :meth:`run_sync`.
        """
        return await orchestrate_session_async(
            user_query=user_query,
            model_name=model_name,
            fallback_models=fallback_models,
            max_iters=max_iters,
            initial_plan=initial_plan,
            session_id=session_id,
            session_store=self._session_store,
            tool_registry=tool_registry,
            permission_policy=permission_policy,
            approval_callback=approval_callback,
            hook_manager=hook_manager,
            mode=mode,
            should_cancel=should_cancel,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            strict_tool_scope=strict_tool_scope,
            capability_mode=capability_mode,
            skill_instructions=skill_instructions,
            message_queue=message_queue,
            interrupt_step=interrupt_step,
            cwd=cwd,
            session_step_budget=session_step_budget,
            user_id=user_id,
            source_platform=source_platform,
            invocation_id=invocation_id,
            extra_session_tools=extra_session_tools,
            enable_skills=enable_skills,
            project_autoselect=project_autoselect,
            attachments=attachments,
            user_turn_persisted=user_turn_persisted,
            session_mcp_servers=session_mcp_servers,
        )

    def cancel(self, session_id: str) -> bool:
        """Cancel an active run if present."""
        return self._run_registry.cancel(session_id)

    def is_running(self, session_id: str) -> bool:
        """Return True if session has an active run."""
        return self._run_registry.is_running(session_id)

    def active_run_handle(self, session_id: str) -> RunHandle | None:
        """The live :class:`RunHandle` for *session_id*, if a run is active.

        Read-only steering-signal access for in-run collaborators — the
        ask-user question dispatcher polls the handle's ``cancel_event`` /
        ``interrupt_step`` / ``message_queue`` while blocked so a steer or
        interrupt supersedes a pending question instead of deadlocking behind
        it. Callers must treat the handle as read-only.
        """
        return self._run_registry.get_handle(session_id)

    def is_terminated(self, session_id: str) -> bool:
        """Return True iff the session was permanently terminated.

        The ONE choke point every entry-point guard reads — a thin read-through
        to the store so there is zero duplicated status derivation. An unknown
        session reads ``False`` (no stamp).
        """
        return self._session_store.is_terminated(session_id)

    def register_on_terminate(self, callback: Callable[[str], int | None]) -> None:
        """Register a callback fired once when a session is terminated.

        The Wave-2 cascade-cancel seam: the callback receives the terminated
        ``session_id`` and MAY return an int count of downstream artifacts it
        cancelled (e.g. scheduled triggers), which :meth:`terminate_session`
        sums into ``cancelled_triggers``. Best-effort — a raising callback is
        logged and skipped, never blocking termination.
        """
        self._on_terminate.append(callback)

    def terminate_session(self, session_id: str) -> dict[str, object]:
        """Permanently terminate a session — idempotent and irreversible.

        First call: stamps ``terminated_at``, cancels any live run (cooperative,
        via the run registry's cancel event), fires the ``on_terminate``
        callbacks (summing any returned cancelled-artifact counts), and appends
        a ``session_terminated`` transcript event — which rides the standard
        ``append_event`` → ``SessionEventBus`` → SSE choke-point, so a live
        stream observes the termination with no new transport. A repeat call is
        a no-op that returns the SAME shape with the ORIGINAL ``terminated_at``
        and ``cancelled_triggers: 0`` (the side effects never re-fire).

        Side effects fire exactly once, arbitrated by the store's set-once
        write: ``SessionStoreBase.terminate_session`` returns ``True`` only to
        the call that newly stamped the timestamp, so two concurrent FIRST
        calls (Flask is threaded even at ``--workers 1``) can't both pass an
        unlocked read and duplicate the event + callback fan-out — exactly one
        of them wins the race and runs the block below.

        Callers guard unknown-session (404) upstream; this assumes the session
        exists. Returns ``{session_id, status:"terminated", terminated_at,
        cancelled_triggers}``.
        """
        newly_terminated = self._session_store.terminate_session(session_id)
        terminated_at = self._session_store.get_terminated_at(session_id)
        if not newly_terminated:
            return {
                "session_id": session_id,
                "status": "terminated",
                "terminated_at": terminated_at,
                "cancelled_triggers": 0,
            }
        # Cancel any in-flight run so the terminated session stops working; the
        # cancel event unwinds the loop cooperatively (best-effort, no-op if idle).
        self._run_registry.cancel(session_id)
        cancelled_triggers = 0
        for callback in self._on_terminate:
            try:
                result = callback(session_id)
            except Exception:
                logging.warning(
                    "on_terminate callback failed for session {}", session_id, exc_info=True
                )
                continue
            if isinstance(result, int):
                cancelled_triggers += result
        # append_terminal_event, NOT append_event: the store already cached
        # this session as terminated (line above), so a guarded append would
        # drop the tombstone event it is itself trying to write.
        self._session_store.append_terminal_event(
            session_id,
            {
                "type": "session_terminated",
                "payload": {
                    "session_id": session_id,
                    "terminated_at": terminated_at,
                    "cancelled_triggers": cancelled_triggers,
                },
            },
        )
        return {
            "session_id": session_id,
            "status": "terminated",
            "terminated_at": terminated_at,
            "cancelled_triggers": cancelled_triggers,
        }

    def start_command(
        self,
        session_id: str,
        target: Callable[[threading.Event], None],
    ) -> bool:
        """Start a non-orchestration background run for a slash command.

        Reuses the same RunRegistry as ``start_async`` so ``is_running()``
        and the events-polling pipeline treat command runs identically to
        query runs. The FE drives all in-flight UI off the same
        authoritative server state — no browser-side patching required.
        """
        return self._run_registry.start(session_id, target=target)

    def resolve_recovery_query(
        self,
        session_id: str,
        action: RecoveryAction,
        *,
        from_ts: str | None = None,
        replacement_text: str | None = None,
        trigger: RecoveryTrigger | None = None,
    ) -> str:
        """Resolve the user query text for a retry/continue recovery action.

        Appends a ``recovery`` audit event to the transcript and returns the
        query text the caller should pass to :meth:`start_async`. The
        orchestrator automatically picks up prior events via
        :class:`ContextBuilder`, so the caller does not need to trim the
        transcript.

        *replacement_text* substitutes the query this method would otherwise
        build: for ``retry`` the original user message (enabling "edit and
        regenerate"), for ``continue`` the generic resume prompt. An automatic
        re-drive supplies its own deterministic prompt that way rather than
        appending a second turn of its own.

        *trigger* names a NON-HUMAN originator on the ``recovery`` marker (see
        :data:`RecoveryTrigger`); omitted for every operator-driven recovery.
        It is what an automatic re-drive reads back to know it has already
        fired.

        Raises :class:`ValueError` when ``action`` is unrecognised, there is
        no prior user message to recover from, or (for ``retry``) the last
        user message is empty.
        Raises :class:`RuntimeError` if a run is already active for the
        session — cancel it first.
        """
        if action not in ("retry", "continue"):
            raise ValueError(f"unknown recovery action: {action!r}; expected 'retry' or 'continue'")
        if self.is_running(session_id):
            raise RuntimeError(f"session {session_id} is running; cancel before recovering")
        events = self._session_store.load_transcript(session_id)

        # Find the target user event.  When *from_ts* is given (only
        # meaningful for "retry"), locate the user event at that exact
        # timestamp so the caller can retry from any turn — not just the
        # last one.  Otherwise fall back to the most recent user event.
        if from_ts and action == "retry":
            last_user = next(
                (e for e in events if e.get("type") == "user" and e.get("ts") == from_ts),
                None,
            )
            if last_user is None:
                raise ValueError(f"no user event at ts={from_ts!r}")
        else:
            last_user = next(
                (e for e in reversed(events) if e.get("type") == "user"),
                None,
            )
        if last_user is None:
            raise ValueError(
                "no prior user message to recover from — start with a fresh query instead"
            )
        user_payload = last_user.get("payload") or {}
        original_text = user_payload.get("text", "") if isinstance(user_payload, dict) else ""

        if action == "retry":
            # ----------------------------------------------------------
            # Retry = time-travel: delete the failed turn so the session
            # looks like it ended right before that user message was
            # sent. ``Orchestrator.run`` re-appends the user event +
            # runs a fresh attempt. Prior successful turns stay intact.
            # ----------------------------------------------------------
            if not replacement_text and not original_text:
                raise ValueError("last user message has empty text; cannot retry")
            last_user_ts = last_user.get("ts", "")
            if last_user_ts:
                # Delete the user event itself + everything after it
                # (tool_results, completion, recovery events, …). Use
                # ``ts >= last_user_ts`` semantics by truncating after
                # the timestamp just BEFORE the user event.
                #
                # Find the event immediately before the last user.
                prev_ts = ""
                for ev in events:
                    if ev is last_user:
                        break
                    prev_ts = ev.get("ts", "")
                if prev_ts:
                    self._session_store.truncate_after(session_id, prev_ts)
                else:
                    # The user event is the first event — nuke everything
                    # by truncating after an impossibly-early timestamp.
                    self._session_store.truncate_after(session_id, "0000-00-00T00:00:00+00:00")
            query_text = replacement_text or original_text
        else:
            # ----------------------------------------------------------
            # Continue = stitch: keep the failed run's traces, delete
            # only a STALE PRIOR continue attempt, then start a new
            # continuation turn.
            #
            # A prior continue leaves a ``recovery`` audit marker + the
            # synthetic turn it drove; re-continuing drops that stale
            # attempt so the transcript doesn't accrete duplicate recovery
            # prompts. The cut anchors on the LAST ``recovery`` marker —
            # NEVER the last ``completion``. A run killed mid-flight
            # (process restart) emits neither a completion nor a recovery
            # marker for its turn, so a completion anchor falls back to the
            # PREVIOUS completed turn and deletes the entire interrupted
            # turn: its user message and every tool call/result. No prior
            # recovery marker ⇒ nothing stale ⇒ preserve all traces (the
            # interrupted turn stays an open turn the continuation resumes
            # from).
            #
            # The marker alone is NOT licence to cut. It records that a
            # continue was once triggered — never that it was the last
            # thing to happen. An attempt that went on to produce work
            # leaves that work AFTER the marker, so cutting at the marker
            # deletes it outright. A marker is stale only when its attempt
            # produced NOTHING DURABLE: no ``assistant`` message, no
            # ``completion`` and no ``tool_result``. Only a bare synthetic
            # re-prompt that the model never answered is safe to drop.
            #
            # ``assistant`` counts as durable even with no ``completion``
            # behind it. ``Orchestrator.run`` appends the assistant event
            # and the completion as SEPARATE writes with a title-generation
            # call between them, so a run that dies in that window leaves a
            # real answer the user already read on screen — the same window
            # the startup sweep closes with a synthetic completion.
            # Anything the user could have read must never be deleted.
            #
            # Staleness is a PREDICATE, not an anchor: the cut still lands
            # on the recovery marker.
            #
            # Both scans key off APPEND ORDER rather than a ts comparison.
            # The transcript is an append-only log, so position is its
            # ground truth, whereas ISO strings sort chronologically only
            # while every producer spells the UTC offset identically —
            # ``Z`` sorts above ``+00:00``, so a single same-second mix is
            # enough to widen the cut past the marker.
            # ----------------------------------------------------------
            last_recovery_idx = -1
            for idx, ev in enumerate(events):
                if ev.get("type") == "recovery":
                    last_recovery_idx = idx
            # A marker at index 0 leaves no earlier event to anchor the
            # cut on, so it is likewise treated as nothing to drop.
            if last_recovery_idx > 0 and not any(
                ev.get("type") in {"assistant", "completion", "tool_result"}
                for ev in events[last_recovery_idx + 1 :]
            ):
                # Truncate just BEFORE the stale recovery marker so its
                # own synthetic continue-turn is removed but the real
                # work preceding it is kept.
                prev_ts = events[last_recovery_idx - 1].get("ts", "")
                if prev_ts:
                    self._session_store.truncate_after(session_id, prev_ts)
            query_text = replacement_text or get_prompt_registry().render(
                "loop.recovery_continue"
            )
            # Audit marker so the transcript records when a continue was
            # triggered. Not appended for retry (the failed turn is deleted
            # entirely — no trace left to annotate), which is also why an
            # automatic re-drive must use ``continue``: this marker is its
            # one-shot ledger, and retry would delete it.
            payload: dict[str, object] = {"action": action}
            if trigger is not None:
                payload["trigger"] = trigger
            self._session_store.append_event(
                session_id,
                {"type": "recovery", "payload": payload},
            )

        return query_text

    def reinject_recovery_context(self, session_id: str) -> None:
        """Re-emit capability-gating fields so a recovered run keeps them.

        The orchestrator reads the *most-recent* ``context`` event when it
        builds the system prompt and resolves capability-gated AgentDefs.
        After a recovery turn the latest context event may be a plain
        ``mode``/``model`` update that does NOT carry the original
        ``client_capabilities`` / ``structured_workspace`` — so a recovered
        wiki/QA/structured session would silently lose its capability and
        ``spawn_agent`` lookups for the gated AgentDefs would fail.

        This scans the transcript for the latest value of each gating key
        (preserving exactly what the session already had — no origin→capability
        map) and appends a single fresh ``context`` event carrying them, so the
        most-recent context event after recovery still gates correctly. A no-op
        when the session never declared any gating field.

        Shared by BOTH the API recover endpoint and the CLI recovery command —
        the single source of truth for recovery capability re-injection.
        """
        events = self._session_store.load_transcript(session_id)
        gating: dict[str, object] = {}
        for event in events:
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            for key in _RECOVERY_GATING_KEYS:
                if key in payload:
                    gating[key] = payload[key]
        if gating:
            self.append_context_event(session_id, gating)

    def enqueue_message(self, session_id: str, text: str) -> bool:
        """Enqueue a steering message for the root agent of a running session.

        The message is also persisted as a ``"user"`` event so it appears in
        the session transcript (console timeline, CLI history, Langfuse).

        Returns False if no active run or no message queue.
        """
        handle = self._run_registry.get_handle(session_id)
        if handle and handle.message_queue is not None:
            handle.message_queue.put_nowait(text)
            self._session_store.append_event(
                session_id, {"type": "user_steer", "payload": {"text": text}}
            )
            return True
        return False

    def interrupt_step(self, session_id: str) -> bool:
        """Interrupt the current tool execution step.

        The loop continues after the interrupted step with error results.
        Returns False if no active run or no interrupt event.
        """
        handle = self._run_registry.get_handle(session_id)
        if handle and handle.interrupt_step is not None:
            handle.interrupt_step.set()
            self._session_store.append_event(
                session_id,
                {"type": "user_steer", "payload": {"text": "[Interrupted by user]"}},
            )
            return True
        return False

    def _has_pending_plan_proposal(self, session_id: str) -> tuple[bool, int, str]:
        """Check if the session has an unresolved plan_proposed event.

        Returns ``(has_pending, revision, plan_path)`` where *revision* is the
        latest unresolved ``plan_proposed`` revision number, or 0 if none.
        """
        events = self._session_store.load_transcript(session_id)
        proposed_revisions: set[int] = set()
        resolved_revisions: set[int] = set()
        plan_path = ""
        for event in events:
            etype = event.get("type")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            rev = payload.get("revision", 0)
            if etype == "plan_proposed":
                proposed_revisions.add(rev)
                plan_path = payload.get("plan_path", "")
            elif etype in ("plan_approved", "plan_rejected"):
                resolved_revisions.add(rev)
        pending = proposed_revisions - resolved_revisions
        if pending:
            return True, max(pending), plan_path
        return False, 0, ""

    def approve_plan(self, session_id: str) -> bool:
        """Approve a pending plan proposal episodically.

        Emits a ``plan_approved`` event to the transcript. Does NOT start
        a new run — the caller (API endpoint) is responsible for starting
        the act-mode run via ``start_async``.

        Returns False if no pending plan proposal exists or a run is
        already active.
        """
        if self.is_running(session_id):
            return False
        has_pending, revision, plan_path = self._has_pending_plan_proposal(session_id)
        if not has_pending:
            return False
        self._session_store.append_event(
            session_id,
            {
                "type": "plan_approved",
                "payload": {"plan_path": plan_path, "revision": revision},
            },
        )
        # Signal mode transition so all clients pick up the change.
        self._session_store.append_event(
            session_id,
            {"type": "context", "payload": {"mode": "act"}},
        )
        return True

    def reject_plan(self, session_id: str) -> bool:
        """Reject a pending plan proposal.

        Emits a ``plan_rejected`` event. The session stays dormant —
        the user can type refinement guidance as a new message, which
        starts a fresh plan-mode run.

        Returns False if no pending plan proposal exists or a run is
        already active.
        """
        if self.is_running(session_id):
            return False
        has_pending, revision, plan_path = self._has_pending_plan_proposal(session_id)
        if not has_pending:
            return False
        self._session_store.append_event(
            session_id,
            {
                "type": "plan_rejected",
                "payload": {"plan_path": plan_path, "revision": revision},
            },
        )
        return True
