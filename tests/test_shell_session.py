"""Tests for long-lived shell sessions: start, read, write, kill, and reaping.

The store and the buffer take their bounds as constructor arguments, so every
capacity and TTL case here runs against a purpose-built instance rather than the
process-wide singleton — no monkeypatching, no sleeping out a real hour.
"""

from __future__ import annotations

import time

import pytest
from mewbo_core.classes import ActionStep
from mewbo_tools.integration.shell_session import (
    OutputBuffer,
    ShellSession,
    ShellSessionStore,
    ShellStartArgs,
)
from mewbo_tools.integration.shell_session_tool import ShellSessionTool


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* until it holds or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class FakeClock:
    """A clock whose only source of time is the test that holds it."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, as the store and its sessions will read it."""
        self.now += seconds


@pytest.fixture
def store():
    """A private store, torn down so no test leaves a process behind."""
    instance = ShellSessionStore()
    yield instance
    instance.shutdown()


def start(store, command: str, cwd, **kwargs) -> ShellSession:
    """Start a background session under *store*."""
    args = ShellStartArgs(command=command, run_in_background=True, **kwargs)
    return store.create(args, str(cwd))


# -- OutputBuffer ---------------------------------------------------------


def test_buffer_reads_only_what_is_new():
    """A cursor read returns the tail written since that cursor, and no more."""
    buffer = OutputBuffer()
    buffer.append("alpha")
    text, cursor, missed = buffer.read_from(0)
    assert (text, missed) == ("alpha", 0)
    buffer.append("beta")
    text, cursor, missed = buffer.read_from(cursor)
    assert (text, missed) == ("beta", 0)
    # Nothing new: an empty read, not a repeat of what was already delivered.
    assert buffer.read_from(cursor)[0] == ""


def test_buffer_drops_from_the_front_and_reports_the_gap():
    """Overflow evicts the oldest text and a late reader is TOLD it missed some."""
    buffer = OutputBuffer(capacity=10)
    buffer.append("0123456789")
    buffer.append("abcde")
    text, _, missed = buffer.read_from(0)
    assert missed == 5, "a reader starting at 0 lost the first five characters"
    assert text == "56789abcde"
    assert buffer.dropped == 5
    assert buffer.total_written == 15


def test_buffer_cursor_is_absolute_over_the_whole_stream():
    """The cursor counts everything ever written, not the retained window."""
    buffer = OutputBuffer(capacity=4)
    for chunk in ("aa", "bb", "cc"):
        buffer.append(chunk)
    assert buffer.total_written == 6
    assert buffer.read_from(6)[0] == ""


# -- session lifecycle ----------------------------------------------------


def test_background_session_reports_running_then_exited(store, tmp_path):
    """Liveness comes from the process handle, never from whether output exists."""
    session = start(store, "sleep 0.3; echo done", tmp_path)
    assert session.status == "running"
    assert wait_until(lambda: session.status == "exited")
    assert session.exit_code == 0
    assert "done" in str(session.read()["output"])


def test_silent_process_is_still_reported_as_running(store, tmp_path):
    """A process that has emitted nothing yet is alive, not absent.

    This is the failure qwen-code keeps a status sidecar for: a block-buffered
    child can print nothing for its whole run, so an empty read must never be
    read as "finished" or "dead".
    """
    session = start(store, "sleep 2", tmp_path)
    assert session.read()["output"] == ""
    assert session.status == "running"
    assert session.exit_code is None


def test_incremental_read_across_turns(store, tmp_path):
    """Passing the previous cursor back yields only what arrived since."""
    session = start(store, "echo one; sleep 0.4; echo two", tmp_path)
    assert wait_until(lambda: "one" in str(session.read()["output"]))
    first = session.read()
    assert "one" in str(first["output"])
    assert wait_until(lambda: session.status == "exited")
    second = session.read(cursor=int(first["cursor"]))
    assert "two" in str(second["output"])
    assert "one" not in str(second["output"]), "already-delivered output was repeated"


