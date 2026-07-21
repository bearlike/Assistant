"""Tests for shared session runtime helpers."""

import threading
import time

import pytest
from mewbo_core.session_runtime import SessionRuntime, parse_core_command
from mewbo_core.session_store import SessionStore


def test_parse_core_command():
    """Detect supported core commands."""
    assert parse_core_command("/compact") == "/compact"
    assert parse_core_command("/terminate now") == "/terminate"
    assert parse_core_command("/status") == "/status"
    assert parse_core_command("/unknown") is None


def test_runtime_resolve_and_summarize(tmp_path):
    """Resolve sessions and return summaries."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session(session_tag="primary")
    assert store.resolve_tag("primary") == session_id
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    summary = runtime.summarize_session(session_id)
    assert summary["session_id"] == session_id
    assert summary["title"] == "hello"


def test_runtime_summarize_prefers_stored_title(tmp_path):
    """summarize_session prefers a stored title over first-user-message."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.save_title(session_id, "curated title")
    summary = runtime.summarize_session(session_id)
    assert summary["title"] == "curated title"


def test_runtime_summarize_title_fallback_to_session_id(tmp_path):
    """summarize_session falls back to Session {id[:8]} when no user/title."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    # Only a context event, no user event
    runtime.session_store.append_event(session_id, {"type": "context", "payload": {}})
    summary = runtime.summarize_session(session_id)
    assert summary["title"] == f"Session {session_id[:8]}"


def test_runtime_load_events_filters_by_after(tmp_path):
    """Filter events by timestamp when loading."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "one"}})
    time.sleep(0.002)
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "two"}})
    events = runtime.load_events(session_id)
    after = events[0]["ts"]
    filtered = runtime.load_events(session_id, after)
    assert len(filtered) == 1
    assert filtered[0]["payload"]["text"] == "two"


def test_runtime_start_async_and_cancel(tmp_path):
    """Start and cancel an async run; start_async returns the minted run_id."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        runtime.session_store.append_event(
            session_id, {"type": "user", "payload": {"text": user_query}}
        )
        while should_cancel and not should_cancel():
            time.sleep(0.01)
        runtime.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "canceled", "task_result": None},
            },
        )

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    run_id = runtime.start_async(session_id=session_id, user_query="hello")
    # The first run mints "<session_id>:r1"; resolvable back to session_id.
    assert run_id == f"{session_id}:r1"
    assert run_id.split(":", 1)[0] == session_id
    assert runtime.is_running(session_id) is True
    assert runtime.cancel(session_id) is True

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not runtime.is_running(session_id):
            break
        time.sleep(0.01)
    assert runtime.is_running(session_id) is False


def test_runtime_start_async_runid_increments_per_turn(tmp_path):
    """Each run on a session mints a fresh 1-based ``:r<seq>`` run_id."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        # Mirror production: the run appends its own user event.
        runtime.session_store.append_event(
            session_id, {"type": "user", "payload": {"text": user_query}}
        )

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()

    first = runtime.start_async(session_id=session_id, user_query="one")
    assert first == f"{session_id}:r1"
    _wait_idle(runtime, session_id)

    second = runtime.start_async(session_id=session_id, user_query="two")
    assert second == f"{session_id}:r2"
    _wait_idle(runtime, session_id)


def test_runtime_start_async_returns_empty_when_busy(tmp_path):
    """A concurrent start is refused with a falsy empty run_id (busy contract)."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    first = runtime.start_async(session_id=session_id, user_query="busy")
    assert first  # truthy run_id
    try:
        # Registry refuses a second concurrent run → empty string (falsy) so
        # existing ``if not started`` callers still detect the refusal.
        second = runtime.start_async(session_id=session_id, user_query="again")
        assert second == ""
    finally:
        runtime.cancel(session_id)
        _wait_idle(runtime, session_id)


def test_runtime_start_async_forwards_structured_params(tmp_path):
    """start_async threads extra_session_tools/approval_callback/strict_tool_scope."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    captured: dict = {}
    done = threading.Event()

    def fake_run_sync(**kwargs):
        captured.update(kwargs)
        done.set()

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()

    sentinel_tool = object()

    def my_callback(*_a, **_k):
        return True

    runtime.start_async(
        session_id=session_id,
        user_query="hi",
        extra_session_tools=[sentinel_tool],
        approval_callback=my_callback,
        strict_tool_scope=True,
    )
    assert done.wait(2.0)
    assert captured["extra_session_tools"] == [sentinel_tool]
    assert captured["approval_callback"] is my_callback
    assert captured["strict_tool_scope"] is True
    _wait_idle(runtime, session_id)


