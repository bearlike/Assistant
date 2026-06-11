"""Tests for the ``scg_observe`` SessionTool — agent-driven neighborhood reads.

Search-on-Graph (arXiv 2510.08825) inverts the retrieval split: ``scg_route``
ranks ENTRY points, then the agent OBSERVES a node's typed neighborhood and
navigates. ``scg_observe`` is that read — a thin projection over the SCG store +
``ScgGraphView``'s memory assembly. These exercise the handler against a seeded
JSON-backed ``ScgStore`` end-to-end (no LLM, no network, no MongoDB):

* neighborhood assembly — typed edges (kind + weight + direction) + neighbor cards;
* ``direction`` filtering (incoming / outgoing / both) over directed edges;
* the two-stage survey (``kinds_only`` rollup above the degree threshold) +
  ``edge_kinds`` selective retrieval;
* ``ScgScope`` filtering — an out-of-scope hop is dropped;
* anchored connector memory notes attached to the observed node;
* an unknown reference degrades gracefully (``found: False``), never a raise.

The ONLY stubbed seams are ``ScgCore.store`` (→ the tmp store) and
``ScgCore.memory_bridge`` (→ a real bridge over a real wiki JSON store + a fake
embedder) — mirroring ``test_plugin_tools.py``. The projection logic runs for real.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_graph.plugins.scg import _core
from mewbo_graph.plugins.scg.observe import _SURVEY_THRESHOLD, ScgObserveTool
from mewbo_graph.scg.memory_bridge import (
    CONNECTOR_SLUG,
    ScgAnchorResolver,
    ScgMemoryBridge,
)
from mewbo_graph.scg.scope import ScgScope
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import RouteRecipe, ScgEdge, ScgNode


class _FakeEmbedder:
    """Token-presence embedder — offline, deterministic (the only stubbed seam)."""

    model = "fake-embed"
    _VOCAB = ["repo", "issue", "id", "field", "github", "slack"]

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [float(low.count(t)) for t in self._VOCAB]

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list:
        from mewbo_graph.wiki.types import Embedding

        return [
            Embedding(slug=slug, node_id=nid, vector=self._vec(text),
                      model=self.model, dim=len(self._VOCAB))
            for nid, text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A two-source SCG: github (search→Repo, an entity + recipe) + slack (peer)."""
    s = JsonScgStore(root_dir=tmp_path / "scg")
    s.upsert_nodes([
        ScgNode(source_key="github#search", kind="capability",
                source_id="github", name="search",
                doc="Search github repositories by query."),
        ScgNode(source_key="github#Repo", kind="entity_type",
                source_id="github", name="Repo", doc="A repository.",
                auth_scope="oauth:repo"),
        ScgNode(source_key="github#get_issue", kind="capability",
                source_id="github", name="get_issue", doc="Get one issue by id."),
        ScgNode(source_key="slack#Channel", kind="entity_type",
                source_id="slack", name="Channel", doc="A slack channel."),
    ])
    s.upsert_edges([
        # github#search PRODUCES github#Repo (outgoing from search).
        ScgEdge(source="github#search", target="github#Repo", kind="PRODUCES",
                weight=0.9),
        # github#get_issue CONSUMES github#search (incoming to search).
        ScgEdge(source="github#get_issue", target="github#search", kind="CONSUMES",
                weight=0.7),
        # A cross-source RESOLVES_TO into slack (out of a github-only scope).
        ScgEdge(source="github#Repo", target="slack#Channel", kind="RESOLVES_TO",
                weight=0.5),
    ])
    s.upsert_recipes([
        RouteRecipe(source_key="github#search",
                    steps=["github#search", "github#Repo"]),
    ])
    return s


@pytest.fixture()
def patched_core(
    monkeypatch: pytest.MonkeyPatch, store: JsonScgStore, tmp_path: Path
) -> JsonScgStore:
    """Point ``ScgCore.store`` at the tmp store + ``memory_bridge`` at a real bridge.

    The bridge wraps a real wiki JSON store + the fake embedder, so the memory
    layer assembles for real; its resolver is pinned to the tmp SCG store so a
    connector anchor resolves against the live structure.
    """
    from mewbo_graph.wiki.store import JsonWikiStore

    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    bridge = ScgMemoryBridge(wiki_store=wiki_store, embedder=_FakeEmbedder(), llm=None)
    bridge.resolver = ScgAnchorResolver(store)

    monkeypatch.setattr(_core.ScgCore, "store", staticmethod(lambda: store))
    monkeypatch.setattr(
        _core.ScgCore, "memory_bridge", classmethod(lambda cls, s: bridge)
    )
    # Hang the bridge off the store fixture so tests can deposit notes through it.
    store._bridge = bridge  # type: ignore[attr-defined]  # noqa: SLF001
    return store


