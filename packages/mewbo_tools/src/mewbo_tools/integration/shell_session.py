#!/usr/bin/env python3
"""Long-lived shell sessions: background start, incremental read, stdin, kill.

The one process primitive behind BOTH shell surfaces. A foreground call is not a
different mechanism from a background one — it is the same session waited on
until it exits, which is why there is a single :class:`ShellSession` here and no
parallel one-shot path. (Codex's ``unified_exec`` reaches the same conclusion
from the other direction: one ``exec_command`` returns final output when the
process finishes inside its yield window and a session handle when it does not.)

Four verbs ship together — start, read, write, kill — deliberately. opencode
REMOVED model-facing background execution rather than expose a start with no
observation or cancellation ("process-local status is not a sufficient remote
contract"), and that is the right bar: a background start whose output nobody
can read and whose process nobody can stop is a leak wearing a feature's name.
"""

from __future__ import annotations

import atexit
import os
import pty
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Literal, get_args, get_origin

from mewbo_core.workspaces.workspace import get_active_project_root
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_tools.integration.landlock import ShellScope, scoped_preexec

# Environment overlay that keeps every shell invocation non-interactive by
# default: no pager waiting on a keypress, no credential prompt blocking on
# stdin. A PTY session deliberately keeps this too — ``tty=True`` exists so a
# program can be DRIVEN through its prompts, not so ``git log`` can rediscover
# ``less``.
_PAGER_SAFE_ENV = {
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

# Slightly under ToolSpec's default 120s tool-call timeout
# (mewbo_core.tooling.tool_registry.ToolSpec.timeout, enforced by
# ToolUseLoop._safe_execute via asyncio.wait_for) so the subprocess reaps itself
# before the outer timeout fires. Cancelling that asyncio.wait_for only abandons
# the awaiting coroutine — it can't kill a blocking wait — so without a bound
# here the process (and any pager child) would leak as an orphan every time the
# outer timeout fired.
DEFAULT_TIMEOUT_S = 115.0

# Per-session retained output. Sized between qwen-code's 30k model-facing cap and
# Codex's 1 MiB / kilocode's 2 MB process buffers: this is the STORE, not what
# reaches the model, so it holds far more than one read returns.
MAX_BUFFER_CHARS = 1_000_000

# How many sessions may exist at once, and what happens past that (root
# CLAUDE.md: a long-lived resource states both). Terminal sessions are evicted
# least-recently-used to make room; a store full of RUNNING sessions REFUSES a
# new one rather than killing work nobody asked to stop. Codex caps at 64,
# qwen-code retains 32, kilocode 25.
MAX_SESSIONS = 32

# A running session nobody has read from or written to for this long is reaped.
# Checked lazily on every store access rather than by a reaper thread — a thread
# is one more thing that can silently stop running, which is exactly how
# ``shutdown_lsp_managers`` came to be declared-but-never-called.
IDLE_TTL_S = 3600.0

# Grace between SIGTERM and SIGKILL when stopping a process group, matching
# opencode's ``forceKillAfter: "3 seconds"``.
_KILL_GRACE_S = 3.0

# Upper bound on ``read(wait_ms=…)``. Kept well under ToolSpec's 120s ceiling so
# a blocking read can never be the thing that times the tool call out.
MAX_READ_WAIT_MS = 30_000


class ShellStartArgs(BaseModel):
    """Validated arguments for starting a command.

    Crosses the model→process trust boundary, so it is validated at definition
    with unknown fields forbidden rather than read defensively downstream.
    """

    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    root: str | None = None
    timeout: float = Field(default=DEFAULT_TIMEOUT_S, gt=0)
    run_in_background: bool = False
    tty: bool = False

    @field_validator("command")
    @classmethod
    def _command_is_present(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("command is required.")
        return text


class ShellSessionArgs(BaseModel):
    """Validated arguments for operating on an already-started session."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["read", "write", "kill", "list"] = "read"
    shell_id: str | None = None
    input: str = ""
    newline: bool = True
    cursor: int | None = None
    filter: str | None = None
    wait_ms: int = Field(default=0, ge=0, le=MAX_READ_WAIT_MS)

    @field_validator("filter")
    @classmethod
    def _filter_compiles(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"filter is not a valid regular expression: {exc}") from exc
        return value

    @classmethod
    def explain_errors(cls, exc: ValidationError) -> str:
        """Render EVERY error with its field path, then the expected field set.

        ``exc.errors()[0]["msg"]`` alone is the refusal that kept a real agent
        looping: "Extra inputs are not permitted" without ``loc`` never says
        WHICH key was extra, so the only strategy left is dropping keys at
        random. Naming the whole contract also covers the caller this tool is
        aliased to serve — a Claude-Code-shaped agent definition maps
        ``BashOutput``/``KillShell`` onto this tool by NAME only, so its model
        arrives speaking another vocabulary (``bash_id``) and must be able to
        re-derive the real one from a single refusal.
        """
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '(arguments)'}: "
            f"{err['msg']}"
            for err in exc.errors()
        )
        return f"{problems}. Expected fields: {cls.describe_fields()}."

    @classmethod
    def describe_fields(cls) -> str:
        """The accepted argument names with their choices/bounds, derived.

        Read off ``model_fields`` rather than hand-written, so a field added to
        this model appears in the refusal for free instead of drifting.
        """
        parts: list[str] = []
        for name, field in cls.model_fields.items():
            detail = ""
            annotation = field.annotation
            if get_origin(annotation) is Literal:
                detail = "|".join(repr(v) for v in get_args(annotation))
                detail = f" ({detail})"
            else:
                lower = next(
                    (m.ge for m in field.metadata if getattr(m, "ge", None) is not None),
                    None,
                )
                upper = next(
                    (m.le for m in field.metadata if getattr(m, "le", None) is not None),
                    None,
                )
                if lower is not None or upper is not None:
                    detail = f" ({lower}..{upper})"
            parts.append(f"{name}{detail}")
        return ", ".join(parts)


class OutputBuffer:
    """A process's output, bounded, with an ABSOLUTE cursor over the full stream.

    The cursor counts every character ever written, not the buffer's contents, so
    a reader that falls behind learns it MISSED output instead of silently
    receiving a later window as though it were contiguous. Dropping from the
    front rather than refusing to grow is what keeps a chatty long-running
    process readable at its tail, which is the end that carries the outcome.

    Cost: ``O(1)`` amortised per append, ``O(requested window)`` per read.
    """

    def __init__(self, capacity: int = MAX_BUFFER_CHARS) -> None:
        """Create an empty buffer retaining at most *capacity* characters."""
        self._capacity = max(1, capacity)
        self._chunks: list[str] = []
        self._size = 0
        self.total_written = 0
        self.dropped = 0

    def append(self, text: str) -> None:
        """Record *text*, evicting from the front to stay within capacity."""
        if not text:
            return
        self._chunks.append(text)
        self._size += len(text)
        self.total_written += len(text)
        while self._size > self._capacity and self._chunks:
            head = self._chunks[0]
            overflow = self._size - self._capacity
            if len(head) <= overflow:
                self._chunks.pop(0)
                self._size -= len(head)
                self.dropped += len(head)
            else:
                self._chunks[0] = head[overflow:]
                self._size -= overflow
                self.dropped += overflow

    def read_from(self, cursor: int) -> tuple[str, int, int]:
        """Return ``(text, next_cursor, missed)`` for everything after *cursor*.

        *missed* is how many characters were evicted before this reader got to
        them — non-zero means the gap is real and unrecoverable, never that the
        text is merely late.
        """
        start = max(0, min(cursor, self.total_written))
        available_from = self.total_written - self._size
        missed = max(0, available_from - start)
        text = "".join(self._chunks)
        offset = max(0, start - available_from)
        return text[offset:], self.total_written, missed


class ShellSession:
    """One shell process: its handle, its output, and everything done to it.

    Hot in-process runtime state, so a plain atomic class rather than a Pydantic
    model — it crosses no trust boundary (``ShellStartArgs`` already did that on
    the way in) and validating every output chunk would cost the hot path
    without buying anything. Same reasoning as ``RunHandle``.

    ``start_new_session=True`` puts the command in its own process group so a
    kill reaps the command AND anything it spawned (a pager, a build's children)
    instead of orphaning them.

    An optional :class:`ShellScope` confines the child — and everything it
    goes on to spawn — to the directories its agent was granted. It arrives as a
    constructor argument rather than being read from ambient state here, so a
    test can spawn a scoped session without a live agent context.
    """

    def __init__(
        self,
        shell_id: str,
        args: ShellStartArgs,
        cwd: str,
        *,
        scope: ShellScope | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Spawn the command described by *args* in *cwd* and start draining it.

        *clock* is the source of the idle-tracking timestamps only — the store
        that owns this session passes its own, so a TTL can be reached by
        advancing an injected clock instead of waiting out a real one. The
        read deadline keeps its own ``time.monotonic``: that one bounds a wait
        on a real process, which no injected clock can hurry.
        """
        self.shell_id = shell_id
        self.command = args.command
        self.cwd = cwd
        self.tty = args.tty
        self._clock = clock
        self.started_at = clock()
        self.last_used = self.started_at
        self.buffer = OutputBuffer()
        self._lock = threading.Lock()
        self._master_fd: int | None = None
        self._start_error: str | None = None
        self.process: subprocess.Popen[bytes] | None = None

        env = {**os.environ, **_PAGER_SAFE_ENV}
        # The ruleset is created and populated in THIS process; the hook the
        # context manager yields performs only the two syscalls that apply it,
        # between fork and exec. Both branches take it, because a PTY session is
        # no less able to read a sibling project than a piped one.
        enforcement = scoped_preexec(scope)
        try:
            with enforcement as restrict_child:
                self._spawn(args, cwd, env, restrict_child)
        # SubprocessError joins OSError because a preexec_fn that RAISES (rather
        # than exiting) is re-raised by the parent as
        # ``SubprocessError("Exception occurred in preexec_fn.")``, which is not
        # an OSError. Letting it escape would skip the graceful ``_start_error``
        # envelope every other spawn failure gets.
        except (OSError, subprocess.SubprocessError) as exc:
            self._start_error = str(exc)
            self.buffer.append(str(exc))
            return

        self._reader = threading.Thread(
            target=self._drain, name=f"shell-{shell_id}", daemon=True
        )
        self._reader.start()

    def _spawn(
        self,
        args: ShellStartArgs,
        cwd: str,
        env: dict[str, str],
        restrict_child: Callable[[], None] | None,
    ) -> None:
        """Start the process, on a PTY or a pipe, under *restrict_child* if given."""
        if args.tty:
            self._master_fd, slave_fd = pty.openpty()
            # ``finally``, because a Popen that RAISES would otherwise leak the
            # slave descriptor permanently — the session is never registered, so
            # nothing ever reaches ``kill()`` to clean up after it.
            try:
                # A PTY merges stdout and stderr onto one stream by construction;
                # that is the point — it is what the driven program itself sees.
                self.process = subprocess.Popen(
                    args.command,
                    shell=True,
                    cwd=cwd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    env=env,
                    preexec_fn=restrict_child,
                )
            finally:
                os.close(slave_fd)
        else:
            # A FOREGROUND call gets no writable stdin, and that is a
            # correctness rule rather than a saving: it returns no
            # ``shell_id``, so nothing can ever write to it, while an open
            # pipe leaves any stdin-reader (``cat``, a pager, a credential
            # prompt) blocked until the timeout kills it. DEVNULL is what
            # made those return instantly before background sessions
            # existed, and only a session the model can address by handle
            # has a reason to trade that away.
            self.process = subprocess.Popen(
                args.command,
                shell=True,
                cwd=cwd,
                stdin=subprocess.PIPE if args.run_in_background else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                preexec_fn=restrict_child,
            )

    # -- lifecycle ---------------------------------------------------------

    def _drain(self) -> None:
        """Copy the process's output into the buffer until the stream closes.

        A dedicated thread rather than a poll: a block-buffered child can emit
        nothing for its whole run, so LIVENESS MUST NEVER BE INFERRED FROM
        OUTPUT (qwen-code keeps a status sidecar for exactly this reason; here
        the process handle itself is the authority and this thread only moves
        bytes).
        """
        # Read the raw DESCRIPTOR in both modes, never the buffered file object.
        # ``BufferedReader.read(n)`` blocks until it has n bytes or EOF, so a
        # process that prints one line and keeps working delivered NOTHING until
        # it exited — which silently reduced every incremental read to "wait for
        # the end". ``os.read`` returns as soon as any bytes are available,
        # which is the semantics an incremental cursor needs.
        source_fd = self._master_fd
        if source_fd is None:
            stdout = self.process.stdout if self.process is not None else None
            if stdout is None:
                return
            source_fd = stdout.fileno()
        try:
            while True:
                try:
                    raw = os.read(source_fd, 65536)
                except OSError:
                    # The writer closed its end — normal teardown, either the
                    # PTY slave or the pipe.
                    break
                if not raw:
                    break
                with self._lock:
                    self.buffer.append(raw.decode("utf-8", errors="replace"))
        except Exception:  # pragma: no cover - defensive; a drain fault must
            # never propagate onto a daemon thread and take the process with it.
            pass

    @property
    def exit_code(self) -> int | None:
        """The process's exit status, or ``None`` while it is still running."""
        if self._start_error is not None:
            return 1
        if self.process is None:
            return 1
        return self.process.poll()

    @property
    def status(self) -> Literal["running", "exited"]:
        """Whether the process is still alive. Read from the handle, not output."""
        return "running" if self.exit_code is None else "exited"

    def wait(self, timeout: float) -> bool:
        """Block up to *timeout* seconds for exit. ``True`` if it exited."""
        if self.process is None:
            return True
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self._flush()
        return True

    def _flush(self) -> None:
        """Settle the drain thread so the buffer holds everything the process wrote.

        Bounded rather than unbounded: a reader wedged on a descendant that
        inherited the pipe must delay a read, never own it.
        """
        reader = getattr(self, "_reader", None)
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)

    def kill(self) -> None:
        """Terminate the whole process group, SIGTERM then SIGKILL.

        Best-effort side effect at the edge: it logs nothing and raises nothing
        into the caller, because a process that is already gone is the outcome
        the caller wanted.
        """
        if self.process is None or self.process.poll() is not None:
            self._close_master()
            return
        try:
            pgid = os.getpgid(self.process.pid)
        except ProcessLookupError:
            self._close_master()
            return
        for sig, grace in ((signal.SIGTERM, _KILL_GRACE_S), (signal.SIGKILL, 1.0)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            try:
                self.process.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        self._close_master()

    def _close_master(self) -> None:
        """Release the PTY master descriptor exactly once."""
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    # -- interaction -------------------------------------------------------

    def write(self, text: str, *, newline: bool = True) -> None:
        """Send *text* to the process's stdin, appending a newline by default.

        Raises ``ValueError`` when the process has already exited — a write that
        vanishes is worse than a refusal the model can read and act on.
        """
        if self.status == "exited":
            raise ValueError(
                f"shell {self.shell_id} has already exited "
                f"(exit_code={self.exit_code}); nothing can be written to it."
            )
        payload = text + ("\n" if newline and not text.endswith("\n") else "")
        data = payload.encode("utf-8")
        if self._master_fd is not None:
            os.write(self._master_fd, data)
        elif self.process is not None and self.process.stdin is not None:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        else:
            raise ValueError(f"shell {self.shell_id} has no writable stdin.")
        self.touch()

    def read(
        self,
        *,
        cursor: int | None = None,
        pattern: str | None = None,
        wait_ms: int = 0,
    ) -> dict[str, object]:
        """Return output after *cursor*, optionally waiting briefly for more.

        With no *cursor* this returns everything retained. *pattern* filters
        LINES for display only and never consumes them — a filtered read leaves
        the cursor where an unfiltered one would, so narrowing what you look at
        can't silently destroy what you have not looked at yet.

        Cost: ``O(retained window)``.
        """
        start = 0 if cursor is None else max(0, cursor)
        deadline = time.monotonic() + min(wait_ms, MAX_READ_WAIT_MS) / 1000.0
        while True:
            with self._lock:
                text, next_cursor, missed = self.buffer.read_from(start)
            if text or self.status == "exited" or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if self.status == "exited":
            # Exit and the drain thread's last append are NOT ordered: the
            # process can be reaped while the final chunk is still in flight, so
            # a read that trusted ``status`` alone silently dropped the tail —
            # the end that carries the exit banner and the traceback. Settling
            # the reader first is what makes "exited" mean "everything is here".
            self._flush()
            with self._lock:
                text, next_cursor, missed = self.buffer.read_from(start)
        self.touch()
        if pattern is not None:
            matcher = re.compile(pattern)
            text = "\n".join(line for line in text.splitlines() if matcher.search(line))
        payload: dict[str, object] = {
            "shell_id": self.shell_id,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": text,
            "cursor": next_cursor,
        }
        if missed:
            payload["missed_characters"] = missed
        return payload

    def touch(self) -> None:
        """Mark the session as recently used, deferring its idle reaping."""
        self.last_used = self._clock()

    def describe(self) -> dict[str, object]:
        """A listing row: identity and liveness, never the output itself."""
        return {
            "shell_id": self.shell_id,
            "command": self.command,
            "cwd": self.cwd,
            "tty": self.tty,
            "status": self.status,
            "exit_code": self.exit_code,
            "running_for_s": round(self._clock() - self.started_at, 1),
        }


class ShellSessionStore:
    """The bounded registry of live shell sessions.

    Reaping is LAZY — every public method sweeps first — rather than a
    background thread. A thread is one more thing that can stop running without
    anyone noticing, which is precisely how ``shutdown_lsp_managers`` came to be
    documented as running at session teardown while having zero callers. A sweep
    on the access path cannot rot: if sessions are being created, they are being
    reaped.

    Cost: ``O(sessions)`` per access, bounded by :data:`MAX_SESSIONS`.
    """

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        idle_ttl_s: float = IDLE_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create an empty store bounded by *max_sessions* and *idle_ttl_s*.

        *clock* is injected rather than read from the wall, and it is handed
        DOWN to every session this store creates — the TTL compares a store
        reading against a session's own ``last_used``, so a caller advancing
        one of the two would be comparing readings from different clocks. One
        clock for both is what lets a TTL case reach its deadline by setting an
        instant rather than waiting out a real one.
        """
        self._sessions: dict[str, ShellSession] = {}
        self._clock = clock
        self._lock = threading.RLock()
        self._seq = 0
        self._max_sessions = max_sessions
        self._idle_ttl_s = idle_ttl_s

    def _next_id(self) -> str:
        self._seq += 1
        return f"shell_{self._seq}"

    def reap(self, *, headroom: int = 0) -> None:
        """Kill idle-past-TTL sessions and evict terminal ones.

        *headroom* is how many free slots the caller needs AFTER the sweep. A
        plain sweep asks for none; ``create`` asks for one, because a store
        sitting exactly at capacity with a finished session in it must yield
        that slot rather than refuse — evicting only once already OVER capacity
        is a bound that can never be reached, so it never evicted at all.
        """
        with self._lock:
            now = self._clock()
            for session in list(self._sessions.values()):
                if session.status == "running" and now - session.last_used > self._idle_ttl_s:
                    session.kill()
            overflow = len(self._sessions) - (self._max_sessions - headroom)
            if overflow <= 0:
                return
            terminal = [s for s in self._sessions.values() if s.status == "exited"]
            terminal.sort(key=lambda s: s.last_used)
            for session in terminal[:overflow]:
                self._sessions.pop(session.shell_id, None)

    def create(self, args: ShellStartArgs, cwd: str) -> ShellSession:
        """Start a session, refusing rather than evicting live work at capacity.

        The scope comes from the loop-published working directory, never from
        *cwd* — a model may supply its own ``root`` when no containment is
        active, and a model-chosen root would be a model-chosen sandbox. This is
        also the one funnel every session passes through, so deriving it here
        makes the scope impossible to forget at a future call site.

        The spawn itself happens OUTSIDE the lock. ``Popen`` blocks until the
        child execs or dies, and with a ``preexec_fn`` in play that is no longer
        a bounded wait — a child wedged between fork and exec would otherwise
        hold this lock forever, and every other verb (read, write, kill, list,
        and ``atexit`` shutdown) takes the same one. The whole shell surface of a
        single-worker deployment would wedge with nothing logged.

        The cost is that two concurrent creates can both pass the capacity check
        and overshoot ``_max_sessions`` by the number racing. That is bounded,
        self-correcting on the next ``reap``, and strictly better than trading a
        hard cap for a deadlock.
        """
        self.reap(headroom=1)
        scope = ShellScope.for_active_root(get_active_project_root())
        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                running = sum(1 for s in self._sessions.values() if s.status == "running")
                raise ValueError(
                    f"the shell session limit of {self._max_sessions} is reached "
                    f"({running} still running). Kill one with "
                    f"shell_session_tool {{'operation': 'kill', 'shell_id': ...}} "
                    f"— list them with {{'operation': 'list'}} — then retry."
                )
            shell_id = self._next_id()
        session = ShellSession(shell_id, args, cwd, scope=scope, clock=self._clock)
        with self._lock:
            self._sessions[shell_id] = session
        return session

    def get(self, shell_id: str) -> ShellSession:
        """Return the named session, naming the live ones when it is absent."""
        self.reap()
        with self._lock:
            session = self._sessions.get(shell_id)
            if session is None:
                known = ", ".join(sorted(self._sessions)) or "none"
                raise ValueError(f"unknown shell_id {shell_id!r}. Known sessions: {known}.")
            return session

    def list(self) -> list[dict[str, object]]:
        """Describe every retained session, newest first."""
        self.reap()
        with self._lock:
            return [
                s.describe()
                for s in sorted(
                    self._sessions.values(), key=lambda s: s.started_at, reverse=True
                )
            ]

    def discard(self, shell_id: str) -> None:
        """Forget a session, killing it first if it is still alive."""
        with self._lock:
            session = self._sessions.pop(shell_id, None)
        if session is not None:
            session.kill()

    def shutdown(self) -> None:
        """Kill every session. Registered with ``atexit`` so nothing outlives us."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.kill()


# The one process-wide store. Both shell tools resolve through it, so there is
# exactly one answer to "what is running" no matter which surface asked.
SHELL_SESSIONS = ShellSessionStore()

atexit.register(SHELL_SESSIONS.shutdown)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_BUFFER_CHARS",
    "MAX_READ_WAIT_MS",
    "MAX_SESSIONS",
    "OutputBuffer",
    "SHELL_SESSIONS",
    "ShellSession",
    "ShellSessionArgs",
    "ShellSessionStore",
    "ShellStartArgs",
]
