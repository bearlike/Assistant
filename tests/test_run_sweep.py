#!/usr/bin/env python3
"""Contract tests for the startup orphaned-session-run sweep.

Real JSON store, an injected clock (never a patched wall clock), no LLM. The
sweep's job: a session whose worker died mid-turn (run activity, no terminal
``completion``) gets exactly one synthetic error completion so
``summarize_session`` flips it to ``failed`` and the console's recovery card
renders; everything else is left untouched, and the pass is idempotent + never
raises.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from mewbo_api.run_sweep import SessionRunSweeper
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore


@pytest.fixture(autouse=True)
def _clear_boot_sweep_env(monkeypatch):
    """Keep the suite hermetic: the sweep defaults ON, so an ambient
    ``MEWBO_BOOT_RUN_SWEEP`` must never leak into the expect-settled cases. The
    opt-out test sets it explicitly on top of this."""
    monkeypatch.delenv("MEWBO_BOOT_RUN_SWEEP", raising=False)


def _now_fixed() -> datetime:
    """A stable 'now' well after any just-appended event (in-window)."""
    return datetime.now(timezone.utc)


def _completions(store: SessionStore, session_id: str) -> list[dict]:
    """Every ``completion`` event currently in a session transcript."""
    return [e for e in store.load_transcript(session_id) if e.get("type") == "completion"]


def _run_accepted(session_id: str) -> dict:
    """The run-start lifecycle marker the runtime emits before heavy setup."""
    return {
        "type": "run_accepted",
        "payload": {"session_id": session_id, "run_id": f"{session_id}:r1"},
    }


def _completion(done_reason: str) -> dict:
    """A terminal completion event with the given ``done_reason``."""
    return {
        "type": "completion",
        "payload": {"done": True, "done_reason": done_reason, "task_result": None},
    }


def _make_orphan(store: SessionStore) -> str:
    """A session whose last turn began (user → run_accepted → llm_call_start)
    but never closed — the process-restart signature."""
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do the thing"}})
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    return session_id


def _sweeper(store: SessionStore, *, now=None, window=None) -> SessionRunSweeper:
    """Build a sweeper over the store with an injected clock/window."""
    kwargs: dict = {"now": now or _now_fixed}
    if window is not None:
        kwargs["window"] = window
    return SessionRunSweeper(store, **kwargs)


def test_orphaned_run_settled_and_status_flips_to_failed(tmp_path):
    """An open run gets exactly one synthetic completion → status ``failed``."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = _make_orphan(store)

    assert runtime.summarize_session(session_id)["status"] == "idle"

    settled = _sweeper(store).sweep()

    assert settled == 1
    completions = _completions(store, session_id)
    assert len(completions) == 1
    payload = completions[0]["payload"]
    assert payload["done"] is True
    assert payload["done_reason"] == "error"
    assert payload["error"] == "interrupted: process restart"
    assert payload["task_result"] is None

    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "failed"
    assert summary["done_reason"] == "error"


def test_run_accepted_only_is_orphaned(tmp_path):
    """A run that died before its first LLM call (only run_accepted) is settled."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(session_id, _run_accepted(session_id))

    assert _sweeper(store).sweep() == 1
    assert len(_completions(store, session_id)) == 1


def test_completed_session_untouched(tmp_path):
    """A run that closed cleanly keeps its single completion, stays ``completed``."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi back"}})
    store.append_event(session_id, _completion("completed"))

    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1
    assert runtime.summarize_session(session_id)["status"] == "completed"


def test_completion_trailed_by_epilogue_untouched(tmp_path):
    """Post-run epilogue after a completion is not mistaken for an open run."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "go"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, _completion("completed"))
    # Events the runtime can append AFTER the completion (auto-compact,
    # attestation) — none is a run marker, so the run stays closed.
    store.append_event(session_id, {"type": "context_compacted", "payload": {"summary": "…"}})
    store.append_event(session_id, {"type": "attestation", "payload": {"record_hash": "abc"}})

    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1


def test_awaiting_approval_untouched(tmp_path):
    """A parked plan (completion with awaiting_approval) is not an orphan."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "plan it"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, _completion("awaiting_approval"))

    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1


