"""JSON-backend tests for ScgStore — the SCG structure persistence layer.

Exercises the filesystem driver end-to-end in a tmp dir (no MongoDB required;
the Mongo driver is skipped when pymongo is absent, mirroring the run-store
tests). Covers upsert + get + query (by source_id / kind / name), neighbors,
cosine-ranked vector_search, scoped delete_source, and reset_for_tests
isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg import store as scg_store
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import (
    RouteRecipe,
    ScgEdge,
    ScgEmbedding,
    ScgNode,
    SourceDescriptor,
)

# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A fresh JSON-backed SCG store under a throwaway temp dir."""
    return JsonScgStore(root_dir=tmp_path / "scg")


def _node(source_id: str, name: str, kind: str = "entity_type") -> ScgNode:
    return ScgNode(
        source_key=f"{source_id}#{name}",
        kind=kind,  # type: ignore[arg-type]
        source_id=source_id,
        name=name,
    )


# ── upsert + get ────────────────────────────────────────────────────────────


def test_upsert_and_get_node(store: JsonScgStore) -> None:
    """A node persists and is retrievable by its derived node_id."""
    node = _node("github", "Repo")
    store.upsert_nodes([node])
    got = store.get_node(node.node_id)
    assert got is not None
    assert got.source_key == "github#Repo"
    assert got.node_id == node.node_id


def test_upsert_is_idempotent_keyed_on_node_id(store: JsonScgStore) -> None:
    """Re-upserting the same node_id replaces, not duplicates."""
    store.upsert_nodes([_node("github", "Repo")])
    updated = _node("github", "Repo")
    updated = updated.model_copy(update={"doc": "the repo entity"})
    store.upsert_nodes([updated])
    rows = store.query_nodes(source_id="github")
    assert len(rows) == 1
    assert rows[0].doc == "the repo entity"


def test_get_node_missing_returns_none(store: JsonScgStore) -> None:
    """get_node returns None for an unknown id."""
    assert store.get_node("does-not-exist") is None


# ── query_nodes filters ─────────────────────────────────────────────────────


def test_query_nodes_by_source_id(store: JsonScgStore) -> None:
    """query_nodes filters to a single source_id."""
    store.upsert_nodes(
        [_node("github", "Repo"), _node("github", "Issue"), _node("slack", "Channel")]
    )
    gh = store.query_nodes(source_id="github")
    assert {n.name for n in gh} == {"Repo", "Issue"}


def test_query_nodes_by_kind(store: JsonScgStore) -> None:
    """query_nodes filters to a single kind."""
    store.upsert_nodes(
        [
            _node("github", "Repo", kind="entity_type"),
            _node("github", "search", kind="capability"),
        ]
    )
    caps = store.query_nodes(kind="capability")
    assert [n.name for n in caps] == ["search"]


def test_query_nodes_by_name_contains(store: JsonScgStore) -> None:
    """query_nodes matches a case-insensitive name substring."""
    store.upsert_nodes([_node("github", "PullRequest"), _node("github", "Issue")])
    hits = store.query_nodes(name_contains="request")
    assert [n.name for n in hits] == ["PullRequest"]


def test_query_nodes_combined_filters(store: JsonScgStore) -> None:
    """Filters compose (source_id AND kind)."""
    store.upsert_nodes(
        [
            _node("github", "Repo", kind="entity_type"),
            _node("github", "search", kind="capability"),
            _node("slack", "search", kind="capability"),
        ]
    )
    hits = store.query_nodes(source_id="github", kind="capability")
    assert [n.name for n in hits] == ["search"]


# ── edges + neighbors ───────────────────────────────────────────────────────


def test_upsert_edges_and_list_edges(store: JsonScgStore) -> None:
    """Edges persist and list_edges filters by source/kind."""
    store.upsert_edges(
        [
            ScgEdge(source="github#Repo", target="github#Repo.id", kind="HAS_FIELD"),
            ScgEdge(source="github#search", target="github#Issue", kind="PRODUCES"),
        ]
    )
    assert len(store.list_edges()) == 2
    assert len(store.list_edges(kind="HAS_FIELD")) == 1
    assert len(store.list_edges(source="github#search")) == 1