def test_read_can_wait_briefly_for_output(store, tmp_path):
    """wait_ms blocks for late output instead of returning an empty read."""
    session = start(store, "sleep 0.3; echo late", tmp_path)
    payload = session.read(wait_ms=3000)
    assert "late" in str(payload["output"])


def test_filter_is_display_only_and_never_consumes(store, tmp_path):
    """A filtered read leaves the cursor where an unfiltered one would."""
    session = start(store, "printf 'keep\\ndrop\\n'", tmp_path)
    assert wait_until(lambda: session.status == "exited")
    filtered = session.read(pattern="keep")
    assert "drop" not in str(filtered["output"])
    assert "keep" in str(filtered["output"])
    # The unfiltered text is still retrievable from the same starting point.
    assert "drop" in str(session.read()["output"])


def test_write_drives_a_program_through_stdin(store, tmp_path):
    """Input sent to a running session reaches the program and its reply comes back."""
    session = start(store, "read name; echo hello $name", tmp_path)
    session.write("world")
    assert wait_until(lambda: "hello world" in str(session.read()["output"]))


def test_write_drives_an_interactive_program_on_a_tty(store, tmp_path):
    """With tty=True the program sees a terminal and can still be driven."""
    session = start(store, "read -p 'name: ' n; echo got-$n", tmp_path, tty=True)
    assert wait_until(lambda: "name:" in str(session.read()["output"]))
    session.write("mewbo")
    assert wait_until(lambda: "got-mewbo" in str(session.read()["output"]))


def test_write_to_an_exited_session_refuses(store, tmp_path):
    """A vanishing write is worse than a refusal the model can read."""
    session = start(store, "true", tmp_path)
    assert wait_until(lambda: session.status == "exited")
    with pytest.raises(ValueError, match="already exited"):
        session.write("anything")


def test_kill_reaps_the_whole_process_group(store, tmp_path):
    """Killing a session takes the children it spawned with it."""
    marker = tmp_path / "survived.txt"
    session = start(store, f"sleep 30 && touch {marker}", tmp_path)
    session.kill()
    assert wait_until(lambda: session.status == "exited")
    time.sleep(0.5)
    assert not marker.exists(), "a descendant outlived the kill as an orphan"


# -- store bounds ---------------------------------------------------------


def test_store_refuses_rather_than_killing_live_work(tmp_path):
    """At capacity with everything running, a new start is REFUSED.

    Evicting here would silently stop work nobody asked to stop. The refusal
    names the cap and how to free a slot, so the model can act on it in one turn
    rather than guessing.
    """
    store = ShellSessionStore(max_sessions=2)
    try:
        start(store, "sleep 30", tmp_path)
        start(store, "sleep 30", tmp_path)
        with pytest.raises(ValueError) as excinfo:
            start(store, "sleep 30", tmp_path)
        message = str(excinfo.value)
        assert "limit of 2" in message
        assert "kill" in message
    finally:
        store.shutdown()


def test_store_evicts_terminal_sessions_to_make_room(tmp_path):
    """A finished session yields its slot; a running one does not."""
    store = ShellSessionStore(max_sessions=2)
    try:
        finished = start(store, "true", tmp_path)
        assert wait_until(lambda: finished.status == "exited")
        start(store, "sleep 30", tmp_path)
        newest = start(store, "sleep 30", tmp_path)
        assert newest.status == "running"
        assert finished.shell_id not in [row["shell_id"] for row in store.list()]
    finally:
        store.shutdown()


def test_store_reaps_an_idle_running_session(tmp_path):
    """A session nobody has touched past its TTL is killed, not accumulated.

    The deadline is REACHED by setting an instant, not by waiting one out: the
    store and the sessions it creates share this clock, so advancing it moves
    both sides of the ``now - last_used`` comparison the TTL is made of.
    """
    clock = FakeClock()
    store = ShellSessionStore(idle_ttl_s=300.0, clock=clock)
    try:
        session = start(store, "sleep 30", tmp_path)
        store.reap()
        assert session.status == "running", "reaped before its TTL was reached"
        clock.advance(301.0)
        store.reap()
        # The process dying is real OS work — the only wait left is on that.
        assert wait_until(lambda: session.status == "exited")
    finally:
        store.shutdown()