def test_non_run_session_untouched(tmp_path):
    """A session with no run activity at all is never settled."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "demo"}})

    assert _sweeper(store).sweep() == 0
    assert _completions(store, session_id) == []


def test_archived_session_untouched(tmp_path):
    """An archived orphan is skipped — the sweep only settles live sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _make_orphan(store)
    store.archive_session(session_id)

    assert _sweeper(store).sweep() == 0
    assert _completions(store, session_id) == []


def test_terminated_session_untouched(tmp_path):
    """A terminated orphan is skipped (its appends would be dropped anyway)."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _make_orphan(store)
    store.terminate_session(session_id)

    assert _sweeper(store).sweep() == 0
    assert _completions(store, session_id) == []


def test_old_window_session_untouched(tmp_path):
    """An orphan whose last activity predates the recency window is left alone."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _make_orphan(store)

    # The injected clock is 30 days after the just-appended events, past the
    # 7-day default window — the events are backdated relative to 'now'.
    future = _now_fixed() + timedelta(days=30)
    assert _sweeper(store, now=lambda: future).sweep() == 0
    assert _completions(store, session_id) == []


def test_custom_window_boundary(tmp_path):
    """The window is honoured: same events settle under a wide window."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _make_orphan(store)

    future = _now_fixed() + timedelta(days=30)
    assert _sweeper(store, now=lambda: future, window=timedelta(days=365)).sweep() == 1
    assert len(_completions(store, session_id)) == 1


def test_double_sweep_is_idempotent(tmp_path):
    """A second, independent sweep settles nothing — one completion, not two."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _make_orphan(store)

    assert _sweeper(store).sweep() == 1
    # A fresh instance (a second process would build its own) sees the now-closed
    # run and no-ops; idempotency is a property of the transcript, not the guard.
    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1


def test_second_call_on_same_instance_is_a_noop(tmp_path):
    """The in-process guard makes a repeat call on one instance a no-op."""
    store = SessionStore(root_dir=str(tmp_path))
    _make_orphan(store)

    sweeper = _sweeper(store)
    assert sweeper.sweep() == 1
    assert sweeper.sweep() == 0


def test_list_sessions_failure_logs_and_does_not_raise(tmp_path):
    """A store that fails to enumerate logs and returns 0, never raising."""

    class _BrokenList(SessionStore):
        def list_sessions(self) -> list[str]:
            raise RuntimeError("store unavailable")

    store = _BrokenList(root_dir=str(tmp_path))
    # Must not raise; returns 0 settled.
    assert _sweeper(store).sweep() == 0


def test_per_session_failure_is_isolated(tmp_path):
    """One session's read failure is swallowed; the sweep still settles the rest."""
    store = SessionStore(root_dir=str(tmp_path))
    good = _make_orphan(store)
    bad = _make_orphan(store)

    class _PartialFail(SessionStore):
        def load_recent_events(self, session_id, limit=8, include_types=None):
            if session_id == bad:
                raise RuntimeError("corrupt transcript")
            return super().load_recent_events(session_id, limit, include_types)

    broken = _PartialFail(root_dir=str(tmp_path))
    # The bad session raises inside the per-session try/except; the good one is
    # still settled.
    settled = _sweeper(broken).sweep()
    assert settled == 1
    assert len(_completions(store, good)) == 1
    assert _completions(store, bad) == []


# -- marker-set narrowing: post-completion events must never false-flip --------


def test_completed_then_new_user_turn_not_flipped(tmp_path):
    """A completed run followed by a queued next user turn must NOT flip.

    ``user`` is deliberately not a run marker: it lands after a completion the
    instant the user types again, before the next run starts.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "first"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, _completion("completed"))
    store.append_event(session_id, {"type": "user", "payload": {"text": "second"}})

    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1
    assert runtime.summarize_session(session_id)["status"] == "completed"


def test_completed_then_trailing_sub_agent_not_flipped(tmp_path):
    """A background sub_agent stop landing after the completion must NOT flip."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "go"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, _completion("completed"))
    # A child's lifecycle stop arrives past the 3s drain window.
    store.append_event(session_id, {"type": "sub_agent", "payload": {"phase": "stop"}})

    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1


