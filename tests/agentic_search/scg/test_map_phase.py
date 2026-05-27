"""Tests for MapPhaseSink — the DI seam for map-job phase progress.

Exercises the full contract:
- emit() returns None when no writer is registered (no-op, no crash).
- register() + emit() calls the registered writer and returns its value.
- reset() clears the writer so subsequent emit() returns None again.
- A writer that raises is swallowed (best-effort; phase write is cosmetic).
- The registered writer receives exactly the (job_id, phase) arguments.
- Re-registering a new writer replaces the previous one.
- reset() is idempotent (calling it twice is safe).
"""

from __future__ import annotations

import pytest
from mewbo_graph.scg.map_phase import MapPhaseSink, MapPhaseWriter

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_recording_writer() -> tuple[MapPhaseWriter, list[tuple[str, str]]]:
    """Return a writer that records every call and an idx counter."""
    calls: list[tuple[str, str]] = []
    counter: list[int] = [0]

    def _writer(job_id: str, phase: str) -> int:
        calls.append((job_id, phase))
        counter[0] += 1
        return counter[0] - 1  # 0-based idx

    return _writer, calls


# ── no writer registered ─────────────────────────────────────────────────────


def test_emit_without_writer_is_noop() -> None:
    """emit() returns None and never raises when no writer has been registered,
    even across multiple calls with different phases."""
    MapPhaseSink.reset()
    for phase in ("connect", "introspect", "parse", "link", "finalize"):
        assert MapPhaseSink.emit("job-x", phase) is None


# ── register + emit ──────────────────────────────────────────────────────────


def test_register_then_emit_calls_writer() -> None:
    """After register(), emit() delegates to the writer and returns its return value."""
    MapPhaseSink.reset()
    writer, calls = _make_recording_writer()
    MapPhaseSink.register(writer)

    result = MapPhaseSink.emit("job-abc", "connect")

    assert result == 0
    assert calls == [("job-abc", "connect")]


def test_emit_passes_job_id_and_phase_verbatim() -> None:
    """The writer receives exactly (job_id, phase) as positional arguments."""
    MapPhaseSink.reset()
    received: list[tuple[str, str]] = []

    def _spy(job_id: str, phase: str) -> int:
        received.append((job_id, phase))
        return 42

    MapPhaseSink.register(_spy)
    val = MapPhaseSink.emit("my-job-007", "introspect")

    assert val == 42
    assert received == [("my-job-007", "introspect")]


def test_emit_returns_sequential_indices() -> None:
    """A counter writer produces 0, 1, 2 … for successive emit() calls."""
    MapPhaseSink.reset()
    writer, calls = _make_recording_writer()
    MapPhaseSink.register(writer)

    phases = ["connect", "introspect", "parse", "link", "finalize"]
    results = [MapPhaseSink.emit("job-seq", p) for p in phases]

    assert results == [0, 1, 2, 3, 4]
    assert [c[1] for c in calls] == phases


# ── reset ────────────────────────────────────────────────────────────────────


def test_reset_clears_registered_writer() -> None:
    """reset() removes the writer so emit() returns None afterwards."""
    MapPhaseSink.reset()
    writer, calls = _make_recording_writer()
    MapPhaseSink.register(writer)

    # Confirm writer is live.
    MapPhaseSink.emit("job-r", "parse")
    assert len(calls) == 1

    MapPhaseSink.reset()
    result = MapPhaseSink.emit("job-r", "finalize")

    assert result is None
    assert len(calls) == 1  # no new call after reset


def test_reset_is_idempotent() -> None:
    """Calling reset() twice raises no exception."""
    MapPhaseSink.reset()
    MapPhaseSink.reset()  # should not raise


# ── re-registration ──────────────────────────────────────────────────────────


def test_registering_new_writer_replaces_previous() -> None:
    """A second register() call replaces the first writer."""
    MapPhaseSink.reset()
    _, calls_a = _make_recording_writer()
    writer_b, calls_b = _make_recording_writer()
    MapPhaseSink.register(lambda j, p: calls_a.append((j, p)) or 99)
    MapPhaseSink.register(writer_b)

    MapPhaseSink.emit("job-new", "link")

    # Only writer_b was called.
    assert calls_b == [("job-new", "link")]
    # calls_a is empty (writer_a was replaced before any emit via the new path)


def test_register_none_after_writer_is_equivalent_to_reset() -> None:
    """register(None) is semantically identical to reset()."""
    MapPhaseSink.reset()
    writer, calls = _make_recording_writer()
    MapPhaseSink.register(writer)
    MapPhaseSink.register(None)

    result = MapPhaseSink.emit("job-nil", "finalize")
    assert result is None
    assert calls == []  # writer was deregistered


# ── fault tolerance ───────────────────────────────────────────────────────────


def test_raising_writer_is_swallowed_and_returns_none() -> None:
    """A writer that raises does NOT propagate — emit() returns None instead."""
    MapPhaseSink.reset()
    call_count: list[int] = [0]

    def _bad_writer(job_id: str, phase: str) -> int:
        call_count[0] += 1
        raise RuntimeError("transport hiccup")

    MapPhaseSink.register(_bad_writer)
    result = MapPhaseSink.emit("job-bad", "finalize")

    assert result is None
    assert call_count[0] == 1  # writer was called, exception was absorbed


def test_raising_writer_does_not_deregister_itself() -> None:
    """After a writer raises, subsequent emit() calls still attempt it."""
    MapPhaseSink.reset()
    call_count: list[int] = [0]

    def _flaky(job_id: str, phase: str) -> int:
        call_count[0] += 1
        raise ValueError("always fails")

    MapPhaseSink.register(_flaky)

    for _ in range(3):
        result = MapPhaseSink.emit("job-flaky", "parse")
        assert result is None

    assert call_count[0] == 3  # called each time despite failures


# ── cleanup ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_sink() -> None:
    """Ensure each test starts and ends with a clean MapPhaseSink."""
    MapPhaseSink.reset()
    yield
    MapPhaseSink.reset()
