"""Tests for the builtin ``scg`` plugin — deterministic tool handlers + AgentDefs.

These exercise the SessionTool handlers against a seeded JSON-backed
:class:`ScgStore` end-to-end (no LLM, no network, no MongoDB) and assert the
AgentDefs load and gate on the ``scg`` capability — mirroring the wiki plugin's
``tests/wiki/test_agent_defs.py`` + per-tool handler tests.

The ONLY seam mocked is :class:`ScgCore` (the plugin↔API-core bridge): its
accessors are monkeypatched to inject the tmp store + a deterministic fake
embedder + a fake memory bridge, so no real wiki ``Embedder`` (litellm) or wiki
memory substrate is ever constructed. The deterministic core classes
(parser/router/aligner) run for real over the tmp store.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mewbo_core.agent_registry import parse_agent_def
from mewbo_core.capabilities import filter_by_capabilities
from mewbo_core.session_tools import SessionTool
from mewbo_graph.plugins.scg import _core
from mewbo_graph.plugins.scg.build_structure import ScgBuildStructureTool
from mewbo_graph.plugins.scg.finalize_map import ScgFinalizeMapTool
from mewbo_graph.plugins.scg.introspect_source import ScgIntrospectSourceTool
from mewbo_graph.plugins.scg.link_entities import ScgLinkEntitiesTool
from mewbo_graph.plugins.scg.memory import ScgMemoryTool
from mewbo_graph.plugins.scg.route import ScgRouteTool
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import (
    RouteRecipe,
    ScgEdge,
    ScgEmbedding,
    ScgNode,
)

PLUGIN_DIR = Path("packages/mewbo_graph/src/mewbo_graph/plugins/scg")
AGENTS_DIR = PLUGIN_DIR / "agents"


# ── Fakes (deterministic, no network) ───────────────────────────────────────


class _FakeEmbedder:
    """The wiki ``Embedder`` surface the parser + router need (no network).

    ``embed_nodes`` returns one fixed-dim row per item; ``embed_query`` maps a
    known string to a fixed vector (else zeros). Deterministic so route ordering
    and embedding upserts are stable.
    """

    model = "fake-embed"

    def __init__(self, query_table: dict[str, list[float]] | None = None) -> None:
        self._table = query_table or {}

    def embed_nodes(
        self, items: list[tuple[str, str]], *, slug: str = ""
    ) -> list[Any]:
        return [
            SimpleNamespace(node_id=nid, vector=[1.0, 0.0], dim=2)
            for nid, _text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        return self._table.get(text, [0.0, 0.0])


class _FakeIngestResult:
    """Stand-in for ``wiki.memory.IngestResult`` (the bridge write return)."""

    def __init__(self) -> None:
        self.ok = True
        self.claims = [SimpleNamespace(action="created", node_id="note1")]


class _FakeMemoryBridge:
    """Records writes + returns canned insights (the ``ScgMemoryBridge`` seam)."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, list[str]]] = []
        # Full kwargs of the most recent write (polarity/workspace/labels) so
        # attribution + polarity assertions can read them without the positional
        # tuple shape changing.
        self.write_kwargs: list[dict[str, Any]] = []

    def write_insight(
        self, slug: str, content: str, *, source_keys: list[str], **kw: Any
    ) -> _FakeIngestResult:
        self.writes.append((slug, content, list(source_keys)))
        self.write_kwargs.append(dict(kw))
        return _FakeIngestResult()

    def read_insights(self, slug: str, query_vec: list[float], *, k: int = 10) -> list[Any]:
        return [SimpleNamespace(node_id="note1", content="github#search is bound by repo")]


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A fresh JSON-backed SCG store under a throwaway temp dir."""
    return JsonScgStore(root_dir=tmp_path / "scg")


@pytest.fixture()
def patched_core(
    monkeypatch: pytest.MonkeyPatch, store: JsonScgStore
) -> JsonScgStore:
    """Point every ``ScgCore`` accessor at the tmp store + fake embedder.

    The deterministic parser/router/aligner run for real over the tmp store; only
    the embedder (litellm) and the wiki memory substrate are faked.
    """
    from mewbo_graph.scg.entity_resolution import TypeAligner
    from mewbo_graph.scg.parser import ScgParser
    from mewbo_graph.scg.providers import StructureProviderRegistry
    from mewbo_graph.scg.router import ScgRouter
    from mewbo_graph.scg.types import SourceDescriptor

    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})

    monkeypatch.setattr(_core.ScgCore, "store", staticmethod(lambda: store))
    monkeypatch.setattr(_core.ScgCore, "embedder", staticmethod(lambda: embedder))
    monkeypatch.setattr(
        _core.ScgCore,
        "source_descriptor",
        staticmethod(
            lambda *, source_id, source_type, raw: SourceDescriptor(
                source_id=source_id, source_type=source_type, raw=raw
            )
        ),
    )

    def _parser(s: JsonScgStore) -> ScgParser:
        registry = StructureProviderRegistry.with_defaults()
        return ScgParser(
            store=s,
            providers=registry.providers(),
            embedder=embedder,
            aligner=TypeAligner(store=s),
        )

    monkeypatch.setattr(_core.ScgCore, "parser", classmethod(lambda cls, s: _parser(s)))
    monkeypatch.setattr(
        _core.ScgCore,
        "router",
        staticmethod(lambda s: ScgRouter(store=s, embedder=embedder)),
    )
    return store


def _step(tool_input: dict) -> SimpleNamespace:
    """A minimal ActionStep stand-in carrying ``tool_input`` (wiki test shape)."""
    return SimpleNamespace(tool_id="t", operation="execute", tool_input=tool_input)


def _run(tool: SessionTool, tool_input: dict) -> dict:
    """Invoke ``tool.handle`` and parse its MockSpeaker dict payload."""
    speaker = asyncio.run(tool.handle(_step(tool_input)))
    return ast.literal_eval(speaker.content)


def _mcp_descriptor_raw() -> dict:
    """A tiny MCP-tool-list descriptor: one tool, one bound input, one output."""
    return {
        "tools": [
            {
                "name": "search",
                "description": "Search repositories",
                "inputSchema": {
                    "type": "object",
                    "properties": {"repo": {"type": "string"}},
                    "required": ["repo"],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            }
        ]
    }


# ── scg_introspect_source ────────────────────────────────────────────────────


def test_introspect_source_persists_descriptor(patched_core: JsonScgStore) -> None:
    """The descriptor is validated + upserted onto the store."""
    tool = ScgIntrospectSourceTool(session_id="s1")
    out = _run(
        tool,
        {
            "source_id": "github",
            "source_type": "mcp_tool_list",
            "raw": _mcp_descriptor_raw(),
        },
    )

    assert out == {
        "accepted": True,
        "source_id": "github",
        "source_type": "mcp_tool_list",
    }
    sources = patched_core.list_sources()
    assert [s.source_id for s in sources] == ["github"]
    assert sources[0].source_type == "mcp_tool_list"


def test_introspect_source_rejects_unknown_field(patched_core: JsonScgStore) -> None:
    """``extra="forbid"`` surfaces as a structured validation error."""
    tool = ScgIntrospectSourceTool(session_id="s1")
    out = _run(tool, {"source_id": "x", "source_type": "y", "raw": {}, "bogus": 1})

    assert out["error"]["code"] == "validation"
    assert patched_core.list_sources() == []


# ── scg_build_structure ──────────────────────────────────────────────────────


def test_build_structure_parses_into_graph(patched_core: JsonScgStore) -> None:
    """An introspected MCP source parses into capability nodes + edges + embeddings."""
    ScgIntrospectSourceTool(session_id="s1")  # ensure import side-effects are clean
    _run(
        ScgIntrospectSourceTool(session_id="s1"),
        {
            "source_id": "github",
            "source_type": "mcp_tool_list",
            "raw": _mcp_descriptor_raw(),
        },
    )

    out = _run(ScgBuildStructureTool(session_id="s1"), {"source_id": "github"})

    assert out["source_id"] == "github"
    # one source node + one capability node = 2 nodes.
    assert out["nodeCount"] == 2
    assert out["edgeCount"] >= 2  # HAS_ENTITY + SUPPORTS_QUERY (+ PRODUCES)
    nodes = patched_core.query_nodes(source_id="github")
    assert {n.kind for n in nodes} == {"source", "capability"}
    # Embeddings were upserted via the fake embedder (best-effort path ran).
    assert len(patched_core.list_embeddings()) == 2


def test_build_structure_missing_descriptor_is_not_found(
    patched_core: JsonScgStore,
) -> None:
    """Parsing a source that was never introspected returns a not_found error."""
    out = _run(ScgBuildStructureTool(session_id="s1"), {"source_id": "ghost"})
    assert out["error"]["code"] == "not_found"


# ── scg_link_entities ────────────────────────────────────────────────────────


def test_link_entities_emits_resolves_to_across_sources(
    patched_core: JsonScgStore,
) -> None:
    """Two sources with an overlapping entity type yield a RESOLVES_TO edge."""
    # Seed two entity types sharing a field name → TypeAligner confident match.
    def _entity(source_id: str, name: str) -> ScgNode:
        from mewbo_graph.scg.types import CapabilityBinding

        return ScgNode(
            source_key=f"{source_id}#{name}",
            kind="entity_type",
            source_id=source_id,
            name=name,
            bindings=[
                CapabilityBinding(field_key=f"{source_id}#{name}.title", mode="bound"),
                CapabilityBinding(field_key=f"{source_id}#{name}.status", mode="bound"),
            ],
        )

    patched_core.upsert_nodes([_entity("jira", "Issue"), _entity("linear", "Issue")])

    out = _run(
        ScgLinkEntitiesTool(session_id="s1"), {"source_ids": ["jira", "linear"]}
    )

    assert out["resolvesToCount"] >= 1
    resolves = patched_core.list_edges(kind="RESOLVES_TO")
    assert resolves and resolves[0].method == "type_align"


def test_link_entities_empty_graph_is_noop(patched_core: JsonScgStore) -> None:
    """No sources ⇒ both passes are deterministic no-ops, not a raise."""
    out = _run(ScgLinkEntitiesTool(session_id="s1"), {"source_ids": []})
    assert out == {"resolvesToCount": 0, "consumesCount": 0, "source_ids": []}


# ── scg_finalize_map ─────────────────────────────────────────────────────────


def test_finalize_map_reports_counts(
    patched_core: JsonScgStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finalize tallies the SCG; emit_phase is best-effort (None here ⇒ skipped)."""
    # No API runtime in the unit env → emit_phase returns None (phase skipped).
    monkeypatch.setattr(_core.ScgCore, "emit_phase", staticmethod(lambda *_a: None))
    _run(
        ScgIntrospectSourceTool(session_id="s1"),
        {
            "source_id": "github",
            "source_type": "mcp_tool_list",
            "raw": _mcp_descriptor_raw(),
        },
    )
    _run(ScgBuildStructureTool(session_id="s1"), {"source_id": "github"})

    out = _run(ScgFinalizeMapTool(session_id="s1"), {"job_id": "job-1"})

    assert out["complete"] is True
    assert out["job_id"] == "job-1"
    assert out["sourceCount"] == 1
    assert out["nodeCount"] == 2
    assert out["edgeCount"] >= 2
    assert out["phaseEmitted"] is False


