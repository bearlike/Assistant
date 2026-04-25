#!/usr/bin/env python3
"""Shared session runtime utilities for CLI and API."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from truss_core.classes import Plan, TaskQueue
from truss_core.session_store import SessionStoreBase, create_session_store
from truss_core.task_master import orchestrate_session
from truss_core.types import EventRecord


def _derive_core_commands() -> set[str]:
    """Build CORE_COMMANDS from the command registry.

    Transcript-render commands route through the orchestrator's marker path.
    Legacy markers ``/terminate`` and ``/status`` are not yet promoted to the
    registry but stay recognized as core commands.
    """
    from truss_core.commands import COMMANDS, CommandRender

    transcript = {
        f"/{name}"
        for name, cmd in COMMANDS.items()
        if cmd.render is CommandRender.TRANSCRIPT
    }
    return transcript | {"/terminate", "/status"}


CORE_COMMANDS = _derive_core_commands()

RecoveryAction = Literal["retry", "continue"]


def _build_continue_recovery_query() -> str:
    """Build the ``continue`` recovery prompt.

    Terse by design. The original task and prior work are already in the
    system prompt via ``ContextBuilder.recent_events`` (which anchors the
    first user event) and the compaction summary when present. Keeping
    this HumanMessage small preserves the cache prefix for prompt caching.
    """
    return (
        "You were previously interrupted by an error. "
        "Review the conversation context and continue from where "
        "you left off without repeating completed work."
    )


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_core_command(text: str) -> str | None:
    """Return the core command token if present."""
    if not text:
        return None
    command = text.strip().lower().split()[0]
    return command if command in CORE_COMMANDS else None


@dataclass(frozen=True)
class RunHandle:
    """Active orchestration thread tracking."""

    thread: threading.Thread
    cancel_event: threading.Event
    started_at: str
    message_queue: queue.Queue[str] | None = field(default=None)
    interrupt_step: threading.Event | None = field(default=None)


class RunRegistry:
    """Track active orchestration threads per session."""

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
    ) -> bool:
        """Start a new run for the session if one is not already active."""
        with self._lock:
            existing = self._runs.get(session_id)
            if existing and existing.thread.is_alive():
                return False
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._wrap_run,
                args=(session_id, cancel_event, target),
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

    def _wrap_run(
        self,
        session_id: str,
        cancel_event: threading.Event,
        target: Callable[[threading.Event], None],
    ) -> None:
        try:
            target(cancel_event)
        finally:
            with self._lock:
                handle = self._runs.get(session_id)
                if handle and handle.thread.ident == threading.current_thread().ident:
                    self._runs.pop(session_id, None)

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
            return bool(handle and handle.thread.is_alive())

    def get_cancel_event(self, session_id: str) -> threading.Event | None:
        """Return the cancel event for a session, if present."""
        with self._lock:
            handle = self._runs.get(session_id)
            return handle.cancel_event if handle else None

    def get_handle(self, session_id: str) -> RunHandle | None:
        """Return the run handle for a session, if present."""
        with self._lock:
            return self._runs.get(session_id)


def _filter_events(events: list[EventRecord], after_ts: str | None) -> list[EventRecord]:
    if not after_ts:
        return events
    cutoff = _parse_iso(after_ts)
    if cutoff is None:
        return events
    filtered: list[EventRecord] = []
    for event in events:
        ts = _parse_iso(event.get("ts"))
        if ts and ts > cutoff:
            filtered.append(event)
    return filtered


class SessionRuntime:
    """Shared orchestration runtime surface for CLI and API."""

    def __init__(
        self,
        *,
        session_store: SessionStoreBase | None = None,
        run_registry: RunRegistry | None = None,
    ) -> None:
        """Initialize the runtime with session storage and optional run registry."""
        self._session_store = session_store or create_session_store()
        self._run_registry = run_registry or RunRegistry()

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
    ) -> str:
        """Resolve session identifiers, tags, and forks to a session id.

        When *fork_at_ts* is provided alongside *fork_from*, only events up to
        (and including) that timestamp are copied into the new session.
        """
        if fork_from:
            source_session_id = self._session_store.resolve_tag(fork_from) or fork_from
            if fork_at_ts:
                session_id = self._session_store.fork_session_at(source_session_id, fork_at_ts)
            else:
                session_id = self._session_store.fork_session(source_session_id)
        if session_tag and not session_id:
            resolved = self._session_store.resolve_tag(session_tag)
            session_id = resolved if resolved else None
        if not session_id:
            session_id = self._session_store.create_session()
        if session_tag:
            self._session_store.tag_session(session_id, session_tag)
        assert session_id is not None
        return session_id

    def append_context_event(self, session_id: str, context: dict[str, object]) -> None:
        """Append a context event to the session transcript."""
        if not context:
            return
        self._session_store.append_event(session_id, {"type": "context", "payload": context})

    def summarize_session(
        self,
        session_id: str,
        *,
        events: list[EventRecord] | None = None,
    ) -> dict[str, object]:
        """Return a summarized view of a session."""
        if events is None:
            events = self._session_store.load_transcript(session_id)
        created_at = events[0]["ts"] if events else None
        stored_title = self._session_store.load_title(session_id)
        title = stored_title
        status = "idle"
        done_reason = None
        context: dict[str, object] | None = None
        has_user_event = False
        for event in events:
            if event.get("type") == "context":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    context = {**(context or {}), **payload}
            if event.get("type") == "user":
                has_user_event = True
                if title is None:
                    payload = event.get("payload", {})
                    if isinstance(payload, dict):
                        raw = payload.get("text")
                        if isinstance(raw, str):
                            title = raw[:120]
            if event.get("type") == "completion":
                payload = event.get("payload", {})
                if isinstance(payload, dict):
                    done_reason = payload.get("done_reason")
                    status = "completed" if payload.get("done") else "incomplete"
                    if done_reason == "canceled":
                        status = "canceled"
                    elif done_reason == "error":
                        status = "failed"
                    elif done_reason == "max_steps_reached":
                        status = "incomplete"
                    elif done_reason == "awaiting_approval":
                        status = "awaiting_approval"
                    elif isinstance(done_reason, str) and (
                        done_reason == "compact_failed"
                        or done_reason.startswith("command_failed:")
                    ):
                        # Slash-command failure path — keep parity with
                        # ``error`` so the FE StatusBadge renders the red
                        # "Failed" pill instead of the green "Completed" one.
                        status = "failed"
        running = self.is_running(session_id)
        if running:
            status = "running"
        if not has_user_event and not running:
            created_at = None
        if not title:
            title = f"Session {session_id[:8]}"
        return {
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "status": status,
            "done_reason": done_reason,
            "running": running,
            "context": context or {},
            "archived": self._session_store.is_archived(session_id),
        }

    def list_sessions(self, *, include_archived: bool = False) -> list[dict[str, object]]:
        """List sessions with summary metadata."""
        summaries: list[dict[str, object]] = []
        for session_id in self._session_store.list_sessions():
            events = self._session_store.load_transcript(session_id)
            summary = self.summarize_session(session_id, events=events)
            has_visible_event = any(
                event.get("type") not in {"session", "context"} for event in events
            )
            if not has_visible_event and not summary.get("running"):
                continue
            if summary.get("created_at") is None and not summary.get("running"):
                continue
            if not include_archived and summary.get("archived"):
                continue
            summaries.append(summary)
        summaries.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)
        return summaries

    def load_events(self, session_id: str, after: str | None = None) -> list[EventRecord]:
        """Load events for a session with optional timestamp filtering."""
        events = self._session_store.load_transcript(session_id)
        return _filter_events(events, after)

    def start_async(
        self,
        *,
        session_id: str,
        user_query: str,
        model_name: str | None = None,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        tool_registry=None,
        permission_policy=None,
        approval_callback=None,
        hook_manager=None,
        mode: str | None = None,
        allowed_tools: list[str] | None = None,
        skill_instructions: str | None = None,
        cwd: str | None = None,
        session_step_budget: int = 0,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
    ) -> bool:
        """Start an asynchronous orchestration run for the session."""
        msg_queue: queue.Queue[str] = queue.Queue()
        interrupt_event = threading.Event()

        def _run(cancel_event: threading.Event) -> None:
            self.run_sync(
                user_query=user_query,
                session_id=session_id,
                model_name=model_name,
                max_iters=max_iters,
                initial_plan=initial_plan,
                tool_registry=tool_registry,
                permission_policy=permission_policy,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                mode=mode,
                should_cancel=cancel_event.is_set,
                allowed_tools=allowed_tools,
                skill_instructions=skill_instructions,
                message_queue=msg_queue,
                interrupt_step=interrupt_event,
                cwd=cwd,
                session_step_budget=session_step_budget,
                user_id=user_id,
                source_platform=source_platform,
                invocation_id=invocation_id,
            )

        return self._run_registry.start(
            session_id,
            target=_run,
            message_queue=msg_queue,
            interrupt_step=interrupt_event,
        )

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
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        cwd: str | None = None,
        session_step_budget: int = 0,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
    ) -> TaskQueue:
        """Run an orchestration request synchronously."""
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
            skill_instructions=skill_instructions,
            message_queue=message_queue,
            interrupt_step=interrupt_step,
            cwd=cwd,
            session_step_budget=session_step_budget,
            user_id=user_id,
            source_platform=source_platform,
            invocation_id=invocation_id,
        )

    def cancel(self, session_id: str) -> bool:
        """Cancel an active run if present."""
        return self._run_registry.cancel(session_id)

    def is_running(self, session_id: str) -> bool:
        """Return True if session has an active run."""
        return self._run_registry.is_running(session_id)

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
    ) -> str:
        """Resolve the user query text for a retry/continue recovery action.

        Appends a ``recovery`` audit event to the transcript and returns the
        query text the caller should pass to :meth:`start_async`. The
        orchestrator automatically picks up prior events via
        :class:`ContextBuilder`, so the caller does not need to trim the
        transcript.

        When *replacement_text* is provided (only meaningful for ``retry``),
        the edited text is used instead of the original user message — enabling
        "edit and regenerate" workflows.

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
            # only prior recovery attempts (events after the LAST
            # completion), then start a new continuation turn.
            # ----------------------------------------------------------
            last_completion_ts = ""
            for ev in events:
                if ev.get("type") == "completion":
                    last_completion_ts = ev.get("ts", "")
            if last_completion_ts:
                self._session_store.truncate_after(session_id, last_completion_ts)
            query_text = _build_continue_recovery_query()
            # Audit marker so the transcript records when the user
            # triggered a continue. Not appended for retry (the failed
            # turn is deleted entirely — no trace left to annotate).
            self._session_store.append_event(
                session_id,
                {"type": "recovery", "payload": {"action": action}},
            )

        return query_text

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