def _step(tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_id="t", operation="execute", tool_input=tool_input)


def _run(tool: ScgObserveTool, tool_input: dict) -> dict:
    """Invoke ``tool.handle`` and parse its MockSpeaker dict payload."""
    speaker = asyncio.run(tool.handle(_step(tool_input)))
    return ast.literal_eval(speaker.content)


def _one(out: dict) -> dict:
    """The single observed node from a one-ref call."""
    assert out["count"] == 1
    return out["observed"][0]


# ── neighborhood assembly ────────────────────────────────────────────────────


def test_observe_returns_typed_directed_neighborhood(
    patched_core: JsonScgStore,
) -> None:
    """A capability's hood carries its typed edges (kind+weight+dir) + neighbors."""
    node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": ["github#search"]}))

    assert node["found"] is True
    assert node["key"] == "github#search"
    assert node["label"] == "search"
    assert node["kind"] == "capability"
    assert node["mode"] == "rows"
    # Both directions: PRODUCES→Repo (out) + ←CONSUMES from get_issue (in).
    edges = {(e["kind"], e["dir"], e["to"]) for e in node["edges"]}
    assert ("PRODUCES", "out", "github#Repo") in edges
    assert ("CONSUMES", "in", "github#get_issue") in edges
    # Edge weight rides each row (the navigation signal).
    produces = next(e for e in node["edges"] if e["kind"] == "PRODUCES")
    assert produces["w"] == 0.9
    # Neighbor cards carry machine key + human label + kind.
    by_key = {n["key"]: n for n in node["neighbors"]}
    assert by_key["github#Repo"]["label"] == "Repo"
    assert by_key["github#Repo"]["kind"] == "entity_type"


def test_observe_includes_recipes_through_the_node(
    patched_core: JsonScgStore,
) -> None:
    """A recipe whose steps include the observed node surfaces on its hood."""
    node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": ["github#search"]}))
    assert node["recipes"] == [
        {"key": "github#search", "steps": ["github#search", "github#Repo"]}
    ]


def test_observe_resolves_by_raw_node_id(patched_core: JsonScgStore) -> None:
    """A raw 16-char node id resolves the same node as its source_key."""
    nid = ScgNode.make_id("github#search", "capability")
    node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": [nid]}))
    assert node["found"] is True
    assert node["key"] == "github#search"


def test_observe_redacts_auth_scope(patched_core: JsonScgStore) -> None:
    """A node's ``auth_scope`` descriptor never reaches the wire (spec §6)."""
    out = _run(ScgObserveTool(session_id="s1"), {"nodes": ["github#Repo"]})
    assert "oauth:repo" not in repr(out)


# ── direction filtering ──────────────────────────────────────────────────────


def test_observe_direction_outgoing_only(patched_core: JsonScgStore) -> None:
    """``outgoing`` returns only the edges leaving the node."""
    node = _one(_run(
        ScgObserveTool(session_id="s1"),
        {"nodes": ["github#search"], "direction": "outgoing"},
    ))
    dirs = {e["dir"] for e in node["edges"]}
    assert dirs == {"out"}
    assert {e["kind"] for e in node["edges"]} == {"PRODUCES"}


def test_observe_direction_incoming_only(patched_core: JsonScgStore) -> None:
    """``incoming`` returns only the edges arriving at the node."""
    node = _one(_run(
        ScgObserveTool(session_id="s1"),
        {"nodes": ["github#search"], "direction": "incoming"},
    ))
    dirs = {e["dir"] for e in node["edges"]}
    assert dirs == {"in"}
    assert {e["kind"] for e in node["edges"]} == {"CONSUMES"}


# ── edge-kind selective retrieval ────────────────────────────────────────────


def test_observe_edge_kinds_filter(patched_core: JsonScgStore) -> None:
    """``edge_kinds`` narrows the returned hops to just those kinds (stage 2)."""
    node = _one(_run(
        ScgObserveTool(session_id="s1"),
        {"nodes": ["github#search"], "edge_kinds": ["PRODUCES"]},
    ))
    assert {e["kind"] for e in node["edges"]} == {"PRODUCES"}
    assert node["mode"] == "rows"


# ── two-stage survey (kinds_only) ────────────────────────────────────────────


