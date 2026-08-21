#!/usr/bin/env python3
"""Two runs of one session must land on two DISTINCT Langfuse traces.

Before ``invocation_id`` had a producer, ``Orchestrator.arun`` always called
``langfuse_session_context(session_id, invocation_id=None, ...)``, and
``_build_langfuse_trace_context`` used to derive a trace id by SEEDING
``Langfuse.create_trace_id(seed=session_id)`` -- deterministic on that seed, so
every run of the same session collapsed onto one trace. The fix is
``SessionRuntime.start_async`` defaulting ``invocation_id`` to the per-run
``run_id`` it already mints (see ``session_runtime.py:_mint_run_id``) when the
caller supplies none.

This drives the REAL production path -- ``start_async`` -> the background
worker -> ``orchestrate_session`` -> ``Orchestrator.arun`` ->
``langfuse_session_context`` -- stubbing only ``ToolUseLoop.run`` (the LLM
boundary), so a dropped ``invocation_id`` at any hand-off in that chain would
show up here exactly as it would in production: two runs sharing one trace id.
"""

from __future__ import annotations

from unittest.mock import patch

import mewbo_core.components as comp_module
from mewbo_core.classes import OrchestrationState, TaskQueue
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.session.session_store import SessionStore
from test_user_turn_persistence import _join

_GATE_TIMEOUT = 10.0


def test_two_runs_of_one_session_get_distinct_trace_ids(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    captured_trace_ids: list[str | None] = []

    async def _completed_loop_run(*_args, **_kwargs):
        # Read the trace context bound by langfuse_session_context while this
        # run is live -- the same context var langfuse_trace_span reads to
        # attach a span to the running trace.
        ctx = comp_module._LANGFUSE_TRACE_CONTEXT.get()
        captured_trace_ids.append(ctx["trace_id"] if ctx else None)
        task_queue = TaskQueue(action_steps=[])
        task_queue.task_result = "Done"
        state = OrchestrationState(goal="test", session_id=session_id)
        state.done = True
        state.done_reason = "completed"
        return task_queue, state

    with patch.object(ToolUseLoop, "run", _completed_loop_run):
        runtime.start_async(session_id=session_id, user_query="first turn")
        _join(runtime, session_id, timeout=_GATE_TIMEOUT)
        runtime.start_async(session_id=session_id, user_query="second turn")
        _join(runtime, session_id, timeout=_GATE_TIMEOUT)

    assert len(captured_trace_ids) == 2
    # Every run must actually bind a trace context -- a None here would mean
    # invocation_id never reached langfuse_session_context at all.
    assert all(captured_trace_ids), captured_trace_ids
    assert captured_trace_ids[0] != captured_trace_ids[1]


def test_explicit_invocation_id_still_wins_over_the_minted_run_id(tmp_path):
    """A caller-supplied invocation_id is honoured, not overwritten by the mint.

    ``start_async`` only fills in the run id when ``invocation_id`` is absent
    -- an explicit caller (a channel replaying its own correlation id) must
    see that value reach the trace context unchanged.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    explicit_id = "a" * 32  # valid hex, so _build_langfuse_trace_context uses it verbatim

    captured: list[str | None] = []

    async def _completed_loop_run(*_args, **_kwargs):
        ctx = comp_module._LANGFUSE_TRACE_CONTEXT.get()
        captured.append(ctx["trace_id"] if ctx else None)
        task_queue = TaskQueue(action_steps=[])
        task_queue.task_result = "Done"
        state = OrchestrationState(goal="test", session_id=session_id)
        state.done = True
        state.done_reason = "completed"
        return task_queue, state

    with patch.object(ToolUseLoop, "run", _completed_loop_run):
        runtime.start_async(
            session_id=session_id, user_query="turn", invocation_id=explicit_id
        )
        _join(runtime, session_id, timeout=_GATE_TIMEOUT)

    assert captured == [explicit_id]
