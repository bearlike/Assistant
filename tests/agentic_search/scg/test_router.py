"""Tests for ScgRouter — the cheap query→route mechanism over the SCG.

The router is the graph's only query-time job: control routing. It embeds the
query, vector-searches seed nodes in the store, expands along capability/route
edges, and ranks candidate RouteRecipes with a zero-LLM score
(``cosine(seed) + edge weight``). Personalized PageRank is the documented scale
seam and is deliberately NOT exercised here.

A deterministic fake embedder is injected — these tests NEVER call a real
embedding API. The JSON store runs end-to-end in a tmp dir (no MongoDB).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.router import ScgRouter
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import (
    RouteRecipe,
    ScgEdge,
    ScgEmbedding,
    ScgNode,
)

# ── Fake embedder (deterministic, no network) ───────────────────────────────


class _FakeEmbedder:
    """Maps known query strings to fixed vectors; everything else → zero vec.

    Mirrors the wiki ``Embedder`` surface the router depends on: only
    ``embed_query`` is needed. Deterministic so ranking assertions are stable.
    """

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = table or {}

    def embed_query(self, text: str) -> list[float]:
        return self.table.get(text, [0.0, 0.0])


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A fresh JSON-backed SCG store under a throwaway temp dir."""
    return JsonScgStore(root_dir=tmp_path / "scg")


def _node(source_id: str, name: str, kind: str = "capability") -> ScgNode:
    return ScgNode(
        source_key=f"{source_id}#{name}",
        kind=kind,  # type: ignore[arg-type]
        source_id=source_id,
        name=name,
    )


def _seed_graph(store: JsonScgStore) -> None:
    """A tiny two-source graph with one recipe per capability.

    github#search (a capability) is embedded near [1,0]; slack#search near
    [0,1]. Each capability has a route recipe and PRODUCES an entity.
    """
    gh_search = _node("github", "search", kind="capability")
    sl_search = _node("slack", "search", kind="capability")
    store.upsert_nodes([gh_search, sl_search, _node("github", "Repo", "entity_type")])
    store.upsert_embeddings(
        [
            ScgEmbedding(node_id=gh_search.node_id, vector=[1.0, 0.0], model="m", dim=2),
            ScgEmbedding(node_id=sl_search.node_id, vector=[0.0, 1.0], model="m", dim=2),
        ]
    )
    store.upsert_edges(
        [
            ScgEdge(source="github#search", target="github#Repo", kind="PRODUCES"),
            ScgEdge(source="slack#search", target="slack#Channel", kind="PRODUCES"),
        ]
    )
    store.upsert_recipes(
        [
            RouteRecipe(source_key="github#search", steps=["github#search", "github#Repo"]),
            RouteRecipe(source_key="slack#search", steps=["slack#search", "slack#Channel"]),
        ]
    )


# ── route() — happy path ─────────────────────────────────────────────────────


def test_route_returns_expected_recipe(store: JsonScgStore) -> None:
    """A query close to github#search routes to the github recipe first."""
    _seed_graph(store)
    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    recipes = router.route("find repos", k=5)

    assert recipes, "expected at least one route"
    assert recipes[0].source_key == "github#search"


def test_route_orders_by_relevance(store: JsonScgStore) -> None:
    """Both recipes returned, github first when query aligns with github."""
    _seed_graph(store)
    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    keys = [r.source_key for r in router.route("find repos", k=5)]

    assert keys[0] == "github#search"
    assert "slack#search" in keys
    assert keys.index("github#search") < keys.index("slack#search")


def test_route_query_aligned_with_slack(store: JsonScgStore) -> None:
    """A query aligned with slack flips the ordering — slack ranks first."""
    _seed_graph(store)
    embedder = _FakeEmbedder({"team messages": [0.0, 1.0]})
    router = ScgRouter(store=store, embedder=embedder)

    keys = [r.source_key for r in router.route("team messages", k=5)]

    assert keys[0] == "slack#search"


def test_route_respects_k(store: JsonScgStore) -> None:
    """route truncates the candidate set to k seeds."""
    _seed_graph(store)
    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    recipes = router.route("find repos", k=1)

    assert len(recipes) == 1
    assert recipes[0].source_key == "github#search"


# ── route() — edge cases ─────────────────────────────────────────────────────


def test_route_empty_graph_returns_empty(store: JsonScgStore) -> None:
    """An empty SCG yields no routes."""
    embedder = _FakeEmbedder({"anything": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    assert router.route("anything", k=5) == []


def test_route_ranking_is_stable(store: JsonScgStore) -> None:
    """Two identical calls return byte-identical ordering (deterministic)."""
    _seed_graph(store)
    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    first = [r.source_key for r in router.route("find repos", k=5)]
    second = [r.source_key for r in router.route("find repos", k=5)]

    assert first == second


def test_route_seed_without_recipe_expands_via_edges(store: JsonScgStore) -> None:
    """A seed whose own source_key has no recipe still routes via edge expansion.

    github#Repo (an entity) has no recipe of its own but is reachable from
    github#search's recipe — assembling a candidate via edge expansion.
    """
    _seed_graph(store)
    # Embed the query nearest to the Repo entity node, which has no own recipe.
    gh_repo = store.query_nodes(source_id="github", kind="entity_type")[0]
    store.upsert_embeddings(
        [ScgEmbedding(node_id=gh_repo.node_id, vector=[1.0, 0.0], model="m", dim=2)]
    )
    embedder = _FakeEmbedder({"a repo": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    recipes = router.route("a repo", k=5)

    # The entity seed reaches github#search's recipe through edge expansion.
    assert any(r.source_key == "github#search" for r in recipes)


def test_route_returns_route_recipe_instances(store: JsonScgStore) -> None:
    """route returns typed RouteRecipe objects (not dicts)."""
    _seed_graph(store)
    embedder = _FakeEmbedder({"find repos": [1.0, 0.0]})
    router = ScgRouter(store=store, embedder=embedder)

    recipes = router.route("find repos", k=5)

    assert all(isinstance(r, RouteRecipe) for r in recipes)
