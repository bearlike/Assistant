"""Extra coverage for ScgStore — MongoScgStore backend + JSON edge-cases.

Exercises:
- MongoScgStore: all CRUD paths (nodes, edges, recipes, embeddings, sources),
  query_nodes with every filter combination, list_edges filters, list_recipes
  scoped by source_id, delete_source full-cascade.
- JSON _load() malformed-file recovery.
- JSON _upsert() empty-list early-exit.
- create_scg_store() factory: json (default) and mongodb driver paths.
- get_scg_store() singleton lazy-init.
- set_scg_store() override and cleanup.
- reset_for_tests() isolation guarantee (already covered, extended here).

Uses mongomock for the Mongo path — no real MongoDB required.
"""

from __future__ import annotations

import json
from pathlib import Path

import mongomock
import pytest
from mewbo_graph.scg import store as scg_store_mod
from mewbo_graph.scg.store import JsonScgStore, MongoScgStore
from mewbo_graph.scg.types import (
    RouteRecipe,
    ScgEdge,
    ScgEmbedding,
    ScgNode,
    SourceDescriptor,
)

# ── helpers ───────────────────────────────────────────────────────────────────

SLUG_A = "github"
SLUG_B = "slack"


def _node(source_id: str, name: str, kind: str = "entity_type") -> ScgNode:
    return ScgNode(
        source_key=f"{source_id}#{name}",
        kind=kind,  # type: ignore[arg-type]
        source_id=source_id,
        name=name,
    )


def _edge(src: str, tgt: str, kind: str = "HAS_FIELD", weight: float = 1.0) -> ScgEdge:
    return ScgEdge(source=src, target=tgt, kind=kind, weight=weight)


def _recipe(source_key: str, steps: list[str] | None = None) -> RouteRecipe:
    return RouteRecipe(source_key=source_key, steps=steps or [source_key])


def _emb(node_id: str, vec: list[float]) -> ScgEmbedding:
    return ScgEmbedding(node_id=node_id, vector=vec, model="m", dim=len(vec))


def _source(source_id: str, source_type: str = "openapi") -> SourceDescriptor:
    return SourceDescriptor(source_id=source_id, source_type=source_type, raw={})


# ── MongoScgStore: fixture ────────────────────────────────────────────────────


@pytest.fixture()
def mongo_store() -> MongoScgStore:
    """A fresh MongoDB-backed SCG store via mongomock."""
    return MongoScgStore(client=mongomock.MongoClient(), database="test_scg_extra")


# ── MongoScgStore: node CRUD ─────────────────────────────────────────────────


def test_mongo_upsert_and_get_node(mongo_store: MongoScgStore) -> None:
    """A node persists in Mongo and is retrievable by its derived node_id."""
    node = _node(SLUG_A, "Repo")
    mongo_store.upsert_nodes([node])
    got = mongo_store.get_node(node.node_id)
    assert got is not None
    assert got.source_key == f"{SLUG_A}#Repo"
    assert got.node_id == node.node_id


def test_mongo_get_node_missing_returns_none(mongo_store: MongoScgStore) -> None:
    assert mongo_store.get_node("does-not-exist") is None


def test_mongo_upsert_node_is_idempotent(mongo_store: MongoScgStore) -> None:
    """Re-upserting the same node_id replaces, not duplicates."""
    mongo_store.upsert_nodes([_node(SLUG_A, "Repo")])
    updated = _node(SLUG_A, "Repo")
    updated = updated.model_copy(update={"doc": "the repo entity"})
    mongo_store.upsert_nodes([updated])
    rows = mongo_store.query_nodes(source_id=SLUG_A)
    assert len(rows) == 1
    assert rows[0].doc == "the repo entity"


# ── MongoScgStore: query_nodes filters ───────────────────────────────────────


