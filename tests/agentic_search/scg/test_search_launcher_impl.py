"""Tests for ``RunStoreSearchLauncher`` — the api-side self-search backend.

Drives the REAL launcher impl over a real JSON run store with the runner seam
swapped for a synchronous fake (no LLM / session), proving: a start persists a
run and returns its cited snapshot, ``computed_at`` carries the completion
timestamp, an identical query is idempotently reused (no duplicate run), and
workspace resolution raises actionable guidance.

Mirrors ``test_grant_resolution.py`` (real store + capturing fake runner,
``tools_for`` stubbed to identity so no live registry is touched).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from mewbo_api.agentic_search import runs as runs_mod, store as store_mod
from mewbo_api.agentic_search.runner import set_search_runner
from mewbo_api.agentic_search.schemas import (
    AnswerSynthesis,
    RunPayload,
    RunRecord,
    SearchResult,
    Workspace,
    WorkspaceInput,
    utc_now_iso,
)
from mewbo_api.agentic_search.search_launcher_impl import RunStoreSearchLauncher
from mewbo_api.agentic_search.store import JsonAgenticSearchStore


class _SyncRunner:
    """A runner that settles a run synchronously with a canned cited answer."""

    def __init__(self) -> None:
        self.calls = 0

    def start(
        self,
        run: RunRecord,
        workspace: Workspace,
        *,
        store: Any,
        runtime: Any = None,
        source_platform: str | None = None,
    ) -> RunPayload:
        self.calls += 1
        answer = AnswerSynthesis(tldr="The answer.", confidence=0.9, sources_count=1)
        result = SearchResult(
            id="r1", source="github", kind="code", title="X", url="http://x", relevance=0.8
        )
        payload = RunPayload(
            run_id=run.run_id,
            session_id=run.session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status="completed",
            tier=run.tier,
            total_ms=1234,
            answer=answer,
            results=[result],
        )
        store.update_run(
            run.run_id,
            status="completed",
            completed_at=utc_now_iso(),
            total_ms=1234,
            payload=payload,
        )
        return payload


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> JsonAgenticSearchStore:
    """A fresh JSON store wired as the process store + identity ``tools_for``."""
    st = JsonAgenticSearchStore(root_dir=Path(tempfile.mkdtemp(prefix="launcher-")))
    monkeypatch.setattr(store_mod, "get_store", lambda: st)
    monkeypatch.setattr(
        runs_mod.SourceCatalog,
        "tools_for",
        staticmethod(lambda source_ids, project=None: [f"tool::{s}" for s in source_ids]),
    )
    return st


@pytest.fixture(autouse=True)
def _runner():
    """Swap in the synchronous fake runner; restore resolution after."""
    runner = _SyncRunner()
    set_search_runner(runner)
    try:
        yield runner
    finally:
        set_search_runner(None)


def _workspace(store: JsonAgenticSearchStore, name: str, sources: list[str]) -> Workspace:
    return store.create_workspace(WorkspaceInput(name=name, sources=sources))


def test_start_persists_and_returns_cited_snapshot(store: JsonAgenticSearchStore) -> None:
    """A start drives a run and returns its answer + computed_at in one call."""
    _workspace(store, "Eng", ["github"])
    out = RunStoreSearchLauncher().start("how does auth work", workspace="Eng")

    assert out["status"] == "completed"
    assert out["answer"]["tldr"] == "The answer."
    assert out["results"][0]["id"] == "r1"
    assert out["computed_at"]  # the completion timestamp is surfaced
    # The run is durable — fetch by id returns the same snapshot.
    again = RunStoreSearchLauncher().fetch(out["run_id"])
    assert again is not None and again["run_id"] == out["run_id"]


def test_identical_query_is_idempotently_reused(
    store: JsonAgenticSearchStore, _runner: _SyncRunner
) -> None:
    """Re-asking the same question reuses the completed run (no second launch)."""
    _workspace(store, "Eng", ["github"])
    first = RunStoreSearchLauncher().start("same question", workspace="Eng")
    second = RunStoreSearchLauncher().start("same question", workspace="Eng")

    assert _runner.calls == 1  # the runner ran exactly once
    assert second.get("reused") is True
    assert second["run_id"] == first["run_id"]


def test_fetch_unknown_run_returns_none(store: JsonAgenticSearchStore) -> None:
    """An unknown run id is ``None`` (the tool maps it to not_found)."""
    assert RunStoreSearchLauncher().fetch("missing") is None


def test_single_workspace_is_the_default(store: JsonAgenticSearchStore) -> None:
    """With one workspace, omitting ``workspace`` uses it."""
    _workspace(store, "Only", ["github"])
    out = RunStoreSearchLauncher().start("q")
    assert out["status"] == "completed"


def test_ambiguous_workspace_raises_with_candidates(store: JsonAgenticSearchStore) -> None:
    """Several workspaces + no selector → a ValueError listing the names."""
    _workspace(store, "Eng", ["github"])
    _workspace(store, "Docs", ["notion"])
    with pytest.raises(ValueError, match="Docs"):
        RunStoreSearchLauncher().start("q")


def test_unknown_workspace_raises(store: JsonAgenticSearchStore) -> None:
    """An unresolvable workspace ref is a guided ValueError."""
    _workspace(store, "Eng", ["github"])
    with pytest.raises(ValueError, match="no workspace matches"):
        RunStoreSearchLauncher().start("q", workspace="Marketing")
