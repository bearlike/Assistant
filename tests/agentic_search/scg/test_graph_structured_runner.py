"""Tests for :class:`GraphStructuredRunner` — the #77 graph-first structured path.

A structured run bound to a mapped Agentic Search workspace goes graph-first: the
SAME agentic ``StructuredResponder`` / ``ToolUseLoop`` (NOT a separate execution
path), but granted the ``scg`` capability + graph traversal tools + the workspace
source scope, driven by the ``scg-search-structured`` playbook whose terminal is
the schema-validated ``emit_result``. These drive the real runner over a real
JSON store; eligibility is gated on ``scg.enabled`` + a mapped source, and the
composed responder must carry the binding facts.
"""

from __future__ import annotations

from typing import Any

import pytest
from mewbo_api.agentic_search.scg.graph_structured_runner import GraphStructuredRunner
from mewbo_api.agentic_search.scg.workspace_binding import SCG_CAPABILITY
from mewbo_api.agentic_search.schemas import Workspace, WorkspaceInput
from mewbo_api.agentic_search.store import JsonAgenticSearchStore
from mewbo_graph.scg import store as scg_store_mod
from mewbo_graph.scg.types import SourceDescriptor

_SCHEMA = {"type": "object", "properties": {"owner": {"type": "string"}}, "required": ["owner"]}


@pytest.fixture(autouse=True)
def _reset_scg():
    scg_store_mod.reset_for_tests()
    yield
    scg_store_mod.reset_for_tests()


@pytest.fixture
def store(tmp_path) -> JsonAgenticSearchStore:
    return JsonAgenticSearchStore(root_dir=tmp_path)


def _ws(store: JsonAgenticSearchStore, sources: list[str]) -> Workspace:
    return store.create_workspace(WorkspaceInput(name="Eng", sources=sources))


def _map_source(source_id: str) -> None:
    scg_store_mod.get_scg_store().upsert_source(
        SourceDescriptor(source_id=source_id, source_type="mcp_tool_list", raw={})
    )


def _enable_scg(monkeypatch) -> None:
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.graph_structured_runner.ScgConfig.enabled",
        staticmethod(lambda: True),
    )


def test_workspace_for_resolves_by_id_and_name(store) -> None:
    """A workspace resolves by id OR case-insensitive name; unknown → None."""
    ws = _ws(store, ["github"])
    runner = GraphStructuredRunner(store=store)
    assert runner.workspace_for(ws.id) is not None
    assert runner.workspace_for("eng") is not None  # case-insensitive name
    assert runner.workspace_for("no-such-workspace") is None


def test_eligible_only_when_enabled_and_mapped(store, monkeypatch) -> None:
    """Graph-first is gated on scg.enabled AND ≥1 mapped source."""
    ws = _ws(store, ["github"])
    runner = GraphStructuredRunner(store=store)

    # SCG off → never eligible.
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.graph_structured_runner.ScgConfig.enabled",
        staticmethod(lambda: False),
    )
    assert runner.is_graph_eligible(ws) is False

    # SCG on but no mapped source → not eligible (graph routes nothing).
    _enable_scg(monkeypatch)
    assert runner.is_graph_eligible(ws) is False

    # SCG on + the workspace's source mapped → eligible.
    _map_source("github")
    assert runner.is_graph_eligible(ws) is True


def test_build_responder_carries_binding_facts(store, monkeypatch) -> None:
    """The composed responder advertises scg + grants graph tools + binds scope."""
    _enable_scg(monkeypatch)
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.graph_structured_runner.SourceCatalog.tools_for",
        staticmethod(lambda sources, project: [f"mcp_{s}_search" for s in sources]),
    )
    ws = _ws(store, ["github"])
    _map_source("github")

    runner = GraphStructuredRunner(store=store)
    responder = runner.build_responder(
        ws, runtime=object(), schema=_SCHEMA, tools=None, source_platform="mcp"
    )

    # Capability advertisement is scg (not the default wiki).
    assert responder.capabilities == [SCG_CAPABILITY]
    assert responder.context_events[0] == {"client_capabilities": [SCG_CAPABILITY]}
    # The graph traversal verbs + the connector grant are both granted.
    assert "mcp_github_search" in responder.allowed_tools  # type: ignore[operator]
    for verb in ("scg_route", "scg_memory", "spawn_agent", "check_agents"):
        assert verb in responder.allowed_tools  # type: ignore[operator]
    # The graph-first playbook is the trusted skill_instructions extension.
    assert responder.extra_instructions and "emit_result" in responder.extra_instructions
    assert responder.scope_factory is not None
    assert responder.workspace == ws.id
    assert responder.source_platform == "mcp"


def test_build_responder_caller_tools_narrow_never_widen(store, monkeypatch) -> None:
    """A caller-supplied ``tools`` intersects the grant (narrows, never widens)."""
    _enable_scg(monkeypatch)
    monkeypatch.setattr(
        "mewbo_api.agentic_search.scg.graph_structured_runner.SourceCatalog.tools_for",
        staticmethod(lambda sources, project: [f"mcp_{s}_search" for s in sources]),
    )
    ws = _ws(store, ["github", "linear"])
    _map_source("github")
    runner = GraphStructuredRunner(store=store)

    # Caller asks for ONLY github + scg_route + a tool NOT in the grant.
    responder = runner.build_responder(
        ws,
        runtime=object(),
        schema=_SCHEMA,
        tools=["mcp_github_search", "scg_route", "not_granted"],
        source_platform=None,
    )
    allowed: Any = responder.allowed_tools
    assert "mcp_github_search" in allowed
    assert "scg_route" in allowed
    assert "mcp_linear_search" not in allowed  # narrowed away
    assert "not_granted" not in allowed  # caller can't widen past the grant
