"""Tests for the ``agentic_search`` SessionTool — the self-facing search verb.

The tool is a thin async-by-handle wrapper over the down-only
:class:`~mewbo_graph.scg.search_launcher.SearchLauncher` seam: ``query`` starts a
run (immediate ``run_id``), ``run_id`` fetches the cited answer + ``computed_at``.
These exercise the tool in isolation against a FAKE launcher (no api, no run
store, no LLM) so they prove the contract: validation, the unavailable
degradation, pass-through of ``workspace``/``tier``, and the not-found path.

Conventions mirror ``test_scg_results.py`` (SimpleNamespace step +
``ast.literal_eval`` of the ``MockSpeaker`` content).
"""

from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace

import pytest
from mewbo_graph.plugins.scg.search import AgenticSearchTool
from mewbo_graph.scg.search_launcher import SearchLauncher


class _FakeLauncher:
    """A capturing in-memory launcher (no api / run store / LLM)."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.snapshots: dict[str, dict] = {}

    def start(self, query, *, workspace=None, tier=None):
        self.started.append({"query": query, "workspace": workspace, "tier": tier})
        return {"run_id": "run-1", "status": "processing", "query": query}

    def fetch(self, run_id):
        return self.snapshots.get(run_id)


def _step(tool_input):
    """A duck-typed ActionStep (matches the house test style)."""
    return SimpleNamespace(
        tool_id="agentic_search", operation="execute", tool_input=tool_input
    )


def _run(tool_input):
    """Invoke the tool and parse its structured ``MockSpeaker`` content."""
    tool = AgenticSearchTool(session_id="s1")
    speaker = asyncio.run(tool.handle(_step(tool_input)))
    return ast.literal_eval(speaker.content)


@pytest.fixture()
def launcher():
    """Register a fake launcher for the test; always reset after."""
    impl = _FakeLauncher()
    SearchLauncher.register(impl)
    try:
        yield impl
    finally:
        SearchLauncher.reset()


def test_unavailable_when_no_launcher_registered() -> None:
    """With no backend wired the tool degrades to a structured error."""
    SearchLauncher.reset()
    out = _run({"query": "where is the bug"})
    assert out["error"]["code"] == "unavailable"


def test_validation_requires_exactly_one_mode(launcher: _FakeLauncher) -> None:
    """Neither / both of query|run_id is a validation error."""
    assert _run({})["error"]["code"] == "validation"
    assert _run({"query": "q", "run_id": "run-1"})["error"]["code"] == "validation"
    # Blank query counts as absent.
    assert _run({"query": "   "})["error"]["code"] == "validation"


def test_start_returns_handle_and_passes_scope(launcher: _FakeLauncher) -> None:
    """``query`` starts a run and forwards workspace/tier to the launcher."""
    out = _run({"query": "auth flow", "workspace": "Engineering", "tier": "deep"})
    assert out["run_id"] == "run-1"
    assert out["status"] == "processing"
    assert launcher.started == [
        {"query": "auth flow", "workspace": "Engineering", "tier": "deep"}
    ]


def test_fetch_returns_snapshot(launcher: _FakeLauncher) -> None:
    """``run_id`` returns the launcher's last-known snapshot verbatim."""
    launcher.snapshots["run-1"] = {
        "run_id": "run-1",
        "status": "completed",
        "answer": {"tldr": "Done."},
        "computed_at": "2026-06-17T00:00:00Z",
    }
    out = _run({"run_id": "run-1"})
    assert out["status"] == "completed"
    assert out["answer"]["tldr"] == "Done."
    assert out["computed_at"] == "2026-06-17T00:00:00Z"


def test_fetch_unknown_run_is_not_found(launcher: _FakeLauncher) -> None:
    """An unknown run id is a structured not_found, never a crash."""
    assert _run({"run_id": "nope"})["error"]["code"] == "not_found"


def test_invalid_tier_is_rejected(launcher: _FakeLauncher) -> None:
    """An out-of-vocab tier fails validation (the Literal guard)."""
    assert _run({"query": "q", "tier": "turbo"})["error"]["code"] == "validation"