def test_upsert_edges_idempotent_on_triple(store: JsonScgStore) -> None:
    """An edge is keyed on (source, target, kind) — re-upsert replaces."""
    store.upsert_edges(
        [ScgEdge(source="github#Repo", target="github#Repo.id", kind="HAS_FIELD")]
    )
    store.upsert_edges(
        [
            ScgEdge(
                source="github#Repo",
                target="github#Repo.id",
                kind="HAS_FIELD",
                weight=0.5,
            )
        ]
    )
    edges = store.list_edges()
    assert len(edges) == 1
    assert edges[0].weight == 0.5


def test_neighbors_returns_outgoing_edges(store: JsonScgStore) -> None:
    """neighbors returns the edges whose source is the given source_key."""
    store.upsert_edges(
        [
            ScgEdge(source="github#Repo", target="github#Repo.id", kind="HAS_FIELD"),
            ScgEdge(source="github#Repo", target="github#Repo.name", kind="HAS_FIELD"),
            ScgEdge(source="github#Issue", target="github#Issue.id", kind="HAS_FIELD"),
        ]
    )
    nbrs = store.neighbors("github#Repo")
    assert {e.target for e in nbrs} == {"github#Repo.id", "github#Repo.name"}


# ── recipes ─────────────────────────────────────────────────────────────────


def test_upsert_recipes_persist(store: JsonScgStore) -> None:
    """Recipes persist and survive a delete_source scoped wipe of their source."""
    store.upsert_recipes(
        [RouteRecipe(source_key="github#find_pr", steps=["github#search", "github#get_pr"])]
    )
    # No direct list accessor is required by the contract; delete_source proves
    # the recipe was stored (it counts toward the scoped deletion below).
    deleted = store.delete_source("github")
    assert deleted >= 1


# ── embeddings + vector_search ──────────────────────────────────────────────


def test_list_embeddings_roundtrip(store: JsonScgStore) -> None:
    """Embeddings persist keyed on node_id and reload verbatim."""
    emb = ScgEmbedding(node_id="n1", vector=[1.0, 0.0], model="m", dim=2)
    store.upsert_embeddings([emb])
    rows = store.list_embeddings()
    assert len(rows) == 1
    assert rows[0].node_id == "n1"
    assert rows[0].vector == [1.0, 0.0]


def test_vector_search_ranks_by_cosine(store: JsonScgStore) -> None:
    """vector_search returns (node_id, score) ordered by descending cosine."""
    store.upsert_embeddings(
        [
            ScgEmbedding(node_id="aligned", vector=[1.0, 0.0], model="m", dim=2),
            ScgEmbedding(node_id="orthogonal", vector=[0.0, 1.0], model="m", dim=2),
            ScgEmbedding(node_id="opposite", vector=[-1.0, 0.0], model="m", dim=2),
        ]
    )
    ranked = store.vector_search([1.0, 0.0], k=3)
    assert [nid for nid, _ in ranked] == ["aligned", "orthogonal", "opposite"]
    assert ranked[0][1] == pytest.approx(1.0)


def test_vector_search_respects_k(store: JsonScgStore) -> None:
    """vector_search truncates to k."""
    store.upsert_embeddings(
        [
            ScgEmbedding(node_id="a", vector=[1.0, 0.0], model="m", dim=2),
            ScgEmbedding(node_id="b", vector=[0.9, 0.1], model="m", dim=2),
            ScgEmbedding(node_id="c", vector=[0.0, 1.0], model="m", dim=2),
        ]
    )
    assert len(store.vector_search([1.0, 0.0], k=2)) == 2