def test_start_async_emits_run_accepted_before_registry_build(tmp_path, monkeypatch):
    """``run_accepted`` is emitted BEFORE the run thread builds the registry.

    ``orchestrate_session`` is where ``Orchestrator.__init__`` builds the tool
    registry + discovers project instructions. We capture the transcript at the
    moment it is invoked: ``run_accepted`` must already be present, proving it was
    emitted ahead of that heavy synchronous setup so the FE can render the shell
    instantly. We patch ``orchestrate_session`` (not ``run_sync``) so the real
    ``run_sync`` path threads the marker.
    """
    import mewbo_core.session_runtime as sr
    from mewbo_core.classes import TaskQueue

    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    seen: dict = {}
    reached_build = threading.Event()

    def fake_orchestrate_session(**kwargs):
        sid = kwargs["session_id"]
        seen["types"] = [e.get("type") for e in store.load_transcript(sid)]
        reached_build.set()
        return TaskQueue(action_steps=[])

    monkeypatch.setattr(sr, "orchestrate_session", fake_orchestrate_session)

    run_id = runtime.start_async(session_id=session_id, user_query="hello")
    assert run_id == f"{session_id}:r1"

    # The marker is appended synchronously by start_async, before the thread runs.
    transcript = store.load_transcript(session_id)
    accepted = [e for e in transcript if e.get("type") == "run_accepted"]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["run_id"] == run_id
    assert accepted[0]["payload"]["session_id"] == session_id
    assert accepted[0]["payload"]["ts"]

    assert reached_build.wait(2.0)
    _wait_idle(runtime, session_id)
    # The registry-building call saw run_accepted already in the transcript.
    assert "run_accepted" in seen["types"]


def test_start_async_threads_attachments_to_orchestrate_session(tmp_path, monkeypatch):
    """``start_async``/``run_sync`` forward ``attachments`` untouched.

    Mirrors ``test_start_async_emits_run_accepted_before_registry_build``: patch
    ``orchestrate_session`` (not ``run_sync``) so the REAL ``run_sync`` threading
    is exercised without paying for a real ``Orchestrator`` build.
    """
    import mewbo_core.session_runtime as sr
    from mewbo_core.classes import TaskQueue

    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    descriptors = [{"id": "a1", "filename": "note.txt"}]
    captured: dict = {}

    def fake_orchestrate_session(**kwargs):
        captured.update(kwargs)
        return TaskQueue(action_steps=[])

    monkeypatch.setattr(sr, "orchestrate_session", fake_orchestrate_session)

    runtime.start_async(session_id=session_id, user_query="hello", attachments=descriptors)
    _wait_idle(runtime, session_id)

    assert captured.get("attachments") == descriptors


def test_start_async_without_attachments_passes_none(tmp_path, monkeypatch):
    """Backward-compatible default: omitting ``attachments`` forwards ``None``."""
    import mewbo_core.session_runtime as sr
    from mewbo_core.classes import TaskQueue

    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    captured: dict = {}

    def fake_orchestrate_session(**kwargs):
        captured.update(kwargs)
        return TaskQueue(action_steps=[])

    monkeypatch.setattr(sr, "orchestrate_session", fake_orchestrate_session)

    runtime.start_async(session_id=session_id, user_query="hello")
    _wait_idle(runtime, session_id)

    assert captured.get("attachments") is None


def test_start_async_does_not_emit_run_accepted_when_busy(tmp_path):
    """A refused concurrent start emits no second ``run_accepted`` marker."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    first = runtime.start_async(session_id=session_id, user_query="busy")
    assert first
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and not runtime.is_running(session_id):
            time.sleep(0.01)
        second = runtime.start_async(session_id=session_id, user_query="again")
        assert second == ""
        markers = [
            e for e in store.load_transcript(session_id) if e.get("type") == "run_accepted"
        ]
        assert len(markers) == 1  # only the first run's marker
    finally:
        runtime.cancel(session_id)
        _wait_idle(runtime, session_id)


def _wait_idle(runtime, session_id, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and runtime.is_running(session_id):
        time.sleep(0.005)


def test_enqueue_message_persists_user_event(tmp_path):
    """Steering messages enqueued mid-run are persisted as user events."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    runtime.start_async(session_id=session_id, user_query="initial")
    try:
        assert runtime.enqueue_message(session_id, "steer me") is True
        events = store.load_transcript(session_id)
        user_events = [e for e in events if e["type"] == "user_steer"]
        assert any(e["payload"]["text"] == "steer me" for e in user_events)
    finally:
        runtime.cancel(session_id)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(session_id):
            time.sleep(0.01)


