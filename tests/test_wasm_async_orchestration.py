"""Tests for the WASM/async-orchestration surface fixes.

Covers the reviewed fixes to ``session_runtime.py`` / ``orchestrator.py`` /
``task_master.py``:

* Backend selection in :meth:`SessionRuntime.start_async` is gated on BOTH
  ``sys.platform == "emscripten"`` AND an already-running event loop — a
  CPython caller always takes the daemon-thread path, even when invoked from
  within a running loop, so a future async CPython caller can't accidentally
  run blocking orchestration on its own loop.
* The loop-backed run's ``asyncio.Task`` is stored on its ``RunHandle`` — a
  strong reference held by ``RunRegistry`` for the run's lifetime — so
  asyncio can't garbage-collect the fire-and-forget task mid-run.
* ``orchestrate_session`` (sync) collapses to
  ``asyncio.run(orchestrate_session_async(...))``, mirroring
  ``Orchestrator.run() = asyncio.run(self.arun(...))`` one layer up.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

from mewbo_core.classes import TaskQueue
from mewbo_core.orchestrator import Orchestrator
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore


def _wait_idle(runtime: SessionRuntime, session_id: str, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and runtime.is_running(session_id):
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# (a) emscripten + running loop -> loop branch; task stored on the RunHandle
# ---------------------------------------------------------------------------


def test_start_async_emscripten_with_running_loop_takes_loop_branch(tmp_path, monkeypatch):
    """WASM/emscripten with an already-running loop drives orchestration as
    an ``asyncio.Task`` (no daemon thread), and the task is stored on the
    ``RunHandle`` so ``RunRegistry`` holds a strong reference for the run's
    lifetime (fix: untracked-task GC)."""
    import mewbo_core.session_runtime as sr

    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    monkeypatch.setattr(sys, "platform", "emscripten")

    ran = {}

    async def fake_orchestrate_session_async(**kwargs):
        ran["called"] = True
        return TaskQueue(action_steps=[])

    monkeypatch.setattr(sr, "orchestrate_session_async", fake_orchestrate_session_async)

    async def _drive():
        run_id = runtime.start_async(session_id=session_id, user_query="hello")
        assert run_id == f"{session_id}:r1"

        handle = runtime._run_registry.get_handle(session_id)
        assert handle is not None
        assert handle.thread is None  # no daemon thread spun up
        assert handle.loop_active is True
        assert isinstance(handle.task, asyncio.Task)  # task stored on the handle

        await handle.task  # let the scheduled task actually run to completion
        assert ran.get("called") is True

    asyncio.run(_drive())
    # The done-callback finalized the loop-backed run.
    assert runtime.is_running(session_id) is False


# ---------------------------------------------------------------------------
# (b) CPython (non-emscripten) -> always the thread path, even inside a
# running loop (the case the tightened gate now protects against).
# ---------------------------------------------------------------------------


def test_start_async_cpython_uses_thread_path_even_inside_a_running_loop(tmp_path):
    """On CPython (non-emscripten), ``start_async`` always dispatches to the
    daemon-thread backend — even when invoked from within a running event
    loop (e.g. a future async CPython caller) — so blocking orchestration
    never runs on that caller's own loop."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    done = threading.Event()

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        done.wait(timeout=2.0)

    runtime.run_sync = fake_run_sync

    async def _drive():
        assert sys.platform != "emscripten"  # sanity: real platform, unpatched
        run_id = runtime.start_async(session_id=session_id, user_query="hello")
        assert run_id == f"{session_id}:r1"

        handle = runtime._run_registry.get_handle(session_id)
        assert handle is not None
        assert handle.thread is not None  # daemon-thread branch taken
        assert handle.thread.daemon is True
        assert handle.task is None  # loop branch NOT taken
        assert handle.loop_active is False

    asyncio.run(_drive())
    done.set()
    _wait_idle(runtime, session_id)


# ---------------------------------------------------------------------------
# (c) Orchestrator.run() / orchestrate_session() still work synchronously
# after the task_master DRY collapse.
# ---------------------------------------------------------------------------


def test_orchestrator_run_still_works_synchronously(tmp_path, monkeypatch):
    """``Orchestrator.run()`` (``asyncio.run(self.arun(...))``) is unaffected
    by the ``task_master`` DRY collapse — smoke it with ``arun`` stubbed so
    no LLM call happens."""
    store = SessionStore(root_dir=str(tmp_path))
    orch = Orchestrator(session_store=store)
    session_id = store.create_session()

    async def fake_arun(self, user_query, **kwargs):
        return TaskQueue(action_steps=[])

    monkeypatch.setattr(Orchestrator, "arun", fake_arun)

    result = orch.run("hello", session_id=session_id)
    assert isinstance(result, TaskQueue)


def test_orchestrate_session_sync_collapses_to_async_entry_point(tmp_path, monkeypatch):
    """``orchestrate_session`` now delegates to
    ``asyncio.run(orchestrate_session_async(...))``; verify the collapsed
    sync entry point still forwards params correctly end to end."""
    import mewbo_core.task_master as tm

    captured: dict = {}

    async def fake_async(user_query, **kwargs):
        captured["user_query"] = user_query
        captured.update(kwargs)
        return TaskQueue(action_steps=[])

    monkeypatch.setattr(tm, "orchestrate_session_async", fake_async)

    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    result = tm.orchestrate_session(
        "hello",
        session_store=store,
        session_id=session_id,
        strict_tool_scope=True,
        enable_skills=False,
        attachments=[{"id": "a1"}],
    )

    assert isinstance(result, TaskQueue)
    assert captured["user_query"] == "hello"
    assert captured["session_id"] == session_id
    assert captured["strict_tool_scope"] is True
    assert captured["enable_skills"] is False
    assert captured["attachments"] == [{"id": "a1"}]
