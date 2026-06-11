"""Run-grant resolution honours the workspace virtual MCP config (#75).

``SearchRun.start`` computes a run's ``allowed_tools`` from the workspace's source
selection. #75 makes the PERSISTED virtual MCP config the source of truth: when a
config is attached, the grant resolves from its server names; otherwise it falls
back to the workspace's raw ``sources`` (the current global behavior).

Drives the real ``SearchRun.start`` over a real JSON store with the runner seam
swapped for a capturing fake (no LLM / session). The catalog's ``tools_for`` is
stubbed to a deterministic identity map so the assertion is purely "which source
ids did the grant resolve from".
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from mewbo_api.agentic_search import runs as runs_mod
from mewbo_api.agentic_search.mcp_config import WorkspaceMcpConfig
from mewbo_api.agentic_search.runs import SearchRun
from mewbo_api.agentic_search.schemas import RunPayload, RunRecord, Workspace, WorkspaceInput
from mewbo_api.agentic_search.store import JsonAgenticSearchStore


@pytest.fixture()
def store() -> JsonAgenticSearchStore:
    """A fresh JSON agentic_search store under a throwaway temp dir."""
    return JsonAgenticSearchStore(root_dir=Path(tempfile.mkdtemp(prefix="grant-")))


@pytest.fixture(autouse=True)
def _capture_grant(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``tools_for`` (identity, capturing) + the runner seam (no LLM)."""
    seen: dict[str, Any] = {}

    def _fake_tools_for(source_ids: list[str], project: str | None = None) -> list[str]:
        seen["grant_sources"] = list(source_ids)
        return [f"tool::{sid}" for sid in source_ids]

    monkeypatch.setattr(runs_mod.SourceCatalog, "tools_for", staticmethod(_fake_tools_for))

    class _FakeRunner:
        def start(
            self,
            run: RunRecord,
            workspace: Workspace,
            *,
            store: Any,
            runtime: Any = None,
            source_platform: str | None = None,
        ) -> RunPayload:
            seen["allowed_tools"] = list(run.allowed_tools)
            return RunPayload(
                run_id=run.run_id,
                session_id=run.session_id,
                query=run.query,
                workspace_id=run.workspace_id,
                status="completed",
            )

    monkeypatch.setattr(runs_mod, "get_search_runner", lambda: _FakeRunner())
    return seen


def _workspace(store: JsonAgenticSearchStore, sources: list[str]) -> Workspace:
    return store.create_workspace(WorkspaceInput(name="ws", sources=sources))


def test_grant_falls_back_to_raw_sources_without_config(
    store: JsonAgenticSearchStore, _capture_grant: dict[str, Any]
) -> None:
    """No virtual config persisted → grant resolves from the workspace sources."""
    ws = _workspace(store, ["gitea", "slack"])
    SearchRun.start(workspace_id=ws.id, query="q", store=store)
    assert _capture_grant["grant_sources"] == ["gitea", "slack"]
    assert _capture_grant["allowed_tools"] == ["tool::gitea", "tool::slack"]


def test_grant_prefers_attached_virtual_config(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _capture_grant: dict[str, Any],
) -> None:
    """A persisted virtual config is the authoritative grant, not raw sources.

    The workspace's raw ``sources`` and its attached config deliberately differ;
    the grant must follow the CONFIG (the persisted source of truth).
    """
    ws = _workspace(store, ["gitea", "slack", "stale-source"])
    # Attach a config whose server set differs from the raw sources.
    monkeypatch.setattr(
        WorkspaceMcpConfig,
        "attached_server_names",
        staticmethod(lambda store, workspace_id: ["gitea", "internet-search"]),
    )
    SearchRun.start(workspace_id=ws.id, query="q", store=store)
    assert _capture_grant["grant_sources"] == ["gitea", "internet-search"]


def test_empty_attached_config_falls_back_to_sources(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _capture_grant: dict[str, Any],
) -> None:
    """An empty attached list is falsy → falls back to raw sources (``or``).

    (An explicitly-empty selection at the GRANT layer is indistinguishable from
    "no tools"; the run-grant `or` fallback keeps the historical behavior of
    granting the workspace's own sources when the config resolved nothing.)
    """
    ws = _workspace(store, ["gitea"])
    monkeypatch.setattr(
        WorkspaceMcpConfig,
        "attached_server_names",
        staticmethod(lambda store, workspace_id: []),
    )
    SearchRun.start(workspace_id=ws.id, query="q", store=store)
    assert _capture_grant["grant_sources"] == ["gitea"]