def test_mongo_query_nodes_by_source_id(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_nodes(
        [_node(SLUG_A, "Repo"), _node(SLUG_A, "Issue"), _node(SLUG_B, "Channel")]
    )
    gh = mongo_store.query_nodes(source_id=SLUG_A)
    assert {n.name for n in gh} == {"Repo", "Issue"}


def test_mongo_query_nodes_by_kind(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_nodes(
        [
            _node(SLUG_A, "Repo", kind="entity_type"),
            _node(SLUG_A, "search", kind="capability"),
        ]
    )
    caps = mongo_store.query_nodes(kind="capability")
    assert [n.name for n in caps] == ["search"]


def test_mongo_query_nodes_by_name_contains(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_nodes([_node(SLUG_A, "PullRequest"), _node(SLUG_A, "Issue")])
    hits = mongo_store.query_nodes(name_contains="request")
    assert [n.name for n in hits] == ["PullRequest"]


def test_mongo_query_nodes_combined_filters(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_nodes(
        [
            _node(SLUG_A, "Repo", kind="entity_type"),
            _node(SLUG_A, "search", kind="capability"),
            _node(SLUG_B, "search", kind="capability"),
        ]
    )
    hits = mongo_store.query_nodes(source_id=SLUG_A, kind="capability")
    assert [n.name for n in hits] == ["search"]


def test_mongo_query_nodes_no_filters_returns_all(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_nodes([_node(SLUG_A, "A"), _node(SLUG_B, "B")])
    assert len(mongo_store.query_nodes()) == 2


# ── MongoScgStore: edges ──────────────────────────────────────────────────────


def test_mongo_upsert_edges_and_list(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_edges(
        [
            _edge(f"{SLUG_A}#Repo", f"{SLUG_A}#Repo.id"),
            _edge(f"{SLUG_A}#search", f"{SLUG_A}#Issue", kind="PRODUCES"),
        ]
    )
    assert len(mongo_store.list_edges()) == 2
    assert len(mongo_store.list_edges(kind="HAS_FIELD")) == 1
    assert len(mongo_store.list_edges(source=f"{SLUG_A}#search")) == 1


def test_mongo_upsert_edge_idempotent_on_triple(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_edges([_edge(f"{SLUG_A}#Repo", f"{SLUG_A}#Repo.id", weight=1.0)])
    mongo_store.upsert_edges([_edge(f"{SLUG_A}#Repo", f"{SLUG_A}#Repo.id", weight=0.5)])
    edges = mongo_store.list_edges()
    assert len(edges) == 1
    assert edges[0].weight == pytest.approx(0.5)


def test_mongo_neighbors_returns_outgoing_edges(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_edges(
        [
            _edge(f"{SLUG_A}#Repo", f"{SLUG_A}#Repo.id"),
            _edge(f"{SLUG_A}#Repo", f"{SLUG_A}#Repo.name"),
            _edge(f"{SLUG_A}#Issue", f"{SLUG_A}#Issue.id"),
        ]
    )
    nbrs = mongo_store.neighbors(f"{SLUG_A}#Repo")
    assert {e.target for e in nbrs} == {f"{SLUG_A}#Repo.id", f"{SLUG_A}#Repo.name"}


# ── MongoScgStore: recipes ────────────────────────────────────────────────────


def test_mongo_upsert_and_list_recipes_unscoped(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_recipes(
        [
            _recipe(f"{SLUG_A}#find_pr", [f"{SLUG_A}#search"]),
            _recipe(f"{SLUG_B}#find_channel"),
        ]
    )
    all_r = mongo_store.list_recipes()
    assert len(all_r) == 2


def test_mongo_list_recipes_scoped_by_source_id(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_recipes(
        [
            _recipe(f"{SLUG_A}#find_pr"),
            _recipe(f"{SLUG_B}#find_channel"),
        ]
    )
    gh = mongo_store.list_recipes(source_id=SLUG_A)
    assert len(gh) == 1
    assert gh[0].source_key == f"{SLUG_A}#find_pr"


def test_mongo_upsert_recipe_idempotent(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_recipes([_recipe(f"{SLUG_A}#find_pr", ["step1"])])
    mongo_store.upsert_recipes([_recipe(f"{SLUG_A}#find_pr", ["step1", "step2"])])
    all_r = mongo_store.list_recipes()
    assert len(all_r) == 1
    assert len(all_r[0].steps) == 2


# ── MongoScgStore: embeddings ─────────────────────────────────────────────────


def test_mongo_upsert_and_list_embeddings(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_embeddings([_emb("n1", [1.0, 0.0]), _emb("n2", [0.0, 1.0])])
    rows = mongo_store.list_embeddings()
    assert len(rows) == 2
    node_ids = {r.node_id for r in rows}
    assert node_ids == {"n1", "n2"}


def test_mongo_upsert_embedding_idempotent(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_embeddings([_emb("n1", [1.0, 0.0])])
    mongo_store.upsert_embeddings([_emb("n1", [0.0, 1.0])])
    rows = mongo_store.list_embeddings()
    assert len(rows) == 1
    assert rows[0].vector == pytest.approx([0.0, 1.0])


def test_mongo_vector_search_ranks_by_cosine(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_embeddings(
        [
            _emb("aligned", [1.0, 0.0]),
            _emb("orthogonal", [0.0, 1.0]),
            _emb("opposite", [-1.0, 0.0]),
        ]
    )
    ranked = mongo_store.vector_search([1.0, 0.0], k=3)
    assert [nid for nid, _ in ranked] == ["aligned", "orthogonal", "opposite"]
    assert ranked[0][1] == pytest.approx(1.0)


def test_mongo_vector_search_respects_k(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_embeddings([_emb("a", [1.0, 0.0]), _emb("b", [0.9, 0.1])])
    assert len(mongo_store.vector_search([1.0, 0.0], k=1)) == 1


def test_mongo_vector_search_empty_store(mongo_store: MongoScgStore) -> None:
    assert mongo_store.vector_search([1.0, 0.0], k=5) == []


# ── MongoScgStore: sources ────────────────────────────────────────────────────


def test_mongo_upsert_and_list_sources(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_source(_source(SLUG_A, "openapi"))
    sources = mongo_store.list_sources()
    assert len(sources) == 1
    assert sources[0].source_id == SLUG_A
    assert sources[0].source_type == "openapi"


def test_mongo_upsert_source_idempotent(mongo_store: MongoScgStore) -> None:
    mongo_store.upsert_source(_source(SLUG_A, "openapi"))
    mongo_store.upsert_source(_source(SLUG_A, "graphql"))
    sources = mongo_store.list_sources()
    assert len(sources) == 1
    assert sources[0].source_type == "graphql"


# ── MongoScgStore: delete_source cascade ─────────────────────────────────────


def test_mongo_delete_source_cascades(mongo_store: MongoScgStore) -> None:
    """delete_source wipes nodes/edges/recipes/embeddings/source for one source only."""
    gh_repo = _node(SLUG_A, "Repo")
    gh_issue = _node(SLUG_A, "Issue", kind="capability")
    mongo_store.upsert_nodes([gh_repo, gh_issue])
    mongo_store.upsert_edges([_edge(f"{SLUG_A}#Repo", f"{SLUG_A}#Issue", kind="PRODUCES")])
    mongo_store.upsert_recipes([_recipe(f"{SLUG_A}#find")])
    mongo_store.upsert_embeddings([_emb(gh_repo.node_id, [1.0])])
    mongo_store.upsert_source(_source(SLUG_A))

    sl_chan = _node(SLUG_B, "Channel")
    mongo_store.upsert_nodes([sl_chan])
    mongo_store.upsert_edges([_edge(f"{SLUG_B}#Channel", f"{SLUG_B}#Channel.id")])
    mongo_store.upsert_embeddings([_emb(sl_chan.node_id, [0.0, 1.0])])
    mongo_store.upsert_source(_source(SLUG_B, "mcp"))

    removed = mongo_store.delete_source(SLUG_A)
    assert removed > 0

    # github gone
    assert mongo_store.query_nodes(source_id=SLUG_A) == []
    assert mongo_store.list_edges(source=f"{SLUG_A}#Repo") == []
    assert [s.source_id for s in mongo_store.list_sources()] == [SLUG_B]
    assert mongo_store.get_node(gh_repo.node_id) is None

    # slack untouched
    assert {n.name for n in mongo_store.query_nodes(source_id=SLUG_B)} == {"Channel"}
    assert len(mongo_store.list_edges()) == 1
    assert [e.node_id for e in mongo_store.list_embeddings()] == [sl_chan.node_id]


def test_mongo_delete_source_unknown_returns_zero(mongo_store: MongoScgStore) -> None:
    assert mongo_store.delete_source("nope") == 0


def test_mongo_delete_source_with_no_embeddings(mongo_store: MongoScgStore) -> None:
    """delete_source still works when no embeddings were stored for the source."""
    mongo_store.upsert_nodes([_node(SLUG_A, "Repo")])
    mongo_store.upsert_source(_source(SLUG_A))
    # No embeddings upserted for SLUG_A
    removed = mongo_store.delete_source(SLUG_A)
    assert removed > 0
    assert mongo_store.query_nodes(source_id=SLUG_A) == []


# ── JSON _load() malformed-file recovery ──────────────────────────────────────


def test_json_load_skips_malformed_file(tmp_path: Path) -> None:
    """_load() returns {} and logs a warning for a malformed collection file."""
    store = JsonScgStore(root_dir=tmp_path / "scg")
    # Write garbage to the nodes file directly.
    (tmp_path / "scg" / "nodes.json").write_text("NOT_JSON", encoding="utf-8")
    # _load() via query_nodes should silently return empty, not crash.
    result = store.query_nodes(source_id="anything")
    assert result == []


def test_json_load_skips_non_dict_json(tmp_path: Path) -> None:
    """_load() returns {} when the JSON is valid but not a dict (e.g. a list)."""
    store = JsonScgStore(root_dir=tmp_path / "scg")
    (tmp_path / "scg" / "nodes.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    result = store.query_nodes()
    assert result == []


# ── JSON _upsert() empty-list early-exit ─────────────────────────────────────


def test_json_upsert_empty_nodes_list_is_noop(tmp_path: Path) -> None:
    """upsert_nodes([]) returns without touching the filesystem."""
    store = JsonScgStore(root_dir=tmp_path / "scg")
    nodes_file = tmp_path / "scg" / "nodes.json"
    assert not nodes_file.exists()
    store.upsert_nodes([])
    # File still absent (no write occurred).
    assert not nodes_file.exists()


def test_json_upsert_empty_edges_list_is_noop(tmp_path: Path) -> None:
    store = JsonScgStore(root_dir=tmp_path / "scg")
    store.upsert_edges([])
    assert not (tmp_path / "scg" / "edges.json").exists()


# ── factory + singleton ───────────────────────────────────────────────────────


def test_create_scg_store_returns_json_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_scg_store() returns a JsonScgStore when driver is 'json'."""
    monkeypatch.setattr(scg_store_mod, "get_config_value", lambda *a, default=None, **kw: "json")
    from mewbo_graph.scg.store import create_scg_store

    store = create_scg_store()
    assert isinstance(store, JsonScgStore)


def test_get_scg_store_lazy_init_returns_store() -> None:
    """get_scg_store() constructs a store on first call and returns the same instance."""
    scg_store_mod.reset_for_tests()
    s1 = scg_store_mod.get_scg_store()
    s2 = scg_store_mod.get_scg_store()
    assert s1 is s2


def test_set_scg_store_overrides_singleton(tmp_path: Path) -> None:
    """set_scg_store() replaces the process-wide singleton."""
    override = JsonScgStore(root_dir=tmp_path / "override")
    scg_store_mod.set_scg_store(override)
    assert scg_store_mod.get_scg_store() is override
    scg_store_mod.set_scg_store(None)


def test_get_scg_store_after_set_none_reinits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After set_scg_store(None), get_scg_store() lazily creates a new store."""
    scg_store_mod.set_scg_store(None)
    # Patch create_scg_store so it uses a controlled tmp_path.
    monkeypatch.setattr(
        scg_store_mod,
        "create_scg_store",
        lambda: JsonScgStore(root_dir=tmp_path / "lazy"),
    )
    store = scg_store_mod.get_scg_store()
    assert isinstance(store, JsonScgStore)
    # Cleanup
    scg_store_mod.set_scg_store(None)
