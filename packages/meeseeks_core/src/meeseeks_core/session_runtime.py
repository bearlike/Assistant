#!/usr/bin/env python3
"""Shared session runtime utilities for CLI and API."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from meeseeks_core.classes import TaskQueue
from meeseeks_core.session_store import SessionStore
from meeseeks_core.task_master import orchestrate_session
from meeseeks_core.types import EventRecord

CORE_COMMANDS = {"/compact", "/terminate", "/status"}


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
        session_store: SessionStore | None = None,
        run_registry: RunRegistry | None = None,
    ) -> None:
        """Initialize the runtime with session storage and optional run registry."""
        self._session_store = session_store or SessionStore()
        self._run_registry = run_registry or RunRegistry()

    @property
    def session_store(self) -> SessionStore:
        """Expose the underlying session store."""
        return self._session_store

    def resolve_session(
        self,
        *,
        session_id: str | None = None,
        session_tag: str | None = None,
        fork_from: str | None = None,
    ) -> str:
        """Resolve session identifiers, tags, and forks to a session id."""
        if fork_from:
            source_session_id = self._session_store.resolve_tag(fork_from) or fork_from
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

    def summarize_session(self, session_id: str) -> dict[str, object]:
        """Return a summarized view of a session."""
        events = self._session_store.load_transcript(session_id)
        created_at = events[0]["ts"] if events else None
        title = None
        status = "idle"
        done_reason = None
        context: dict[str, object] | None = None
        for event in events:
            if event.get("type") == "context":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    context = payload
            if title is None and event.get("type") == "user":
                payload = event.get("payload", {})
                if isinstance(payload, dict):
                    title = payload.get("text")
            if event.get("type") == "completion":
                payload = event.get("payload", {})
                if isinstance(payload, dict):
                    done_reason = payload.get("done_reason")
                    status = "completed" if payload.get("done") else "incomplete"
        running = self.is_running(session_id)
        if running:
            status = "running"
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
            summary = self.summarize_session(session_id)
            if summary.get("created_at") is None and not summary.get("running"):
                continue
            if not include_archived and summary.get("archived"):
                continue
            summaries.append(summary)
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
        initial_task_queue: TaskQueue | None = None,
        tool_registry=None,
        permission_policy=None,
        approval_callback=None,
        hook_manager=None,
        mode: str | None = None,
    ) -> bool:
        """Start an asynchronous orchestration run for the session."""

        def _run(cancel_event: threading.Event) -> None:
            self.run_sync(
                user_query=user_query,
                session_id=session_id,
                model_name=model_name,
                max_iters=max_iters,
                initial_task_queue=initial_task_queue,
                tool_registry=tool_registry,
                permission_policy=permission_policy,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                mode=mode,
                should_cancel=cancel_event.is_set,
            )

        return self._run_registry.start(session_id, target=_run)

    def run_sync(
        self,
        *,
        user_query: str,
        session_id: str,
        model_name: str | None = None,
        max_iters: int = 3,
        initial_task_queue: TaskQueue | None = None,
        tool_registry=None,
        permission_policy=None,
        approval_callback=None,
        hook_manager=None,
        mode: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TaskQueue:
        """Run an orchestration request synchronously."""
        return orchestrate_session(
            user_query=user_query,
            model_name=model_name,
            max_iters=max_iters,
            initial_task_queue=initial_task_queue,
            session_id=session_id,
            session_store=self._session_store,
            tool_registry=tool_registry,
            permission_policy=permission_policy,
            approval_callback=approval_callback,
            hook_manager=hook_manager,
            mode=mode,
            should_cancel=should_cancel,
        )

    def cancel(self, session_id: str) -> bool:
        """Cancel an active run if present."""
        return self._run_registry.cancel(session_id)

    def is_running(self, session_id: str) -> bool:
        """Return True if session has an active run."""
        return self._run_registry.is_running(session_id)