def test_finalize_map_calls_emit_phase(
    patched_core: JsonScgStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the API store is present, emit_phase is invoked with the finalize phase."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _core.ScgCore,
        "emit_phase",
        staticmethod(lambda job_id, phase: calls.append((job_id, phase)) or 7),
    )

    out = _run(ScgFinalizeMapTool(session_id="s1"), {"job_id": "job-9"})

    assert calls == [("job-9", "finalize")]
    assert out["phaseEmitted"] is True


# ── scg_route ────────────────────────────────────────────────────────────────


def _seed_route_graph(store: JsonScgStore) -> None:
    """A one-capability graph with a recipe + embedding for route() to rank."""
    cap = ScgNode(
        source_key="github#search",
        kind="capability",
        source_id="github",
        name="search",
    )
    store.upsert_nodes([cap])
    store.upsert_embeddings(
        [ScgEmbedding(node_id=cap.node_id, vector=[1.0, 0.0], model="m", dim=2)]
    )
    store.upsert_edges(
        [ScgEdge(source="github#search", target="github#Repo", kind="PRODUCES")]
    )
    store.upsert_recipes(
        [RouteRecipe(source_key="github#search", steps=["github#search", "github#Repo"])]
    )


def test_route_returns_ranked_recipes(patched_core: JsonScgStore) -> None:
    """A query near the seeded capability routes to its recipe (typed JSON)."""
    _seed_route_graph(patched_core)

    out = _run(ScgRouteTool(session_id="s1"), {"query": "find repos", "k": 5})

    assert out["count"] == 1
    assert out["recipes"][0]["source_key"] == "github#search"
    assert out["recipes"][0]["steps"] == ["github#search", "github#Repo"]


