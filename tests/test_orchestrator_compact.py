"""Tests for the user-initiated ``/compact`` path.

When a user types ``/compact`` the orchestrator must not propagate
exceptions — compaction failing (e.g. because an MCP reconnection
raised during summary generation) should surface as a user-facing
response message and a ``compact_failed`` completion reason, never
as a crashed CLI turn.
"""

from __future__ import annotations

from unittest.mock import patch

from mewbo_core.orchestrator import Orchestrator
from mewbo_core.session_store import SessionStore


def _make_orchestrator(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    return Orchestrator(session_store=store), store


class TestUserInitiatedCompactFailure:
    def test_compact_failure_surfaces_as_direct_response(self, tmp_path) -> None:
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        async def _raiser(events, mode, **kwargs):
            raise RuntimeError("mocked compaction boom")

        with patch("mewbo_core.compact.compact_conversation", _raiser):
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

        async def _raiser(events, mode, **kwargs):
            raise TypeError(
                "_create_streamable_http_session() got an unexpected keyword argument 'oauth'"
            )

        with patch("mewbo_core.compact.compact_conversation", _raiser):
            task_queue, state = orch.run(
                user_query="/compact",
                session_id=session_id,
                max_iters=1,
                return_state=True,
            )

        assert task_queue.task_result is not None
        assert "Compaction failed" in task_queue.task_result
        assert state.done_reason == "compact_failed"


class TestServerRegistryDispatch:
    """Any user-typed slash command that names a server-registry handler
    short-circuits the tool-use loop and surfaces ``result.body`` directly,
    so the operation lives in one place (``mewbo_core.commands``) instead
    of being re-implemented per UI."""

    def test_tag_routes_through_server_registry(self, tmp_path) -> None:
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        task_queue, state = orch.run(
            user_query="/tag primary",
            session_id=session_id,
            max_iters=1,
            return_state=True,
        )

        assert state.done is True
        assert state.done_reason == "command:tag"
        # ``_handle_tag`` mutates the store — verify the side effect rather
        # than the rendered string so the test isn't tied to copy.
        assert store.resolve_tag("primary") == session_id
        assert task_queue.task_result is not None
        assert "primary" in task_queue.task_result

    def test_unknown_slash_command_is_not_intercepted(self, tmp_path) -> None:
        """A ``/<name>`` that isn't a server-registry command must NOT be
        intercepted — control falls through to the regular tool-use loop."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        # Patch the context builder to detect that the loop was entered.
        # Any exception raised here proves we got past the interception
        # block (regression check — previously only ``/compact`` was
        # special-cased so this was implicitly true).
        entered_loop = []
        original_build = orch._context_builder.build

        def _spy(*args, **kwargs):
            entered_loop.append(True)
            return original_build(*args, **kwargs)

        with patch.object(orch._context_builder, "build", side_effect=_spy):
            try:
                orch.run(
                    user_query="/notacommand",
                    session_id=session_id,
                    max_iters=1,
                )
            except Exception:
                pass  # Loop will fail without an LLM — we only care it ran.

        assert entered_loop, (
            "loop was never entered — orchestrator wrongly intercepted /notacommand"
        )


class TestAutoCompactThrashGuard:
    """``_maybe_auto_compact`` must skip when the most recent event is a
    compaction marker — otherwise stale ``last_input_tokens`` from before
    the boundary triggers a re-summarization that overwrites the fresh
    summary the user just produced."""

    def test_skips_when_last_event_is_context_compacted(self, tmp_path) -> None:
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        # Simulate freshly-compacted state.
        store.save_summary(session_id, "fresh user-triggered summary")
        store.append_event(
            session_id,
            {
                "type": "context_compacted",
                "payload": {
                    "mode": "user",
                    "model": "m",
                    "tokens_before": 100_000,
                    "tokens_saved": 80_000,
                    "tokens_after": 20_000,
                    "events_summarized": 50,
                    "summary": "fresh user-triggered summary",
                    "fallback": False,
                },
            },
        )

        # Make any call into compact_conversation a hard failure: the guard
        # must short-circuit BEFORE compact_conversation is invoked.
        async def _explode(*args, **kwargs):
            raise AssertionError("compact_conversation must not be called")

        with patch("mewbo_core.compact.compact_conversation", _explode):
            assert orch._maybe_auto_compact(session_id) is None

        # And the marker count is unchanged (no second auto compaction event).
        events = store.load_transcript(session_id)
        markers = [e for e in events if e.get("type") == "context_compacted"]
        assert len(markers) == 1