def test_enqueue_message_returns_false_when_idle(tmp_path):
    """enqueue_message returns False when no run is active."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    assert runtime.enqueue_message(session_id, "nobody home") is False
    events = store.load_transcript(session_id)
    assert not any(e.get("payload", {}).get("text") == "nobody home" for e in events)


def test_interrupt_step_persists_user_event(tmp_path):
    """Interrupting a step records a user event in the transcript."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    runtime.start_async(session_id=session_id, user_query="initial")
    try:
        assert runtime.interrupt_step(session_id) is True
        events = store.load_transcript(session_id)
        user_events = [e for e in events if e["type"] == "user_steer"]
        assert any("Interrupted" in str(e["payload"]["text"]) for e in user_events)
    finally:
        runtime.cancel(session_id)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(session_id):
            time.sleep(0.01)


def test_runtime_list_sessions_skips_empty(tmp_path):
    """Exclude sessions with no events from list output."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    empty_session = store.create_session()
    store.append_event(empty_session, {"type": "session", "payload": {"event": "created"}})
    filled_session = store.create_session()
    store.append_event(filled_session, {"type": "user", "payload": {"text": "hello"}})

    sessions = runtime.list_sessions()
    session_ids = {session["session_id"] for session in sessions}
    assert filled_session in session_ids
    assert empty_session not in session_ids


def test_runtime_list_sessions_filters_archived(tmp_path):
    """Filter archived sessions unless requested."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    active_session = store.create_session()
    archived_session = store.create_session()
    store.append_event(active_session, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(archived_session, {"type": "user", "payload": {"text": "bye"}})
    store.archive_session(archived_session)

    sessions = runtime.list_sessions()
    session_ids = {session["session_id"] for session in sessions}
    assert active_session in session_ids
    assert archived_session not in session_ids

    sessions_with_archived = runtime.list_sessions(include_archived=True)
    all_ids = {session["session_id"] for session in sessions_with_archived}
    assert archived_session in all_ids


# ---------------------------------------------------------------------------
# resolve_recovery_query (Fix E — retry/continue recovery primitive)
# ---------------------------------------------------------------------------


def test_resolve_recovery_query_retry_truncates_failed_turn(tmp_path):
    """``retry`` deletes the failed turn so the session looks like it
    ended right before the failed user message was sent. The returned
    text is the failed user's original query, ready for a fresh run.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "first"}})
    store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {"done": True, "done_reason": "completed", "task_result": "ok"},
        },
    )
    store.append_event(session_id, {"type": "user", "payload": {"text": "second"}})
    store.append_event(
        session_id,
        {
            "type": "tool_result",
            "payload": {"tool_id": "x", "operation": "get", "tool_input": "", "result": "ok"},
        },
    )
    store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {
                "done": True,
                "done_reason": "error",
                "task_result": None,
                "error": "boom",
            },
        },
    )

    query = runtime.resolve_recovery_query(session_id, "retry")
    assert query == "second"
    # The failed turn (user "second" + tool_result + completion) must be gone.
    # Only the first successful turn (user "first" + completion) remains.
    transcript = store.load_transcript(session_id)
    types = [e["type"] for e in transcript]
    assert "user" in types
    user_events = [e for e in transcript if e["type"] == "user"]
    assert len(user_events) == 1
    assert user_events[0]["payload"]["text"] == "first"
    # No recovery audit event — retry deletes, doesn't append.
    assert not any(e["type"] == "recovery" for e in transcript)


def test_resolve_recovery_query_continue_preserves_interrupted_turn(tmp_path):
    """``continue`` keeps an interrupted turn that emitted NO completion.

    A run killed mid-flight (api restart) writes its tool work but never a
    ``completion`` for that turn. Anchoring the truncation on the LAST completion
    fell back to the PREVIOUS completed turn and deleted the entire interrupted
    turn — its user message and every tool call/result — which the console then
    rendered as "prior traces hidden". The continuation must resume from the
    intact turn, so those traces have to survive.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    # Turn 1 — completed cleanly.
    store.append_event(session_id, {"type": "user", "payload": {"text": "T1"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
    )
    # Turn 2 — interrupted mid-run: tool work, NO completion.
    store.append_event(session_id, {"type": "user", "payload": {"text": "T2"}})
    store.append_event(
        session_id,
        {"type": "tool_result", "payload": {"tool_id": "scg_memory", "result": "ok"}},
    )

    runtime.resolve_recovery_query(session_id, "continue")

    transcript = store.load_transcript(session_id)
    user_texts = [e["payload"]["text"] for e in transcript if e["type"] == "user"]
    assert user_texts == ["T1", "T2"]  # the interrupted turn's user message survives
    assert any(
        e["type"] == "tool_result"
        and e["payload"].get("tool_id") == "scg_memory"
        for e in transcript
    ), "interrupted turn's tool traces must be preserved"


def test_resolve_recovery_query_continue_drops_stale_prior_attempt(tmp_path):
    """A SECOND ``continue`` drops the stale prior recovery attempt + its turn.

    The truncation's real job: keep the transcript from accreting duplicate
    recovery prompts. A prior ``recovery`` marker whose attempt produced nothing
    durable is cut from just before the marker — removing the unanswered
    synthetic continue-turn while keeping the real work that preceded it.

    The marker alone is not enough: an attempt that emitted an ``assistant``
    message, a ``tool_result`` or a ``completion`` is never cut, so this fixture
    leaves the synthetic re-prompt genuinely unanswered. See
    ``tests/test_recovery_truncation.py`` for the durable-work side.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "real work"}})
    store.append_event(
        session_id,
        {"type": "tool_result", "payload": {"tool_id": "x", "result": "ok"}},
    )
    # A prior continue attempt: its recovery marker + the synthetic turn it drove.
    store.append_event(session_id, {"type": "recovery", "payload": {"action": "continue"}})
    store.append_event(
        session_id, {"type": "user", "payload": {"text": "stale continue prompt"}}
    )

    runtime.resolve_recovery_query(session_id, "continue")

    transcript = store.load_transcript(session_id)
    user_texts = [e["payload"]["text"] for e in transcript if e["type"] == "user"]
    # The stale continue prompt is gone; the real work survives.
    assert "stale continue prompt" not in user_texts
    assert "real work" in user_texts
    assert any(e["type"] == "tool_result" for e in transcript)


def test_resolve_recovery_query_continue_generic_prompt(tmp_path):
    """``continue`` returns a terse, stable recovery prompt.

    The original task is carried forward via ContextBuilder.recent_events
    (which anchors the first user event) and the compaction summary. The
    HumanMessage stays small to preserve the cache prefix for prompt caching.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    original_task = "build a KISS-compliant auth layer"
    store.append_event(session_id, {"type": "user", "payload": {"text": original_task}})

    query = runtime.resolve_recovery_query(session_id, "continue")
    assert "interrupted" in query.lower()
    assert original_task not in query  # task is NOT re-embedded; carried via ContextBuilder
    assert "continue from where you left off" in query.lower()
    transcript = store.load_transcript(session_id)
    recovery_events = [e for e in transcript if e.get("type") == "recovery"]
    assert recovery_events[-1]["payload"] == {"action": "continue"}


def test_resolve_recovery_query_continue_is_stable_across_tasks(tmp_path):
    """The ``continue`` prompt is independent of the user's task text.

    Stability matters for prompt caching: a deterministic HumanMessage
    means the model provider's cache prefix shape doesn't drift between
    sessions with different original tasks.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    first = runtime.resolve_session()
    store.append_event(first, {"type": "user", "payload": {"text": "task A"}})
    second = runtime.resolve_session()
    store.append_event(second, {"type": "user", "payload": {"text": "an entirely different task"}})

    assert runtime.resolve_recovery_query(first, "continue") == runtime.resolve_recovery_query(
        second, "continue"
    )


def test_resolve_recovery_query_continue_falls_back_without_original(tmp_path):
    """Whitespace-only original user event still yields the generic prompt."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "   "}})

    query = runtime.resolve_recovery_query(session_id, "continue")
    assert "interrupted" in query.lower()
    assert "continue from where you left off" in query.lower()


def test_resolve_recovery_query_rejects_unknown_action(tmp_path):
    """Bad action raises ValueError — nothing appended."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    with pytest.raises(ValueError, match="unknown recovery action"):
        runtime.resolve_recovery_query(session_id, "nuke")
    # No recovery event appended on failure.
    assert not any(e.get("type") == "recovery" for e in store.load_transcript(session_id))


def test_resolve_recovery_query_rejects_session_with_no_user_message(tmp_path):
    """No prior user event ⇒ ValueError."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "context", "payload": {"x": 1}})

    with pytest.raises(ValueError, match="no prior user message"):
        runtime.resolve_recovery_query(session_id, "retry")


def test_resolve_session_fork_at_ts(tmp_path):
    """``fork_at_ts`` creates a session with only events up to the cutoff."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    source = runtime.resolve_session()
    store.append_event(source, {"type": "user", "payload": {"text": "q1"}})
    store.append_event(source, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(source, {"type": "user", "payload": {"text": "q2"}})
    events = store.load_transcript(source)
    cutoff = events[1]["ts"]  # after first assistant response

    forked = runtime.resolve_session(fork_from=source, fork_at_ts=cutoff)
    assert forked != source
    forked_events = store.load_transcript(forked)
    assert len(forked_events) == 2
    assert forked_events[-1]["payload"]["text"] == "a1"


def test_resolve_session_fork_at_ts_ignored_without_fork_from(tmp_path):
    """``fork_at_ts`` without ``fork_from`` creates a fresh session."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session = runtime.resolve_session(fork_at_ts="2099-01-01T00:00:00+00:00")
    assert store.load_transcript(session) == []


def test_resolve_recovery_query_with_replacement_text(tmp_path):
    """``replacement_text`` overrides the original user message on retry."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "original"}})
    store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {"done": True, "done_reason": "error", "task_result": None, "error": "fail"},
        },
    )

    query = runtime.resolve_recovery_query(session_id, "retry", replacement_text="edited prompt")
    assert query == "edited prompt"
    # Transcript should be truncated (original user message removed)
    transcript = store.load_transcript(session_id)
    assert not any(e["type"] == "user" for e in transcript)


def test_resolve_recovery_query_replacement_text_on_empty_original(tmp_path):
    """``replacement_text`` works even when the original message was empty."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": ""}})

    query = runtime.resolve_recovery_query(session_id, "retry", replacement_text="fixed prompt")
    assert query == "fixed prompt"


