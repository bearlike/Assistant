#!/usr/bin/env python3
"""SCG as an scg-capable reasoning surface — gating, scope, approval.

The SCG reasoning tools (``scg_route`` / ``scg_observe`` / ``scg_memory``) are
gated on the ``scg`` capability. This file covers WHO reaches them and how an
scg-capable-but-UNSCOPED session behaves:

* **advertisement-only gating** — importing this suite registers NO
  runtime capability provider, so a bare session (advertised nothing, named no
  scg tool) is NOT auto-granted ``scg`` even when ``scg.enabled`` is on and a
  source is mapped. The earlier blanket provider that granted ``scg`` to
  any such session (binding all 12 scg/agentic_search schemas on every LLM call
  and misclassifying it as ``origin:search``) is removed;
* an UNSCOPED scg session reads the WHOLE graph (no ``ScgScope`` bound → the
  scope default permits every source);
* an UNSCOPED ``scg_memory`` write lands with ``session:<id>`` attribution + a
  live ``ANCHORS`` edge (capability-seeded store, per the lesson — never an
  ``entity_type``-only fixture, which masked the dropped-anchor bug);
* the approval default-allow path — ``scg_route`` / ``scg_observe`` / ``scg_memory``
  classify ``get`` so the DEFAULT permission policy ALLOWs them with no extra knob.

No LLM, no network, no Mongo — the ``ScgCore`` seam is patched to a tmp store +
fake embedder for the read/write paths, and the gating check runs over a real
JSON-backed store with the config flag mocked.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.permissions import PermissionDecision, _default_policy
from mewbo_core.tool_use_loop import _infer_operation
from mewbo_graph.plugins.scg import _core
from mewbo_graph.plugins.scg.memory import ScgMemoryTool
from mewbo_graph.plugins.scg.observe import ScgObserveTool
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import ScgEdge, ScgNode, SourceDescriptor

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeEmbedder:
    """The wiki ``Embedder`` surface (no network); deterministic vectors."""

    model = "fake-embed"

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list[Any]:
        return [SimpleNamespace(node_id=nid, vector=[1.0, 0.0], dim=2) for nid, _ in items]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=False))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def mapped_store(tmp_path: Path) -> JsonScgStore:
    """A store with ONE mapped source whose nodes are ``capability`` kind.

    The lesson: an MCP-tool-list source mints ``capability`` nodes (no entity
    layer), so a fixture seeding only ``entity_type`` masks the dropped-anchor bug.
    This fixture seeds a real two-capability source with a typed edge so the
    predicate, scope read, and anchor resolution all exercise the live shape.
    """
    s = JsonScgStore(root_dir=tmp_path / "scg")
    s.upsert_source(
        SourceDescriptor(source_id="github", source_type="mcp_tool_list", raw={"tools": []})
    )
    s.upsert_nodes(
        [
            ScgNode(
                source_key="github#search_issues",
                kind="capability",
                name="search_issues",
                source_id="github",
                doc="Search issues in a repo",
            ),
            ScgNode(
                source_key="github#get_issue",
                kind="capability",
                name="get_issue",
                source_id="github",
                doc="Fetch one issue by id",
            ),
        ]
    )
    s.upsert_edges(
        [
            ScgEdge(
                source="github#search_issues",
                target="github#get_issue",
                kind="PRODUCES",
                weight=1.0,
            )
        ]
    )
    return s


@pytest.fixture()
def patched_core(monkeypatch: pytest.MonkeyPatch, mapped_store: JsonScgStore) -> JsonScgStore:
    """Point ``ScgCore.store`` / ``embedder`` at the tmp store + fake embedder."""
    embedder = _FakeEmbedder()
    monkeypatch.setattr(_core.ScgCore, "store", staticmethod(lambda: mapped_store))
    monkeypatch.setattr(_core.ScgCore, "embedder", staticmethod(lambda: embedder))
    return mapped_store


def _step(tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_id="t", operation="execute", tool_input=tool_input)


def _run(tool: Any, tool_input: dict) -> dict:
    return ast.literal_eval(asyncio.run(tool.handle(_step(tool_input))).content)


# ── 1. Advertisement-only gating (no blanket runtime grant) ────────────


def test_suite_registers_no_capability_provider(mapped_store: JsonScgStore) -> None:
    """Importing the scg suite must NOT auto-grant ``scg`` to bare sessions.

    The blanket provider granted ``scg`` to ANY session once ``scg.enabled``
    + ≥1 mapped source held — binding all 12 scg/agentic_search schemas on every
    LLM call of a bare ``POST /api/sessions`` coding session and tagging it
    ``origin:search``. It is removed. Re-running the suite's import side-effects
    registers NO capability provider, so ``augment_session_capabilities`` is a
    pass-through for a session that advertised nothing — even with the feature
    enabled and a source mapped. ``scg`` is advertisement-only now.
    """
    import mewbo_core.capabilities as core_caps
    import mewbo_graph.plugins.scg as scg_pkg
    from mewbo_graph.scg.store import set_scg_store

    set_scg_store(mapped_store)
    core_caps.reset_session_capability_providers()  # drop any sibling-test leak
    try:
        importlib.reload(scg_pkg)  # re-run the suite's import-time registration
        # The suite pushes nothing into core's generic provider seam...
        assert core_caps._CAPABILITY_PROVIDERS == []
        # ...and the symbols are gone entirely.
        assert not hasattr(scg_pkg, "_scg_runtime_capability")
        assert not hasattr(scg_pkg, "register_scg_capability")
        # So a bare session gains nothing even with scg.enabled + a mapped source.
        with patch("mewbo_core.config.get_config_value", return_value=True):
            assert core_caps.augment_session_capabilities(()) == ()
        # An explicit advertisement is the ONLY path — and survives untouched.
        assert core_caps.augment_session_capabilities(("scg",)) == ("scg",)
    finally:
        set_scg_store(None)
        core_caps.reset_session_capability_providers()


# ── 2. Unscoped read = whole graph ───────────────────────────────────────────


def test_unscoped_observe_reads_whole_graph(patched_core: JsonScgStore) -> None:
    """With NO ``ScgScope`` bound, an observe read returns the in-graph node + hop.

    The scope default permits every source (``ScgScope.allowed()`` is ``None`` →
    unscoped), so an ordinary session sees the whole graph — verified by the
    typed PRODUCES hop to ``github#get_issue`` surfacing without any workspace.
    """
    from mewbo_graph.scg.scope import ScgScope

    assert ScgScope.allowed() is None  # unscoped default — whole graph

    out = _run(ScgObserveTool(session_id="s1"), {"nodes": ["github#search_issues"]})
    observed = out["observed"][0]
    assert observed["found"] is True
    assert observed["source"] == "github"
    # The cross-capability hop is visible (no source dropped by an absent scope).
    assert any(e["to"] == "github#get_issue" for e in observed["edges"])


# ── 3. Unscoped write = session attribution + live ANCHORS edge ───────────────


def test_unscoped_write_lands_with_session_attribution_and_anchor(
    monkeypatch: pytest.MonkeyPatch, patched_core: JsonScgStore, tmp_path: Path
) -> None:
    """An unscoped deposit attributes to ``session:<id>`` AND creates an ANCHORS edge.

    Exercises the REAL ``ScgMemoryBridge`` (only the wiki store factory is pointed
    at a tmp JSON store) so the kind-agnostic anchor resolution creates a
    live ``ANCHORS`` edge to a ``capability`` source_key — the exact shape that a
    legacy ``entity_type``-only fixture would have silently dropped.
    """
    from mewbo_graph.scg.memory_bridge import CONNECTOR_SLUG
    from mewbo_graph.scg.scope import ScgScope
    from mewbo_graph.wiki.store import JsonWikiStore

    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    monkeypatch.setattr("mewbo_graph.wiki.store.create_wiki_store", lambda: wiki_store)

    assert ScgScope.workspace() is None  # unscoped session

    out = _run(
        ScgMemoryTool(session_id="sess-xyz"),
        {
            "operation": "write",
            "content": "github#search_issues is queryable by repo, not free-text",
            "source_keys": ["github#search_issues"],
            "polarity": "positive",
        },
    )

    assert "error" not in out
    assert out["ok"] is True
    node_id = out["claims"][0]["node_id"]

    # The anchor resolved against the live capability node → an ANCHORS edge.
    edges = wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=node_id)
    assert [e.target for e in edges if e.type == "ANCHORS"] == ["github#search_issues"]

    # Attribution: a session:<id> label (no workspace) rides the persisted note.
    note = wiki_store.get_memory_node(CONNECTOR_SLUG, node_id)
    assert "session:sess-xyz" in note.labels
    assert not any(label.startswith("ws:") for label in note.labels)


# ── 4. Approval default-allow ─────────────────────────────────────────────────


@pytest.mark.parametrize("tool_id", ["scg_route", "scg_observe", "scg_memory"])
def test_scg_tools_default_allow(tool_id: str) -> None:
    """The scg reasoning tools classify ``get`` ⇒ the DEFAULT policy ALLOWs them.

    No secrets cross these tools (the ``auth_scope`` descriptor stays redacted), so
    graph reads + the additive memory deposit are default-allowed via the house
    operation-inference pattern — no new config knob.
    """
    operation = _infer_operation(tool_id)
    assert operation == "get"
    step = ActionStep(title=tool_id, tool_id=tool_id, operation=operation, tool_input={})
    assert _default_policy().decide(step) == PermissionDecision.ALLOW


def test_write_shaped_tool_still_asks() -> None:
    """A genuine write tool stays ``set`` ⇒ ASK — the allow-list is scg-scoped."""
    operation = _infer_operation("wiki_submit_page")
    assert operation == "set"
    step = ActionStep(
        title="wiki_submit_page", tool_id="wiki_submit_page", operation=operation, tool_input={}
    )
    assert _default_policy().decide(step) == PermissionDecision.ASK
