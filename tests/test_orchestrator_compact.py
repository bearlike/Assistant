"""Tests for the user-initiated ``/compact`` path.

When a user types ``/compact`` the orchestrator must not propagate
exceptions — compaction failing (e.g. because an MCP reconnection
raised during summary generation) should surface as a user-facing
response message and a ``compact_failed`` completion reason, never
as a crashed CLI turn.
"""

from __future__ import annotations

from unittest.mock import patch

from meeseeks_core.orchestrator import Orchestrator
from meeseeks_core.session_store import SessionStore


def _make_orchestrator(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    return Orchestrator(session_store=store), store


class TestUserInitiatedCompactFailure:
    def test_compact_failure_surfaces_as_direct_response(self, tmp_path) -> None:
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        async def _raiser(events, mode):
            raise RuntimeError("mocked compaction boom")

        with patch("meeseeks_core.compact.compact_conversation", _raiser):
            task_queue, state = orch.run(
                user_query="/compact",
                session_id=session_id,
                max_iters=1,
                return_state=True,
            )

        # Direct response carries the failure message; state flags compact_failed.
        assert task_queue.task_result is not None
        assert "Compaction failed" in task_queue.task_result
        assert "mocked compaction boom" in task_queue.task_result
        assert state.done is True
        assert state.done_reason == "compact_failed"

    def test_compact_failure_does_not_raise(self, tmp_path) -> None:
        """Regression guard: orchestrator.run must return normally even when
        compact_conversation raises an adapter-style TypeError (the exact
        shape the smoke test surfaced via the slack plugin)."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        async def _raiser(events, mode):
            raise TypeError(
                "_create_streamable_http_session() got an unexpected keyword argument 'oauth'"
            )

        with patch("meeseeks_core.compact.compact_conversation", _raiser):
            task_queue, state = orch.run(
                user_query="/compact",
                session_id=session_id,
                max_iters=1,
                return_state=True,
            )

        assert task_queue.task_result is not None
        assert "Compaction failed" in task_queue.task_result
        assert state.done_reason == "compact_failed"