def test_resolve_recovery_query_refuses_running_session(tmp_path):
    """Attempting recovery on a running session raises RuntimeError."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    runtime.start_async(session_id=session_id, user_query="hi")
    try:
        with pytest.raises(RuntimeError, match="running"):
            runtime.resolve_recovery_query(session_id, "retry")
    finally:
        runtime.cancel(session_id)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(session_id):
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Recovery capability re-injection (Part F1)
# ---------------------------------------------------------------------------


def test_reinject_recovery_context_reemits_client_capabilities(tmp_path):
    """A recovered session whose original context declared ``client_capabilities``
    re-emits it as the most-recent context event, so capability-gated AgentDefs
    (wiki-*, etc.) still resolve after recovery.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.append_context_event(session_id, {"client_capabilities": ["wiki"]})
    store.append_event(session_id, {"type": "user", "payload": {"text": "q"}})
    # Simulate a later gating-less context event (e.g. a model override on recovery)
    runtime.append_context_event(session_id, {"model": "gpt-4o"})

    runtime.reinject_recovery_context(session_id)

    events = store.load_transcript(session_id)
    last_context = next(
        (e for e in reversed(events) if e.get("type") == "context"), None
    )
    assert last_context is not None
    assert last_context["payload"].get("client_capabilities") == ["wiki"]