def test_route_recipes_carry_probe_tool_scope(patched_core: JsonScgStore) -> None:
    """Each recipe lists its sources + EVERY capability of those sources.

    The probe's allowed_tools scope is copied from the route result, never
    inferred — a probe scoped to only the step tools cannot chain a follow-up
    lookup within its own source (the first live run failed exactly this way).
    """
    _seed_route_graph(patched_core)
    sibling = ScgNode(
        source_key="github#get_issue",
        kind="capability",
        source_id="github",
        name="get_issue",
    )
    patched_core.upsert_nodes([sibling])

    out = _run(ScgRouteTool(session_id="s1"), {"query": "find repos", "k": 5})

    recipe = out["recipes"][0]
    assert recipe["source_ids"] == ["github"]
    assert recipe["source_capabilities"] == ["get_issue", "search"]
    # The EXECUTABLE allowlist — real mcp_<server>_<tool> ids, never graph
    # source_keys (passing source_keys granted nothing: run-c52e9597).
    assert recipe["allowed_tool_ids"] == ["mcp_github_get_issue", "mcp_github_search"]


def test_route_empty_graph_returns_no_recipes(patched_core: JsonScgStore) -> None:
    """An empty SCG yields zero routes (not an error)."""
    out = _run(ScgRouteTool(session_id="s1"), {"query": "anything"})
    assert out == {"count": 0, "recipes": []}


