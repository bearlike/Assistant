"""Entity store surface — JSON and Mongo in lockstep.

Mirrors the memory-node persistence idiom (``test_store_memory.py``): one
parameterized fixture exercises BOTH backends so they behave identically.
Mongo path uses mongomock — no real MongoDB required. Covers idempotent
upsert (deterministic id), facet query, cosine vector search, edge dedup, and
the recommendation round-trip.
"""
from __future__ import annotations

import math

import mongomock
import pytest
from mewbo_graph.entities.types import (
    Entity,
    EntityEmbedding,
    EntityFilter,
    EntityRecommendation,
    EntityRelation,
)

SLUG = "org/repo"


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path):
    if request.param == "json":
        from mewbo_graph.wiki.store import JsonWikiStore

        return JsonWikiStore(root_dir=tmp_path / "wiki")
    from mewbo_graph.wiki.store import MongoWikiStore

    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki_ent")


def _emb(entity_id: str, vector: list[float]) -> EntityEmbedding:
    return EntityEmbedding(
        slug=SLUG, entity_id=entity_id, vector=vector, model="m", dim=len(vector)
    )


# ── entities: idempotent upsert + get ────────────────────────────────────────


def test_upsert_and_get_entity_idempotent(store) -> None:
    assert store.get_entity(SLUG, "missing") is None

    e = Entity(name="Ada Lovelace", type="person")
    store.upsert_entities(SLUG, [e])
    # Same deterministic id (surface variant) → converges, no duplicate.
    store.upsert_entities(SLUG, [Entity(name="ada  lovelace", type="person")])

    got = store.get_entity(SLUG, e.id)
    assert got is not None and got.id == e.id
    assert got.name in {"Ada Lovelace", "ada  lovelace"}
    assert len(store.query_entities(SLUG)) == 1


def test_upsert_entities_batch_dedups_by_id(store) -> None:
    a = Entity(name="A", type="person")
    b = Entity(name="B", type="project")
    store.upsert_entities(SLUG, [a, b])
    store.upsert_entities(SLUG, [a, b])  # re-write the same batch
    assert len(store.query_entities(SLUG)) == 2


# ── entities: facet query ────────────────────────────────────────────────────


def test_query_entities_filters_by_type(store) -> None:
    store.upsert_entities(
        SLUG, [Entity(name="A", type="person"), Entity(name="B", type="project")]
    )
    people = store.query_entities(SLUG, filt=EntityFilter(type="person"))
    assert [e.name for e in people] == ["A"]


def test_query_entities_filters_by_status_and_labels(store) -> None:
    store.upsert_entities(
        SLUG,
        [
            Entity(name="A", type="person", status="needs_review", labels=["x"]),
            Entity(name="B", type="person", status="active", labels=["x", "y"]),
        ],
    )
    review = store.query_entities(SLUG, filt=EntityFilter(status="needs_review"))
    assert [e.name for e in review] == ["A"]
    labelled = store.query_entities(SLUG, filt=EntityFilter(labels=["y"]))
    assert [e.name for e in labelled] == ["B"]


def test_query_entities_no_filter_returns_all(store) -> None:
    store.upsert_entities(SLUG, [Entity(name="A", type="person")])
    assert len(store.query_entities(SLUG)) == 1


# ── entity embeddings: idempotent upsert + cosine vector search ──────────────


def test_entity_vector_search_ranks_by_cosine(store) -> None:
    e1 = Entity(name="A", type="person")
    e2 = Entity(name="B", type="person")
    store.upsert_entities(SLUG, [e1, e2])
    store.upsert_entity_embeddings(
        SLUG,
        [_emb(e1.id, [1.0, 0.0]), _emb(e2.id, [0.0, 1.0])],
    )
    hits = store.entity_vector_search(SLUG, [1.0, 0.0], k=1)
    assert len(hits) == 1 and hits[0].entity_id == e1.id

    # 45-degree query sits between the two unit vectors → both returned, sorted.
    both = store.entity_vector_search(SLUG, [1.0, 1.0], k=2)
    assert {h.entity_id for h in both} == {e1.id, e2.id}
    assert math.isclose(
        sum(both[0].vector), sum(both[1].vector)
    )  # both unit vectors


def test_entity_embeddings_upsert_dedups_by_entity_id(store) -> None:
    e1 = Entity(name="A", type="person")
    store.upsert_entities(SLUG, [e1])
    store.upsert_entity_embeddings(SLUG, [_emb(e1.id, [1.0, 0.0])])
    store.upsert_entity_embeddings(SLUG, [_emb(e1.id, [0.0, 1.0])])  # overwrite
    hits = store.entity_vector_search(SLUG, [0.0, 1.0], k=5)
    assert len(hits) == 1 and hits[0].vector == [0.0, 1.0]


def test_entity_vector_search_empty_pool(store) -> None:
    assert store.entity_vector_search(SLUG, [1.0, 0.0], k=5) == []


# ── entity edges: idempotent upsert + scoped list ────────────────────────────


def test_entity_edges_upsert_and_list_dedups(store) -> None:
    rel = EntityRelation(source_id="a", target_id="b", type="works_on")
    store.upsert_entity_edges(SLUG, [rel])
    # Same (source, type, target) → same deterministic id → no duplicate.
    store.upsert_entity_edges(
        SLUG, [EntityRelation(source_id="a", target_id="b", type="works_on")]
    )
    edges = store.list_entity_edges(SLUG)
    assert len(edges) == 1 and edges[0].type == "works_on"


def test_list_entity_edges_scoped_by_source(store) -> None:
    store.upsert_entity_edges(
        SLUG,
        [
            EntityRelation(source_id="a", target_id="b", type="owns"),
            EntityRelation(source_id="c", target_id="b", type="owns"),
        ],
    )
    scoped = store.list_entity_edges(SLUG, source_id="a")
    assert [e.source_id for e in scoped] == ["a"]
    assert len(store.list_entity_edges(SLUG)) == 2


# ── recommendations: round-trip ──────────────────────────────────────────────


def test_recommendations_persist_and_read_back(store) -> None:
    assert store.get_entity_recommendations(SLUG) == []
    rec = EntityRecommendation(
        action="merge",
        subjects=["ada|person", "a lovelace|person"],
        type=None,
        rationale="same person",
    )
    store.save_entity_recommendation(SLUG, rec)
    recs = store.get_entity_recommendations(SLUG)
    assert len(recs) == 1 and recs[0].action == "merge"
    assert recs[0].subjects == ["ada|person", "a lovelace|person"]


def test_recommendations_append_not_overwrite(store) -> None:
    store.save_entity_recommendation(
        SLUG, EntityRecommendation(action="merge", subjects=["a", "b"])
    )
    store.save_entity_recommendation(
        SLUG, EntityRecommendation(action="distinct", subjects=["c", "d"])
    )
    recs = store.get_entity_recommendations(SLUG)
    assert {r.action for r in recs} == {"merge", "distinct"}
    assert len(recs) == 2


# ── slug isolation (shared multiplex, scoped per project) ────────────────────


def test_entities_are_slug_scoped(store) -> None:
    other = "org/other"
    store.upsert_entities(SLUG, [Entity(name="A", type="person")])
    store.upsert_entities(other, [Entity(name="B", type="person")])
    assert [e.name for e in store.query_entities(SLUG)] == ["A"]
    assert [e.name for e in store.query_entities(other)] == ["B"]