def test_died_after_llm_call_end_is_orphaned(tmp_path):
    """Returned from the LLM call but never wrote a completion → settled."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "go"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, {"type": "llm_call_end", "payload": {"step": 1}})

    assert _sweeper(store).sweep() == 1
    assert len(_completions(store, session_id)) == 1


def test_died_mid_steer_is_orphaned_via_run_start_anchor(tmp_path):
    """A second run (started, steered, then killed) is settled via its
    ``run_accepted`` anchor. ``user_steer`` is NOT a marker, so detection here
    rides the run-start event that precedes it, never the steer itself."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "go"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, _completion("completed"))
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(session_id, {"type": "user_steer", "payload": {"text": "also X"}})

    assert _sweeper(store).sweep() == 1
    assert len(_completions(store, session_id)) == 2


def test_trailing_user_steer_after_completion_not_flipped(tmp_path):
    """A steer that raced the run-handle teardown — ``user_steer`` written just
    after the completion with NO new ``run_accepted`` — must NOT flip a
    completed run. ``user_steer`` is excluded from the marker set for exactly
    this window (``is_running`` briefly lags the completion write)."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "go"}})
    store.append_event(session_id, {"type": "llm_call_start", "payload": {"step": 1}})
    store.append_event(session_id, _completion("completed"))
    store.append_event(session_id, {"type": "user_steer", "payload": {"text": "wait"}})

    assert _sweeper(store).sweep() == 0
    assert len(_completions(store, session_id)) == 1
    assert runtime.summarize_session(session_id)["status"] == "completed"


# -- orphaned sub-agent spans -------------------------------------------------
#
# A run that ended without its children ever recording a terminal ``stop``
# leaves those agents pinned live for every consumer that derives an agent's
# state from the last ``sub_agent`` payload. The sweep settles those spans the
# same way it settles the root run: append the terminal the runtime would have
# written had the worker survived.


def _sub_agent(action: str, agent_id: str, **extra) -> dict:
    """A ``sub_agent`` lifecycle event in the shape the spawn bridge emits."""
    payload = {
        "action": action,
        "agent_id": agent_id,
        "parent_id": "root-agent",
        "depth": 1,
        "model": "test-model",
        "detail": "do the sub-thing",
        "status": "running" if action == "start" else "completed",
        "steps_completed": 3,
        "input_tokens": 10,
        "output_tokens": 20,
    }
    payload.update(extra)
    return {"type": "sub_agent", "payload": payload}


def _agent_stops(store: SessionStore, session_id: str) -> list[dict]:
    """Every terminal ``sub_agent`` stop payload in a transcript."""
    return [
        e["payload"]
        for e in store.load_transcript(session_id)
        if e.get("type") == "sub_agent" and e["payload"].get("action") == "stop"
    ]


def _session_with_orphan_agent(store: SessionStore, *, closed: bool = True) -> str:
    """A session whose run ended while a spawned child never stopped."""
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "delegate it"}})
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(session_id, _sub_agent("start", "agent-a"))
    if closed:
        store.append_event(session_id, _completion("completed"))
    return session_id


def test_orphaned_agent_span_settled_after_run_end(tmp_path):
    """A start with no stop, under a run that has since ended, is settled."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _session_with_orphan_agent(store)

    assert _sweeper(store).sweep() == 1
    stops = _agent_stops(store, session_id)
    assert len(stops) == 1
    assert stops[0]["agent_id"] == "agent-a"
    assert stops[0]["status"] == "cancelled"
    # The root run closed cleanly — settling its child must not add a completion.
    assert len(_completions(store, session_id)) == 1


def test_synthetic_agent_stop_mirrors_the_real_stop_shape(tmp_path):
    """Consoles parse one payload shape; the synthetic stop must match it."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(
        session_id, _sub_agent("start", "agent-a", agent_type="probe", depth=2)
    )
    store.append_event(session_id, _completion("completed"))

    _sweeper(store).sweep()
    stop = _agent_stops(store, session_id)[0]

    assert {
        "action",
        "agent_id",
        "parent_id",
        "depth",
        "model",
        "detail",
        "status",
        "steps_completed",
        "input_tokens",
        "output_tokens",
    } <= set(stop)
    # Lane identity is carried over from the start, never re-invented.
    assert stop["agent_type"] == "probe"
    assert stop["depth"] == 2
    assert stop["parent_id"] == "root-agent"
    assert stop["detail"]  # a behavioural reason, never blank


def test_agent_span_settle_is_idempotent(tmp_path):
    """A second sweep appends nothing — the orphan set is empty by construction."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _session_with_orphan_agent(store)

    assert _sweeper(store).sweep() == 1
    after_first = len(store.load_transcript(session_id))
    assert _sweeper(store).sweep() == 0
    assert len(_agent_stops(store, session_id)) == 1
    assert len(store.load_transcript(session_id)) == after_first


