#!/usr/bin/env python3
"""Tests for the CLI auto-titler (#157).

Stubs only the LLM seam (``generate_session_title``); the store is an in-memory
fake so the idempotency + persistence + callback wiring run real code paths.
"""

from __future__ import annotations

import threading
from typing import Any

import mewbo_core.title_generator as title_generator
from mewbo_cli.tui.session.autotitle import AutoTitler


class _FakeStore:
    """Minimal SessionStore stand-in for the title flow."""

    def __init__(self, transcript: list[dict[str, Any]] | None = None) -> None:
        self._titles: dict[str, str] = {}
        self._events: list[dict[str, Any]] = []
        self._transcript = transcript or [
            {"type": "user", "payload": {"text": "fix the bug"}},
            {"type": "assistant", "payload": {"text": "done"}},
        ]

    def load_title(self, session_id: str) -> str | None:
        return self._titles.get(session_id)

    def save_title(self, session_id: str, title: str) -> None:
        self._titles[session_id] = title

    def load_transcript(self, session_id: str) -> list[dict[str, Any]]:
        return self._transcript

    def append_event(self, session_id: str, event: dict[str, Any]) -> None:
        self._events.append(event)


def _run_sync(titler: AutoTitler, session_id: str) -> None:
    """Call the worker body directly (no background thread) for determinism."""
    titler._run(session_id)


def test_skips_when_title_exists() -> None:
    store = _FakeStore()
    store.save_title("s1", "Existing")
    titler = AutoTitler(store=store)
    assert titler.has_title("s1") is True
    assert titler.maybe_title("s1") is False  # no thread started


def test_generates_and_persists_title(monkeypatch: Any) -> None:
    store = _FakeStore()
    monkeypatch.setattr(
        title_generator,
        "generate_session_title",
        _async_return("Fix the bug"),
    )
    sink: list[str] = []
    titler = AutoTitler(store=store, on_title=sink.append)
    _run_sync(titler, "s1")
    assert store.load_title("s1") == "Fix the bug"
    assert sink == ["Fix the bug"]
    # a title_update event was emitted
    assert any(e["type"] == "title_update" for e in store._events)


def test_no_title_when_llm_returns_none(monkeypatch: Any) -> None:
    store = _FakeStore()
    monkeypatch.setattr(title_generator, "generate_session_title", _async_return(None))
    titler = AutoTitler(store=store)
    _run_sync(titler, "s1")
    assert store.load_title("s1") is None


def test_worker_rechecks_race(monkeypatch: Any) -> None:
    store = _FakeStore()
    store.save_title("s1", "Won the race")
    called: list[bool] = []

    async def _should_not_run(events: Any) -> str:
        called.append(True)
        return "Loser"

    monkeypatch.setattr(title_generator, "generate_session_title", _should_not_run)
    AutoTitler(store=store)._run("s1")
    # the worker saw the existing title and never called the LLM
    assert called == []
    assert store.load_title("s1") == "Won the race"


def test_maybe_title_starts_thread(monkeypatch: Any) -> None:
    store = _FakeStore()
    monkeypatch.setattr(title_generator, "generate_session_title", _async_return("Threaded"))
    titler = AutoTitler(store=store)
    assert titler.maybe_title("s1") is True
    # join the daemon worker so the assert is deterministic
    for thread in threading.enumerate():
        if thread.name.startswith("cli-autotitle-"):
            thread.join(timeout=5)
    assert store.load_title("s1") == "Threaded"


def _async_return(value: Any):
    async def _coro(events: Any) -> Any:
        return value

    return _coro