def test_vector_search_empty_store(store: JsonScgStore) -> None:
    """vector_search on an empty store returns []."""
    assert store.vector_search([1.0, 0.0], k=5) == []


# ── sources ─────────────────────────────────────────────────────────────────


def test_upsert_source_and_list_sources(store: JsonScgStore) -> None:
    """A source descriptor persists and lists, keyed on source_id."""
    desc = SourceDescriptor(source_id="github", source_type="openapi", raw={"v": 1})
    store.upsert_source(desc)
    sources = store.list_sources()
    assert [s.source_id for s in sources] == ["github"]
    assert sources[0].source_type == "openapi"


def test_upsert_source_idempotent(store: JsonScgStore) -> None:
    """Re-upserting a source_id replaces it."""
    store.upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )
    store.upsert_source(
        SourceDescriptor(source_id="github", source_type="graphql", raw={})
    )
    sources = store.list_sources()
    assert len(sources) == 1
    assert sources[0].source_type == "graphql"


# ── scoped delete ───────────────────────────────────────────────────────────


def test_delete_source_removes_only_that_source(store: JsonScgStore) -> None:
    """delete_source wipes nodes/edges/recipes/embeddings/source for one source only."""
    # github entities
    gh_repo = _node("github", "Repo")
    gh_issue = _node("github", "Issue", kind="capability")
    store.upsert_nodes([gh_repo, gh_issue])
    store.upsert_edges(
        [ScgEdge(source="github#Repo", target="github#Issue", kind="PRODUCES")]
    )
    store.upsert_recipes(
        [RouteRecipe(source_key="github#find", steps=["github#Repo"])]
    )
    store.upsert_embeddings(
        [ScgEmbedding(node_id=gh_repo.node_id, vector=[1.0], model="m", dim=1)]
    )
    store.upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )

    # slack entities (must survive)
    sl_chan = _node("slack", "Channel")
    store.upsert_nodes([sl_chan])
    store.upsert_edges(
        [ScgEdge(source="slack#Channel", target="slack#Channel.id", kind="HAS_FIELD")]
    )
    store.upsert_embeddings(
        [ScgEmbedding(node_id=sl_chan.node_id, vector=[0.0, 1.0], model="m", dim=2)]
    )
    store.upsert_source(
        SourceDescriptor(source_id="slack", source_type="mcp", raw={})
    )

    removed = store.delete_source("github")
    assert removed > 0

    # github gone entirely
    assert store.query_nodes(source_id="github") == []
    assert store.list_edges(source="github#Repo") == []
    assert [s.source_id for s in store.list_sources()] == ["slack"]
    assert store.get_node(gh_repo.node_id) is None

    # slack untouched
    assert {n.name for n in store.query_nodes(source_id="slack")} == {"Channel"}
    assert len(store.list_edges()) == 1
    assert [e.node_id for e in store.list_embeddings()] == [sl_chan.node_id]


def test_delete_source_unknown_returns_zero(store: JsonScgStore) -> None:
    """delete_source on a source with nothing stored returns 0."""
    assert store.delete_source("nope") == 0


# ── factory + singleton isolation ───────────────────────────────────────────


def test_reset_for_tests_isolates_store() -> None:
    """reset_for_tests swaps a fresh empty JSON store under a tmp dir."""
    scg_store.reset_for_tests()
    s1 = scg_store.get_scg_store()
    s1.upsert_nodes([_node("github", "Repo")])
    assert len(s1.query_nodes(source_id="github")) == 1

    scg_store.reset_for_tests()
    s2 = scg_store.get_scg_store()
    assert s2 is not s1
    assert s2.query_nodes(source_id="github") == []


def test_set_and_get_scg_store(tmp_path: Path) -> None:
    """set_scg_store overrides the process-wide singleton."""
    override = JsonScgStore(root_dir=tmp_path / "override")
    scg_store.set_scg_store(override)
    assert scg_store.get_scg_store() is override
    scg_store.set_scg_store(None)  # cleanup for later tests
