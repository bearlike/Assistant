#!/usr/bin/env python3
"""Startup honesty sweep for orphaned SESSION runs.

A process death (deploy, restart, OOM kill) can strand an in-flight session
run: the worker dies mid-turn, so the transcript ends on run activity (e.g. a
``run_accepted`` / ``llm_call_start`` marker) with no terminal ``completion``
event. ``summarize_session`` then derives ``status="idle"`` — the console's
recovery card never renders and the user is left guessing whether anything ran.

``SessionRunSweeper`` is the session-side peer of the apps ledger's
``AppPipelineRunTracker.sweep_orphaned_runs``: run ONCE at startup, before any
new run can begin, it appends a synthetic terminal ``completion``
(``done_reason="error"``) to each orphaned session so the derived status flips
to ``failed`` and the existing recovery affordance renders. At boot the
in-process run registry is empty by definition, so an open run left in a
transcript is, by definition, an orphan with no live worker behind it.

Cost per backend (bounded scan): the sweep skips archived/terminated sessions
and reads only each candidate's event TAIL (``load_recent_events``). The JSON
driver reads the whole transcript file before slicing — inherent to a flat JSONL
file with no cheap reverse read — so JSON pays O(transcript) per live session;
the Mongo driver OVERRIDES ``load_recent_events`` with a bounded
``sort(ts DESC).limit(n)`` range read, so Mongo pays one indexed tail query per
live session. Either way the recency window bounds which orphans are SETTLED, and
``list_sessions`` on an empty store returns nothing.

Write-at-import: this appends synthetic ``completion`` events to the CONFIGURED
store the moment it runs, and in this app "at startup" == "at import" — so any
test that imports ``backend.py`` triggers it against whatever store the test
config points at. It is idempotent (a re-import settles nothing), but it IS a
write. Set ``MEWBO_BOOT_RUN_SWEEP=0`` to skip entirely (env-driven, default on;
no config-schema knob). See ``apps/mewbo_api/CLAUDE.md`` → the ``/variables``
prime-at-boot post-mortem for why import == startup here.

Deployment note: prod is gunicorn ``--workers 1``, so the sweep runs once per
process. A multi-worker deployment would invoke it once per worker; that is
harmless because the sweep is IDEMPOTENT — a session that already carries a
terminal ``completion`` for its last turn is skipped, so a second pass settles
nothing. The in-process guard (``_done``) only stops a redundant re-run within
one process, mirroring the ``_open_lock`` single-worker reasoning in the apps
pipeline tracker; cross-process double-invocation is covered by idempotency, not
the guard. Store-only I/O, no network.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta

from mewbo_core.common import get_logger
from mewbo_core.session_store import SessionStoreBase
from mewbo_core.types import Event, EventRecord

logging = get_logger(name="api.run_sweep")

# Opt-out for the write-at-import sweep (default ON). Env-driven, no
# config-schema knob — mirrors MEWBO_APPS_ROOT. Set to ``0`` to skip entirely
# (e.g. a test/CLI import that must not write synthetic completions).
_BOOT_SWEEP_ENV = "MEWBO_BOOT_RUN_SWEEP"

# Events that occur ONLY within a root run's active lifetime — their presence
# after the last ``completion`` marks a turn that began and never closed:
# ``run_accepted`` (a run started) and the ``llm_call_start``/``llm_call_end``
# bracket (the exact window the incident died in — a transcript ending on
# ``llm_call_start`` with no matching end and no completion). Deliberately a
# NARROW positive allowlist, and three event types are EXCLUDED because each can
# land legitimately AFTER a completed run's ``completion`` and would FALSE-FLIP
# it: a bare ``user`` turn (a queued next turn), a background ``sub_agent`` stop
# (a child lifecycle event past the drain window), and ``user_steer`` (a
# ``/message`` racing the run-handle teardown — ``is_running`` briefly lags the
# completion write, so a steer can append just after it). ``user_steer`` is also
# redundant: a genuinely-orphaned steered run is already anchored by the
# ``run_accepted`` that started it (after the last completion). An unrecognised
# or post-completion event therefore counts as NON-progress by construction: the
# worst case is a MISSED orphan (status stays ``idle`` — no regression), never a
# completed run wrongly flipped. Miss-only is the contract. (``action_plan`` is
# not emitted as a transcript event, so it is not listed.)
_RUN_MARKER_EVENTS: frozenset[str] = frozenset(
    {
        "run_accepted",
        "llm_call_start",
        "llm_call_end",
    }
)

# Only settle runs whose LAST activity is recent. The window bounds the one-time
# backfill when this sweep first ships (an ancient abandoned mid-call session is
# not the incident this fixes and should not resurface as a fresh red card);
# steady-state boots only ever see runs orphaned since the previous start, well
# inside it.
_RECENT_ACTIVITY_WINDOW = timedelta(days=7)

# The sweeper examines only each candidate's event TAIL, never scanning full
# history in its own logic. A closed run's ``completion`` sits at the very end
# of its turn — ahead of at most a handful of post-run epilogue events — so a
# tail this deep always contains it relative to that turn's run markers. NOTE
# this bounds the STORE read only on Mongo (the overridden bounded tail query);
# the JSON driver materializes the whole transcript file to serve the tail
# (inherent — see the module docstring's per-backend cost).
_TAIL_SCAN_LIMIT = 64

# The agent-span pass cannot use the tail: a child's ``start`` can sit
# arbitrarily far back in a long turn while the ``completion`` that stranded it
# is the very last event, so pairing starts to stops needs the whole run of
# lifecycle events, not a window. The cost is bounded three ways — the pass runs
# only for sessions that already cleared the archived/terminated and recency
# gates, the read is filtered to the two event types the pairing reads (server
# side on Mongo, in the base template on JSON), and this cap stops a pathological
# transcript from materialising without limit. A session with more lifecycle
# events than this keeps its oldest spans open, which is the same miss-only
# failure mode the root pass already accepts.
_AGENT_SCAN_LIMIT = 4096

# The event types the agent-span pass pairs over. ``completion`` is what marks
# the run as ended; ``sub_agent`` carries the start/stop lifecycle.
_AGENT_SCAN_TYPES: frozenset[str] = frozenset({"sub_agent", "completion"})

# The synthetic terminal for a child whose run ended without it stopping.
# Behavioural, not diagnostic: the transcript records what is missing, and the
# status field alongside it carries the terminal state.
_ORPHANED_AGENT_DETAIL = "run ended without a recorded child stop"

# The synthetic terminal error the runtime would have written had the worker
# survived. Shape matches ``orchestrator.py``'s failure completion so every
# downstream reader (``summarize_session`` status, the ``error`` blurb, the
# recovery affordance) is satisfied.
_INTERRUPTED_ERROR = "interrupted: process restart"


class SessionRunSweeper:
    """Settle session runs orphaned by a process restart, once at startup.

    Collaborators are injected as fields: the session store it reads and writes
    through its existing ``append_event`` seam, and a ``now`` clock (a test
    passes a fixed lambda — no clock is ever patched). Pure decision logic
    (``_is_orphaned``) is separated from the I/O edges (``sweep``).
    """

    def __init__(
        self,
        store: SessionStoreBase,
        *,
        now: Callable[[], datetime],
        window: timedelta = _RECENT_ACTIVITY_WINDOW,
        tail_limit: int = _TAIL_SCAN_LIMIT,
    ) -> None:
        """Wire the store + clock; ``window``/``tail_limit`` are test overrides."""
        self._store = store
        self._now = now
        self._window = window
        self._tail_limit = tail_limit
        self._done = False

    def sweep(self) -> int:
        """Append a synthetic terminal completion to every orphaned session run.

        Returns the number of sessions settled (for a startup log line). Never
        raises: an enumeration failure and any single session's failure are both
        logged and swallowed, so a bad transcript or an unreachable store can
        never break startup — which, at import, is also every test import.
        Re-invoking after a successful pass is a no-op via the in-process guard,
        belt to the transcript-level idempotency the detection already
        guarantees. Honors the ``MEWBO_BOOT_RUN_SWEEP=0`` opt-out (default on).
        """
        if os.environ.get(_BOOT_SWEEP_ENV, "1") == "0":
            logging.info("Session-run startup sweep disabled via {}=0", _BOOT_SWEEP_ENV)
            return 0
        if self._done:
            return 0
        self._done = True
        try:
            session_ids = self._store.list_sessions()
        except Exception:
            logging.warning("Session-run sweep could not list sessions", exc_info=True)
            return 0
        settled = 0
        for session_id in session_ids:
            try:
                if self._settle_if_orphaned(session_id):
                    settled += 1
            except Exception:
                logging.warning(
                    "Session-run sweep skipped {} after an error", session_id, exc_info=True
                )
        return settled

    def _settle_if_orphaned(self, session_id: str) -> bool:
        """Settle one session's orphaned run AND agent spans; return settled?.

        Cheap gates first (archived / terminated index reads), then a bounded
        tail read, then the recency window, then the pure open-run check — so a
        session that is out of scope never pays for the tail scan's successors.

        The two passes are ordered, not independent: the root pass runs first so
        that a run killed mid-flight has its synthetic ``completion`` on the
        transcript BEFORE the agent pass looks for children stranded behind a
        run end. Reversed, a stranded child of a still-open run would be missed
        on this boot and only settle on the next one.
        """
        if self._store.is_archived(session_id) or self._store.is_terminated(session_id):
            return False
        tail = self._store.load_recent_events(session_id, limit=self._tail_limit)
        if not tail:
            return False
        if not self._within_window(tail[-1]):
            return False
        settled = False
        if self._is_orphaned(tail):
            self._store.append_event(session_id, self._interrupted_completion())
            settled = True
        return self._settle_agent_spans(session_id) or settled

    def _settle_agent_spans(self, session_id: str) -> bool:
        """Append a terminal ``stop`` for every child stranded by a run end.

        Returns whether anything was appended. Idempotent by construction: each
        appended stop puts its ``agent_id`` in the stopped set, so the orphan
        set is empty on the next pass and a re-run writes nothing.
        """
        events = self._store.load_recent_events(
            session_id, limit=_AGENT_SCAN_LIMIT, include_types=set(_AGENT_SCAN_TYPES)
        )
        orphans = self._orphaned_agent_starts(events)
        for start in orphans:
            self._store.append_event(session_id, self._orphaned_agent_stop(start))
        return bool(orphans)

    def _orphaned_agent_starts(self, events: list[EventRecord]) -> list[dict]:
        """The ``start`` payloads with no stop, under a run that has ended.

        Pure over the event list. An agent is orphaned when its ``start`` was
        never paired with a ``stop`` AND a ``completion`` landed after it — the
        run it belonged to finished while the child was still open, so nothing
        is left to terminate it. A start AFTER the last completion is deliberately
        excluded: no run has ended behind it, so it is not yet evidence of a
        stranded child. An event with no ``agent_id`` can never be paired and is
        ignored rather than guessed at.
        """
        last_completion = -1
        starts: dict[str, tuple[int, dict]] = {}
        stopped: set[str] = set()
        for idx, event in enumerate(events):
            if event.get("type") == "completion":
                last_completion = idx
                continue
            payload = event.get("payload") or {}
            agent_id = payload.get("agent_id")
            if not isinstance(agent_id, str):
                continue
            action = payload.get("action")
            if action == "start":
                starts.setdefault(agent_id, (idx, payload))
            elif action == "stop":
                stopped.add(agent_id)
        return [
            payload
            for agent_id, (idx, payload) in starts.items()
            if agent_id not in stopped and last_completion > idx
        ]

    def _orphaned_agent_stop(self, start: dict) -> Event:
        """The synthetic terminal ``stop`` for one stranded child.

        Mirrors the spawn bridge's own stop payload, carrying the lane identity
        (``parent_id``/``depth``/``model``/``agent_type``) straight over from the
        ``start`` rather than re-inventing it — a console renders the lane from
        those keys, and a value invented here would be a value it renders wrong.
        """
        payload: dict[str, object] = {
            "action": "stop",
            "agent_id": start.get("agent_id"),
            "parent_id": start.get("parent_id"),
            "depth": start.get("depth", 0),
            "model": start.get("model", ""),
            "detail": _ORPHANED_AGENT_DETAIL,
            "status": "cancelled",
            "steps_completed": start.get("steps_completed", 0),
            "input_tokens": start.get("input_tokens", 0),
            "output_tokens": start.get("output_tokens", 0),
        }
        agent_type = start.get("agent_type")
        if agent_type:
            payload["agent_type"] = agent_type
        return {"type": "sub_agent", "payload": payload}

    def _is_orphaned(self, tail: list[EventRecord]) -> bool:
        """True iff a run marker appears with no ``completion`` after it.

        Pure over the event tail — the injected clock and the store never enter
        here. Mirrors ``summarize_session``'s "died mid-call with no completion"
        notion: a run began and its closing ``completion`` never landed. Robust
        to post-run epilogue events, which are not run markers and so leave the
        marker position behind the last completion.
        """
        last_marker = -1
        last_completion = -1
        for idx, event in enumerate(tail):
            etype = event.get("type")
            if etype == "completion":
                last_completion = idx
            elif etype in _RUN_MARKER_EVENTS:
                last_marker = idx
        return last_marker > last_completion

    def _within_window(self, event: EventRecord) -> bool:
        """True iff the event's timestamp is within the recency window.

        A missing/unparseable timestamp is treated as in-window (settle rather
        than silently skip): the honesty fix is the safer default when the
        recency signal is absent.
        """
        raw = event.get("ts")
        if not isinstance(raw, str):
            return True
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return (self._now() - ts) <= self._window

    def _interrupted_completion(self) -> Event:
        """The synthetic terminal completion appended to an orphaned run.

        The 4-key shape ``summarize_session`` needs to derive ``failed``. The
        runtime's own failure completion (``orchestrator.py``) additionally
        carries two ADDITIVE keys (``last_error``, ``error_detail``); they are
        omitted here and every reader tolerates their absence — both are
        ``NotRequired`` on ``CompletionPayload`` and Aura decodes with
        ``ignoreUnknownKeys``.
        """
        return {
            "type": "completion",
            "payload": {
                "done": True,
                "done_reason": "error",
                "error": _INTERRUPTED_ERROR,
                "task_result": None,
            },
        }


__all__ = ["SessionRunSweeper"]