def test_reinject_recovery_context_reemits_structured_workspace(tmp_path):
    """``structured_workspace`` (the grounded-structured slug) survives recovery."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.append_context_event(
        session_id, {"structured_workspace": "my-repo", "client_capabilities": ["wiki"]}
    )
    runtime.append_context_event(session_id, {"model": "gpt-4o"})

    runtime.reinject_recovery_context(session_id)

    events = store.load_transcript(session_id)
    last_context = next(
        (e for e in reversed(events) if e.get("type") == "context"), None
    )
    assert last_context is not None
    assert last_context["payload"].get("structured_workspace") == "my-repo"
    assert last_context["payload"].get("client_capabilities") == ["wiki"]


def test_reinject_recovery_context_noop_without_gating_fields(tmp_path):
    """A session that never declared a gating field gets no extra context event."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.append_context_event(session_id, {"model": "gpt-4o"})
    before = len(store.load_transcript(session_id))

    runtime.reinject_recovery_context(session_id)

    assert len(store.load_transcript(session_id)) == before


def test_reinject_recovery_context_uses_latest_gating_value(tmp_path):
    """When the gating field changed across events, the LATEST value wins."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.append_context_event(session_id, {"client_capabilities": ["wiki"]})
    runtime.append_context_event(session_id, {"client_capabilities": ["wiki", "scg"]})

    runtime.reinject_recovery_context(session_id)

    events = store.load_transcript(session_id)
    last_context = next(
        (e for e in reversed(events) if e.get("type") == "context"), None
    )
    assert last_context is not None
    assert last_context["payload"].get("client_capabilities") == ["wiki", "scg"]


# ---------------------------------------------------------------------------
# ``recoverable`` flag (Part F2)
# ---------------------------------------------------------------------------


def _append_completion(store, session_id, *, done, done_reason):
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": done, "done_reason": done_reason}},
    )


def test_recoverable_true_when_failed(tmp_path):
    """A failed session with a prior user turn is recoverable."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    _append_completion(store, session_id, done=True, done_reason="error")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "failed"
    assert summary["recoverable"] is True