def test_route_recipes_carry_memory_hints(
    monkeypatch: pytest.MonkeyPatch, patched_core: JsonScgStore, tmp_path: Path
) -> None:
    """A routed recipe surfaces its anchored connector insights as compact hints (#76).

    Drives the REAL memory-aware router (a real bridge over a real wiki JSON
    store) through ``ScgCore.router`` so the ``scg_route`` projection carries
    ``memory_hints`` for the pathway, capped + compact, without a second lookup.
    """
    from mewbo_graph.scg.memory_bridge import (
        CONNECTOR_SLUG,
        ScgAnchorResolver,
        ScgMemoryBridge,
    )
    from mewbo_graph.scg.router import ScgRouter
    from mewbo_graph.wiki.store import JsonWikiStore

    _seed_route_graph(patched_core)
    # The note anchors on the entity_type surface (the resolver's anchor target).
    patched_core.upsert_nodes([
        ScgNode(source_key="github#search", kind="entity_type",
                source_id="github", name="search"),
    ])

    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})
    # Embedder shared by the parser-style node embed + query; deposit a positive
    # note so a hint exists for the github#search pathway.
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    bridge = ScgMemoryBridge(wiki_store=wiki_store, embedder=embedder, llm=None)
    bridge.resolver = ScgAnchorResolver(patched_core)
    bridge.write_insight(
        CONNECTOR_SLUG, "github#search is queryable by repo, not free-text",
        source_keys=["github#search"], polarity="positive",
    )
    monkeypatch.setattr(
        _core.ScgCore, "router",
        staticmethod(lambda s: ScgRouter(store=s, embedder=embedder, memory_bridge=bridge)),
    )

    out = _run(ScgRouteTool(session_id="s1"), {"query": "find repos", "k": 5})

    recipe = out["recipes"][0]
    assert recipe["source_key"] == "github#search"
    hints = recipe.get("memory_hints")
    assert hints, "expected anchored memory hints on the routed recipe"
    assert hints[0]["source_key"] == "github#search"
    assert "free-text" in hints[0]["text"]
    # Compact: capped per recipe (the projection never dumps the whole corpus).
    assert len(hints) <= 3


# ── scg_memory ───────────────────────────────────────────────────────────────


@pytest.fixture()
def patched_memory(
    monkeypatch: pytest.MonkeyPatch, patched_core: JsonScgStore
) -> _FakeMemoryBridge:
    """Inject a fake ScgMemoryBridge at the ScgCore seam (no wiki substrate)."""
    bridge = _FakeMemoryBridge()
    monkeypatch.setattr(
        _core.ScgCore, "memory_bridge", classmethod(lambda cls, s: bridge)
    )
    return bridge


def test_memory_write_deposits_insight(patched_memory: _FakeMemoryBridge) -> None:
    """A write routes to the bridge with the content + anchors and reports claims."""
    out = _run(
        ScgMemoryTool(session_id="s1"),
        {
            "operation": "write",
            "content": "github#search is queryable by repo, not free-text",
            "source_keys": ["github#search"],
        },
    )

    assert out["operation"] == "write"
    assert out["ok"] is True
    assert out["claims"] == [{"action": "created", "node_id": "note1"}]
    assert patched_memory.writes == [
        (
            "__connector__",
            "github#search is queryable by repo, not free-text",
            ["github#search"],
        )
    ]