def test_reading_a_session_defers_its_reaping(tmp_path):
    """Touching a session resets its idle clock, so active work is never reaped."""
    clock = FakeClock()
    store = ShellSessionStore(idle_ttl_s=300.0, clock=clock)
    try:
        session = start(store, "sleep 30", tmp_path)
        # Four TTLs' worth of time passes; each read defers the deadline again.
        for _ in range(4):
            clock.advance(299.0)
            session.read()
            store.reap()
        assert session.status == "running"
    finally:
        store.shutdown()


def test_unknown_shell_id_names_the_live_ones(store, tmp_path):
    """A rejection tells the model what IS available, never just what is not."""
    session = start(store, "sleep 5", tmp_path)
    with pytest.raises(ValueError) as excinfo:
        store.get("shell_999")
    assert session.shell_id in str(excinfo.value)


def test_shutdown_kills_every_session(tmp_path):
    """Nothing outlives the store — this is what atexit relies on."""
    store = ShellSessionStore()
    session = start(store, "sleep 30", tmp_path)
    store.shutdown()
    assert wait_until(lambda: session.status == "exited")
    assert store.list() == []


# -- tool surface ---------------------------------------------------------


def call_tool(**tool_input: object) -> object:
    """Invoke the session tool once and return whatever it put on the wire."""
    step = ActionStep(
        tool_id="shell_session_tool",
        operation="set",
        tool_input=tool_input,
    )
    return ShellSessionTool().set_state(step).content


def test_tool_rejects_an_unknown_argument():
    """extra="forbid" turns a smuggled field into a readable refusal."""
    content = call_tool(operation="list", nonsense=1)
    assert isinstance(content, str)
    assert "Invalid arguments" in content


def test_refusal_names_the_offending_field_and_the_expected_set():
    """The refusal must close the gap, not merely report it.

    'Extra inputs are not permitted' WITHOUT the field name kept a real agent
    looping for five calls — it could not tell which of the keys it sent was
    the wrong one. A Claude-Code-shaped agent definition gets this tool via a
    NAME-only alias (`BashOutput` → `shell_session_tool`) and sends `bash_id`,
    so a single refusal must be enough to re-derive the whole contract.
    """
    content = call_tool(operation="read", bash_id="shell_3")
    assert isinstance(content, str)
    assert "bash_id" in content, "the refusal must name WHICH input was extra"
    assert "Expected fields" in content
    assert "shell_id" in content
    assert "wait_ms (0..30000)" in content
    assert "'read'|'write'|'kill'|'list'" in content


def test_refusal_reports_every_error_not_just_the_first():
    """Two mistakes in one call surface together, so one round trip fixes both."""
    content = call_tool(operation="read", shell_id="s", wait_ms=60000, timeout=30)
    assert isinstance(content, str)
    assert "wait_ms" in content and "less than or equal to 30000" in content
    assert "timeout" in content and "Extra inputs are not permitted" in content


def test_tool_rejects_an_uncompilable_filter():
    """A bad regex is refused at the boundary, not raised from inside a read."""
    content = call_tool(operation="read", shell_id="shell_1", filter="([")
    assert isinstance(content, str)
    assert "not a valid regular expression" in content


def test_tool_requires_a_shell_id_for_targeted_operations():
    """Operations that name a session say so when it is missing."""
    content = call_tool(operation="read")
    assert isinstance(content, str)
    assert "shell_id is required" in content


def test_tool_lists_sessions():
    """list needs no handle and always answers with the current set."""
    payload = call_tool(operation="list")
    assert isinstance(payload, dict)
    assert payload["operation"] == "list"
    assert isinstance(payload["sessions"], list)