def test_agent_with_its_own_stop_is_left_alone(tmp_path):
    """A child that recorded its own terminal is never given a second one."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(session_id, _sub_agent("start", "agent-a"))
    store.append_event(session_id, _sub_agent("stop", "agent-a"))
    store.append_event(session_id, _completion("completed"))

    assert _sweeper(store).sweep() == 0
    assert len(_agent_stops(store, session_id)) == 1


def test_start_after_last_completion_is_not_settled(tmp_path):
    """A child spawned after the last completion has no ended run behind it."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(session_id, _completion("completed"))
    store.append_event(session_id, _sub_agent("start", "agent-late"))

    assert _sweeper(store).sweep() == 0
    assert _agent_stops(store, session_id) == []


def test_orphaned_run_and_orphaned_child_both_settled(tmp_path):
    """The run's synthetic completion is what closes its stranded child."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _session_with_orphan_agent(store, closed=False)

    assert _sweeper(store).sweep() == 1
    assert len(_completions(store, session_id)) == 1
    assert len(_agent_stops(store, session_id)) == 1
    assert _agent_stops(store, session_id)[0]["agent_id"] == "agent-a"


def test_only_one_stop_per_orphaned_agent(tmp_path):
    """Several stranded children each get exactly one terminal."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, _run_accepted(session_id))
    for agent_id in ("agent-a", "agent-b", "agent-c"):
        store.append_event(session_id, _sub_agent("start", agent_id))
    store.append_event(session_id, _sub_agent("stop", "agent-b"))
    store.append_event(session_id, _completion("completed"))

    assert _sweeper(store).sweep() == 1
    stops = _agent_stops(store, session_id)
    settled = sorted(s["agent_id"] for s in stops if s["status"] == "cancelled")
    assert settled == ["agent-a", "agent-c"]
    assert len(stops) == 3  # agent-b keeps its own, the other two gain one each


def test_archived_session_agent_spans_untouched(tmp_path):
    """The existing gates cover the agent pass too — archived is skipped."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _session_with_orphan_agent(store)
    store.archive_session(session_id)

    assert _sweeper(store).sweep() == 0
    assert _agent_stops(store, session_id) == []


def test_out_of_window_session_agent_spans_untouched(tmp_path):
    """An ancient stranded child is not resurfaced as fresh news."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _session_with_orphan_agent(store)

    future = _now_fixed() + timedelta(days=30)
    assert _sweeper(store, now=lambda: future).sweep() == 0
    assert _agent_stops(store, session_id) == []


def test_unkeyed_sub_agent_events_are_ignored(tmp_path):
    """A lifecycle event with no ``agent_id`` can never be paired — skip it."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, _run_accepted(session_id))
    store.append_event(session_id, {"type": "sub_agent", "payload": {"phase": "start"}})
    store.append_event(session_id, _completion("completed"))

    assert _sweeper(store).sweep() == 0
    assert _agent_stops(store, session_id) == []


# -- env opt-out --------------------------------------------------------------


def test_env_opt_out_skips(monkeypatch, tmp_path):
    """MEWBO_BOOT_RUN_SWEEP=0 skips the sweep entirely — no writes."""
    monkeypatch.setenv("MEWBO_BOOT_RUN_SWEEP", "0")
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _make_orphan(store)

    assert _sweeper(store).sweep() == 0
    assert _completions(store, session_id) == []


def test_env_default_on_settles(tmp_path):
    """With the var unset (the autouse fixture clears it) the sweep runs."""
    store = SessionStore(root_dir=str(tmp_path))
    _make_orphan(store)

    assert _sweeper(store).sweep() == 1