def test_memory_write_unscoped_falls_back_to_session_attribution(
    patched_memory: _FakeMemoryBridge,
) -> None:
    """An UNSCOPED deposit (no workspace bound) attributes to ``session:<id>``.

    #83-B: ordinary sessions (CLI/console/channel) deposit insights with no
    ``ScgScope`` workspace bound, so attribution falls back to the session id —
    reusing the same ``labels`` mechanism as ``ws:<id>`` (no new field). The
    workspace kwarg is ``None`` (no partition) and a ``session:s1`` label rides
    the deposit so the learned layer stays attributable to which task curated it.
    """
    from mewbo_graph.scg.scope import ScgScope

    # No ScgScope.use(...) → ScgScope.workspace() is None (the unscoped default).
    assert ScgScope.workspace() is None

    _run(
        ScgMemoryTool(session_id="s1"),
        {
            "operation": "write",
            "content": "github#search is queryable by repo, not free-text",
            "source_keys": ["github#search"],
        },
    )

    kw = patched_memory.write_kwargs[-1]
    assert kw["workspace"] is None
    assert kw["labels"] == ["session:s1"]


def test_memory_write_workspace_bound_keeps_ws_attribution(
    patched_memory: _FakeMemoryBridge,
) -> None:
    """A workspace-bound deposit attributes to the workspace, not the session.

    When a run binds an ``ScgScope`` (a workspace-scoped search/structured run),
    the ambient workspace is the attribution and the ``session:<id>`` fallback is
    NOT added — preserving today's behaviour for scoped runs.
    """
    from mewbo_graph.scg.scope import ScgScope

    with ScgScope.use(["github"], workspace="ws-42"):
        _run(
            ScgMemoryTool(session_id="s1"),
            {
                "operation": "write",
                "content": "github#search is queryable by repo",
                "source_keys": ["github#search"],
            },
        )

    kw = patched_memory.write_kwargs[-1]
    assert kw["workspace"] == "ws-42"
    # No session fallback label when a workspace is bound (ws:<id> is added by
    # the bridge from the workspace kwarg, not here).
    assert kw["labels"] is None


def test_memory_read_returns_insights(patched_memory: _FakeMemoryBridge) -> None:
    """A read embeds the query (fake embedder) and returns the bridge's insights."""
    out = _run(
        ScgMemoryTool(session_id="s1"), {"operation": "read", "query": "find repos", "k": 5}
    )

    assert out["operation"] == "read"
    assert out["count"] == 1
    assert out["insights"][0]["content"] == "github#search is bound by repo"


def test_memory_write_requires_content_and_anchors(
    patched_memory: _FakeMemoryBridge,
) -> None:
    """A write missing content/source_keys fails validation at the boundary."""
    out = _run(ScgMemoryTool(session_id="s1"), {"operation": "write", "content": "x"})
    assert out["error"]["code"] == "validation"
    assert patched_memory.writes == []


def test_memory_write_succeeds_without_wiki_api_runtime(
    monkeypatch: pytest.MonkeyPatch, patched_core: JsonScgStore, tmp_path: Path
) -> None:
    """A ``scg_memory`` write works when the wiki API runtime is NOT initialised.

    Regression (fix #4): the bridge used to read the wiki store off
    ``wiki.routes._runtime``, which is ``None`` for any deployment that never
    started the wiki API — silently breaking every connector-memory write. The
    bridge now builds the store via the wiki STORE FACTORY directly, so the real
    ``ScgCore.memory_bridge`` path succeeds with ``_runtime`` left ``None``.
    """
    from mewbo_api.wiki import routes as wiki_routes
    from mewbo_graph.scg.memory_bridge import CONNECTOR_SLUG
    from mewbo_graph.wiki.store import JsonWikiStore

    # The wiki API runtime is explicitly NOT initialised.
    monkeypatch.setattr(wiki_routes, "_runtime", None, raising=False)
    # The factory the bridge now uses — point it at a throwaway JSON store so no
    # real Mongo/cache_dir is touched (mirrors create_wiki_store's JSON default).
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    monkeypatch.setattr(
        "mewbo_graph.wiki.store.create_wiki_store", lambda: wiki_store
    )
    # An SCG node so the anchor resolves to a live node → a live ANCHORS edge.
    store: JsonScgStore = patched_core
    store.upsert_nodes(
        [ScgNode(source_key="github#Repo", kind="entity_type", source_id="github", name="Repo")]
    )

    out = _run(
        ScgMemoryTool(session_id="s1"),
        {
            "operation": "write",
            "content": "github#Repo is queryable by id",
            "source_keys": ["github#Repo"],
        },
    )

    # The real bridge ran (no fake injected) and the deposit succeeded — no error.
    assert "error" not in out
    assert out["operation"] == "write"
    assert out["ok"] is True
    assert out["claims"] and out["claims"][0]["action"] in ("created", "merged")
    # The anchor resolved against THIS SCG store (a live ANCHORS edge exists).
    node_id = out["claims"][0]["node_id"]
    edges = wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=node_id)
    assert [e.target for e in edges if e.type == "ANCHORS"] == ["github#Repo"]