def test_observe_surveys_a_large_unfiltered_neighborhood(
    patched_core: JsonScgStore,
) -> None:
    """Above the degree threshold + no filter ⇒ a ``kinds_only`` rollup, not rows."""
    # Fan out one capability to (_SURVEY_THRESHOLD + 5) PRODUCES field neighbors.
    hub = ScgNode(source_key="big#hub", kind="capability",
                  source_id="big", name="hub")
    nodes = [hub]
    edges = []
    for i in range(_SURVEY_THRESHOLD + 5):
        key = f"big#f{i}"
        nodes.append(ScgNode(source_key=key, kind="field", source_id="big",
                             name=f"f{i}"))
        edges.append(ScgEdge(source="big#hub", target=key, kind="PRODUCES"))
    patched_core.upsert_nodes(nodes)
    patched_core.upsert_edges(edges)

    node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": ["big#hub"]}))

    assert node["mode"] == "kinds_only"
    assert "edges" not in node  # instances withheld until the agent filters.
    assert node["survey"]["edgeKinds"]["PRODUCES→"] == _SURVEY_THRESHOLD + 5
    assert node["survey"]["neighborKinds"]["field"] == _SURVEY_THRESHOLD + 5
    assert "edge_kinds" in node["hint"]
    # Re-calling WITH the filter switches to instance rows (selective retrieval).
    filtered = _one(_run(
        ScgObserveTool(session_id="s1"),
        {"nodes": ["big#hub"], "edge_kinds": ["PRODUCES"]},
    ))
    assert filtered["mode"] == "rows"
    assert len(filtered["edges"]) == _SURVEY_THRESHOLD + 5


# ── scope filtering ──────────────────────────────────────────────────────────


def test_observe_drops_out_of_scope_hops(patched_core: JsonScgStore) -> None:
    """Under a github-only ``ScgScope``, the RESOLVES_TO into slack is dropped."""
    with ScgScope.use(["github"]):
        node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": ["github#Repo"]}))

    targets = {e["to"] for e in node["edges"]}
    assert "slack#Channel" not in targets
    # The in-scope incoming PRODUCES from github#search survives.
    assert ("PRODUCES", "in", "github#search") in {
        (e["kind"], e["dir"], e["to"]) for e in node["edges"]
    }


def test_observe_unscoped_keeps_cross_source_hop(patched_core: JsonScgStore) -> None:
    """With no active scope, the cross-source RESOLVES_TO is routable (visible)."""
    node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": ["github#Repo"]}))
    targets = {e["to"] for e in node["edges"]}
    assert "slack#Channel" in targets


# ── memory notes attached ────────────────────────────────────────────────────


def test_observe_attaches_anchored_memory_notes(patched_core: JsonScgStore) -> None:
    """A connector note anchored to the observed node rides its hood."""
    bridge: ScgMemoryBridge = patched_core._bridge  # type: ignore[attr-defined]  # noqa: SLF001
    bridge.write_insight(
        CONNECTOR_SLUG, "github Repo is queryable by id, not free-text",
        source_keys=["github#Repo"], polarity="positive",
    )

    node = _one(_run(ScgObserveTool(session_id="s1"), {"nodes": ["github#Repo"]}))

    assert "memory" in node
    assert any("queryable by id" in m["text"] for m in node["memory"])


# ── graceful misses ──────────────────────────────────────────────────────────


def test_observe_unknown_reference_is_graceful(patched_core: JsonScgStore) -> None:
    """An unresolvable reference yields ``found: False``, never a raise."""
    out = _run(ScgObserveTool(session_id="s1"), {"nodes": ["ghost#nope"]})
    assert out["observed"] == [{"ref": "ghost#nope", "found": False}]


def test_observe_mixes_hits_and_misses(patched_core: JsonScgStore) -> None:
    """A partly-stale seed list returns per-ref results without failing the call."""
    out = _run(
        ScgObserveTool(session_id="s1"),
        {"nodes": ["github#search", "ghost#nope"]},
    )
    assert out["count"] == 2
    found = {o["ref"]: o["found"] for o in out["observed"]}
    assert found == {"github#search": True, "ghost#nope": False}


def test_observe_rejects_empty_nodes(patched_core: JsonScgStore) -> None:
    """An empty ``nodes`` list fails validation at the boundary."""
    out = _run(ScgObserveTool(session_id="s1"), {"nodes": []})
    assert out["error"]["code"] == "validation"


def test_observe_rejects_unknown_field(patched_core: JsonScgStore) -> None:
    """``extra="forbid"`` surfaces as a structured validation error."""
    out = _run(ScgObserveTool(session_id="s1"), {"nodes": ["x#y"], "bogus": 1})
    assert out["error"]["code"] == "validation"
