"""Tests for orchestrator-level title generation integration."""

from unittest.mock import patch

from mewbo_core.orchestrator import Orchestrator
from mewbo_core.session_store import SessionStore


def _orchestrator(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    return Orchestrator(session_store=store), store


def test_maybe_generate_title_skips_when_title_exists(tmp_path):
    """A session that already has a stored title does not spawn a worker."""
    orch, store = _orchestrator(tmp_path)
    session_id = store.create_session()
    store.save_title(session_id, "pre-existing")

    with patch.object(orch, "_run_title_generation") as mock_worker:
        orch._maybe_generate_title(session_id)
    mock_worker.assert_not_called()
    assert store.load_title(session_id) == "pre-existing"


def test_run_title_generation_saves_and_emits_event(tmp_path):
    """Worker persists the generated title and emits a title_update event."""
    orch, store = _orchestrator(tmp_path)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "hello"}})

    async def fake_gen(_events):
        return "Fresh Title"

    with patch("mewbo_core.title_generator.generate_session_title", fake_gen):
        orch._run_title_generation(session_id)

    assert store.load_title(session_id) == "Fresh Title"
    events = store.load_transcript(session_id)
    title_events = [e for e in events if e.get("type") == "title_update"]
    assert len(title_events) == 1
    assert title_events[0]["payload"]["title"] == "Fresh Title"


def test_run_title_generation_noop_when_generator_returns_none(tmp_path):
    """No title saved and no event emitted when generator returns None."""
    orch, store = _orchestrator(tmp_path)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    async def fake_gen(_events):
        return None

    with patch("mewbo_core.title_generator.generate_session_title", fake_gen):
        orch._run_title_generation(session_id)

    assert store.load_title(session_id) is None
    events = store.load_transcript(session_id)
    assert not any(e.get("type") == "title_update" for e in events)


def test_run_title_generation_swallows_exceptions(tmp_path):
    """A raising generator must not propagate out of the worker."""
    orch, store = _orchestrator(tmp_path)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    async def fake_gen(_events):
        raise RuntimeError("boom")

    with patch("mewbo_core.title_generator.generate_session_title", fake_gen):
        orch._run_title_generation(session_id)  # Must not raise.

    assert store.load_title(session_id) is None


def test_title_generation_sees_assistant_event(tmp_path):
    """Title generator receives both user and assistant events."""
    orch, store = _orchestrator(tmp_path)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi there"}})

    captured_events: list = []

    async def capturing_gen(events):
        captured_events.extend(events)
        return "Greeting Exchange"

    with patch("mewbo_core.title_generator.generate_session_title", capturing_gen):
        orch._run_title_generation(session_id)

    # Verify the generator received both event types
    event_types = [e["type"] for e in captured_events]
    assert "user" in event_types
    assert "assistant" in event_types


def test_maybe_generate_title_spawns_background_thread(tmp_path):
    """Dispatching path runs the worker off-thread and returns immediately."""
    import threading

    orch, store = _orchestrator(tmp_path)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    done = threading.Event()

    def slow_worker(_sid: str) -> None:
        done.wait(timeout=2.0)

    with patch.object(orch, "_run_title_generation", side_effect=slow_worker):
        orch._maybe_generate_title(session_id)  # must NOT block on the worker
        # Caller returned before the worker finished → we get here immediately.
        done.set()