def test_recoverable_true_when_canceled(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    _append_completion(store, session_id, done=False, done_reason="canceled")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "canceled"
    assert summary["recoverable"] is True


def test_recoverable_true_when_incomplete(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    _append_completion(store, session_id, done=False, done_reason="max_steps_reached")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "incomplete"
    assert summary["recoverable"] is True


def test_recoverable_true_when_budget_exhausted(tmp_path):
    """Graduated exhaustion finishes the run cleanly (``done=True``,
    a forced wrap-up turn already ran) but never reached natural completion —
    same ``incomplete``/recoverable treatment as ``max_steps_reached``."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    _append_completion(store, session_id, done=True, done_reason="budget_exhausted")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "incomplete"
    assert summary["recoverable"] is True


def test_recoverable_true_when_halted_agent_budget(tmp_path):
    """Forward-compat: a future per-agent contract-budget halt maps the same way."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    _append_completion(store, session_id, done=True, done_reason="halted_agent_budget")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "incomplete"
    assert summary["recoverable"] is True


def test_recoverable_true_when_died_without_completion(tmp_path):
    """The real failure mode: process killed mid-call, NO completion event.

    Status stays ``idle`` but a user turn exists — must be recoverable.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    # No completion event at all (process killed).
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "idle"
    assert summary["recoverable"] is True


def test_recoverable_false_when_completed(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "do it"}})
    _append_completion(store, session_id, done=True, done_reason="completed")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "completed"
    assert summary["recoverable"] is False


def test_recoverable_true_when_awaiting_approval(tmp_path):
    """A session parked in ``awaiting_approval`` is recoverable — recovery
    ``continue`` is the auto-exit, mirroring send_followup re-engagement."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "plan it"}})
    _append_completion(store, session_id, done=True, done_reason="awaiting_approval")
    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "awaiting_approval"
    assert summary["recoverable"] is True


def test_recoverable_false_without_user_turn(tmp_path):
    """No prior user message ⇒ resolve_recovery_query would raise ⇒ not recoverable."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "context", "payload": {"model": "x"}})
    summary = runtime.summarize_session(session_id)
    assert summary["recoverable"] is False


def test_recoverable_false_when_running(tmp_path):
    """A running session is never recoverable (it hasn't failed yet)."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    runtime.start_async(session_id=session_id, user_query="hi")
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and not runtime.is_running(session_id):
            time.sleep(0.01)
        summary = runtime.summarize_session(session_id)
        assert summary["status"] == "running"
        assert summary["recoverable"] is False
    finally:
        runtime.cancel(session_id)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(session_id):
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Permanent termination (WP2)
# ---------------------------------------------------------------------------


def test_is_terminated_choke_point(tmp_path):
    """runtime.is_terminated reflects the store's terminated stamp."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    assert runtime.is_terminated(session_id) is False
    store.terminate_session(session_id)
    assert runtime.is_terminated(session_id) is True


def test_terminate_session_shape_and_event(tmp_path):
    """First terminate returns the 200 shape and appends a session_terminated event."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    result = runtime.terminate_session(session_id)
    assert result["session_id"] == session_id
    assert result["status"] == "terminated"
    assert result["terminated_at"] is not None
    assert result["cancelled_triggers"] == 0

    events = store.load_transcript(session_id)
    marker = next((e for e in events if e.get("type") == "session_terminated"), None)
    assert marker is not None
    assert marker["payload"]["terminated_at"] == result["terminated_at"]


def test_terminate_session_idempotent_repeat(tmp_path):
    """A repeat terminate returns the ORIGINAL timestamp and appends no 2nd event."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    first = runtime.terminate_session(session_id)
    second = runtime.terminate_session(session_id)
    assert second["terminated_at"] == first["terminated_at"]
    assert second["cancelled_triggers"] == 0

    markers = [
        e for e in store.load_transcript(session_id) if e.get("type") == "session_terminated"
    ]
    assert len(markers) == 1


def test_terminate_on_terminate_callbacks_fire_once(tmp_path):
    """on_terminate callbacks run exactly once on the first terminate, summed."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    seen: list[str] = []

    def cb_two(sid: str) -> int:
        seen.append(sid)
        return 2

    def cb_three(sid: str) -> int:
        return 3

    runtime.register_on_terminate(cb_two)
    runtime.register_on_terminate(cb_three)

    result = runtime.terminate_session(session_id)
    assert result["cancelled_triggers"] == 5  # 2 + 3
    assert seen == [session_id]

    # Idempotent repeat must NOT re-fire callbacks.
    repeat = runtime.terminate_session(session_id)
    assert repeat["cancelled_triggers"] == 0
    assert seen == [session_id]


def test_terminate_callback_failure_is_isolated(tmp_path):
    """A raising callback is skipped, never blocking termination or later callbacks."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()

    def boom(_sid: str) -> int:
        raise RuntimeError("cascade blew up")

    def ok(_sid: str) -> int:
        return 4

    runtime.register_on_terminate(boom)
    runtime.register_on_terminate(ok)
    result = runtime.terminate_session(session_id)
    # boom contributes nothing; ok still runs and counts.
    assert result["cancelled_triggers"] == 4
    assert runtime.is_terminated(session_id) is True


def test_terminate_session_side_effects_gated_by_store_return(tmp_path, monkeypatch):
    """Side effects run only when the STORE reports it newly stamped the timestamp.

    Models the losing side of a concurrency race (fix): the store
    already holds a real ``terminated_at`` (a concurrent winner stamped it
    out of band), yet the runtime must gate its cancel/callback/event side
    effects on the store's own set-once return value, never on its own
    unlocked ``get_terminated_at`` read — else two concurrent FIRST callers
    could both observe "not yet terminated" and both run the side effects.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.terminate_session(session_id)  # out-of-band winner

    seen: list[str] = []
    runtime.register_on_terminate(lambda sid: seen.append(sid) or 1)
    monkeypatch.setattr(store, "terminate_session", lambda _sid: False)

    result = runtime.terminate_session(session_id)

    assert result["cancelled_triggers"] == 0
    assert seen == []
    markers = [
        e for e in store.load_transcript(session_id) if e.get("type") == "session_terminated"
    ]
    assert markers == []


def test_summarize_terminated_beats_completion(tmp_path):
    """terminated wins over a completed run and is never recoverable."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
    )
    runtime.terminate_session(session_id)

    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "terminated"
    assert summary["terminated"] is True
    assert summary["terminated_at"] is not None
    assert summary["recoverable"] is False


def test_summarize_terminated_beats_running(tmp_path, monkeypatch):
    """terminated wins even over a still-unwinding live run (cancel is cooperative)."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    store.terminate_session(session_id)
    # Force is_running True to model the window between terminate and loop unwind.
    monkeypatch.setattr(runtime, "is_running", lambda sid: True)

    summary = runtime.summarize_session(session_id)
    assert summary["status"] == "terminated"
    assert summary["recoverable"] is False