# ── AgentDefs — load + capability gating (mirrors wiki test_agent_defs.py) ────


def test_scg_mapper_agent_def_loads() -> None:
    """The mapper AgentDef loads with the map tools + the scg capability."""
    agent_def = parse_agent_def(AGENTS_DIR / "scg-mapper.md", source="plugin:scg")
    assert agent_def is not None
    assert agent_def.name == "scg-mapper"
    assert agent_def.requires_capabilities == ("scg",)
    expected = {
        "scg_introspect_source",
        "scg_build_structure",
        "scg_link_entities",
        "scg_finalize_map",
        "scg_memory",
    }
    assert expected.issubset(set(agent_def.allowed_tools or []))
    for kw in ["scg_introspect_source", "scg_build_structure", "scg_finalize_map"]:
        assert kw in agent_def.body


def test_scg_search_agent_def_loads() -> None:
    """The search AgentDef drives route → spawn → synthesize → deposit."""
    agent_def = parse_agent_def(AGENTS_DIR / "scg-search.md", source="plugin:scg")
    assert agent_def is not None
    assert agent_def.name == "scg-search"
    assert agent_def.requires_capabilities == ("scg",)
    expected = {"scg_route", "scg_memory", "spawn_agent", "check_agents"}
    assert expected.issubset(set(agent_def.allowed_tools or []))
    # Tier is a budget knob, not verification rounds — the playbook says so.
    assert "scg_route" in agent_def.body
    assert "scg-path-probe" in agent_def.body
    assert "verifier" in agent_def.body.lower()


def test_scg_path_probe_agent_def_loads() -> None:
    """The probe is a leaf executor: no spawn_agent, no route in its allowed tools."""
    agent_def = parse_agent_def(AGENTS_DIR / "scg-path-probe.md", source="plugin:scg")
    assert agent_def is not None
    assert agent_def.name == "scg-path-probe"
    assert agent_def.requires_capabilities == ("scg",)
    allowed = set(agent_def.allowed_tools or [])
    assert "spawn_agent" not in allowed
    assert "scg_route" not in allowed
    assert "gaps remaining" in agent_def.body.lower()


def test_agent_defs_gate_on_scg_capability() -> None:
    """All three AgentDefs are hidden without the scg capability, visible with it."""
    defs = [
        parse_agent_def(AGENTS_DIR / name, source="plugin:scg")
        for name in ("scg-mapper.md", "scg-search.md", "scg-path-probe.md")
    ]
    assert all(d is not None for d in defs)
    assert filter_by_capabilities(defs, []) == []
    assert len(filter_by_capabilities(defs, ["scg"])) == 3


def test_plugin_manifest_registers_tools_and_agents() -> None:
    """plugin.json declares every SCG tool + AgentDef under the scg capability."""
    manifest = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "scg"
    assert manifest["requires-capabilities"] == ["scg"]
    tool_ids = {entry["tool_id"] for entry in manifest["session_tools"]}
    assert tool_ids == {
        "scg_introspect_source",
        "scg_build_structure",
        "scg_link_entities",
        "scg_finalize_map",
        "scg_route",
        "scg_observe",
        "scg_memory",
        # Shared abstract-entity tools (#35) registered under scg too so search
        # can read/mint the same holistic entity graph as the wiki.
        "mint_entity",
        "relate_entities",
        "resolve_entity",
    }
    agent_paths = {entry["path"] for entry in manifest["agents"]}
    assert agent_paths == {
        "agents/scg-mapper.md",
        "agents/scg-search.md",
        "agents/scg-path-probe.md",
    }
