"""EntityMinter — idempotent upsert with per-mention provenance + lazy merge.

Uses an in-memory FAKE store (the real ``WikiStoreBase`` entity methods land in
the separate integration pass) that pins the exact store contract the minter
assumes — so this domain core is unit-testable with zero infrastructure.
"""
from __future__ import annotations

from mewbo_graph.entities.minter import EntityMinter
from mewbo_graph.entities.resolver import EntityResolver
from mewbo_graph.entities.types import Entity, EntityEmbedding

SLUG = "org/repo"
CLOCK = "2026-06-07T00:00:00Z"


class FakeStore:
    """In-memory store exposing the entity surface the minter + resolver use."""

    def __init__(self):
        self._entities: dict[str, dict[str, Entity]] = {}
        self._embeddings: dict[str, dict[str, EntityEmbedding]] = {}
        self._edges: dict[str, dict[str, object]] = {}
        self._recs: dict[str, list[object]] = {}

    def upsert_entities(self, slug, entities):
        bucket = self._entities.setdefault(slug, {})
        for e in entities:
            bucket[e.id] = e

    def get_entity(self, slug, entity_id):
        return self._entities.get(slug, {}).get(entity_id)

    def query_entities(self, slug, *, filt=None):
        out = list(self._entities.get(slug, {}).values())
        return out if filt is None else [e for e in out if filt.matches(e)]

    def upsert_entity_embeddings(self, slug, items):
        bucket = self._embeddings.setdefault(slug, {})
        for it in items:
            bucket[it.entity_id] = it

    def entity_vector_search(self, slug, qvec, k=10):
        from mewbo_graph.wiki.embedder import Embedder

        pool = list(self._embeddings.get(slug, {}).values())
        scored = [(emb, Embedder.cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

    def upsert_entity_edges(self, slug, edges):
        bucket = self._edges.setdefault(slug, {})
        for e in edges:
            bucket[e.id] = e

    def list_entity_edges(self, slug, *, source_id=None):
        out = list(self._edges.get(slug, {}).values())
        if source_id is not None:
            out = [e for e in out if e.source_id == source_id]
        return out

    def get_entity_recommendations(self, slug):
        return list(self._recs.get(slug, []))


class FakeEmbedder:
    model = "fake"

    def embed_query(self, text):
        return [1.0, 0.0]

    def embed_nodes(self, items, *, slug=""):
        return [
            EntityEmbedding(slug=slug, entity_id=nid, vector=[1.0, 0.0], model="fake", dim=2)
            for nid, t in items
        ]


def _minter(store):
    emb = FakeEmbedder()
    resolver = EntityResolver(store=store, embedder=emb)
    return EntityMinter(store=store, embedder=emb, resolver=resolver, clock=lambda: CLOCK)


def test_mint_creates_entity_with_mention():
    s = FakeStore()
    m = _minter(s)
    e = m.upsert(Entity(name="Ada Lovelace", type="person"), source="auth.py", slug=SLUG)
    assert e.status == "active"
    got = s.get_entity(SLUG, e.id)
    assert got is not None
    assert got.mentions and got.mentions[0].source == "auth.py"
    assert got.mentions[0].surface_name == "Ada Lovelace"
    assert got.mentions[0].ts == CLOCK


def test_remint_same_name_is_idempotent_and_adds_mention():
    s = FakeStore()
    m = _minter(s)
    m.upsert(Entity(name="Ada", type="person"), source="a.py", slug=SLUG)
    m.upsert(Entity(name="ada", type="person"), source="b.py", slug=SLUG)
    rows = s.query_entities(SLUG)
    assert len(rows) == 1  # deterministic id ⇒ one node
    assert len(rows[0].mentions) == 2  # both provenance records carried


def test_merge_adds_alias_and_keeps_active():
    s = FakeStore()
    m = _minter(s)
    m.upsert(Entity(name="Ada Lovelace", type="person"), source="a.py", slug=SLUG)
    # Near-identical name resolves to merge (FakeEmbedder returns identical vec).
    m.upsert(Entity(name="Augusta Ada King", type="person"), source="b.py", slug=SLUG)
    rows = s.query_entities(SLUG)
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert "Augusta Ada King" in rows[0].aliases  # surface name carried as alias
    assert len(rows[0].mentions) == 2  # provenance from both surfaces


def test_merge_does_not_duplicate_aliases_on_repeat():
    s = FakeStore()
    m = _minter(s)
    m.upsert(Entity(name="Ada Lovelace", type="person"), source="a.py", slug=SLUG)
    m.upsert(Entity(name="Augusta Ada King", type="person"), source="b.py", slug=SLUG)
    m.upsert(Entity(name="Augusta Ada King", type="person"), source="c.py", slug=SLUG)
    rows = s.query_entities(SLUG)
    assert rows[0].aliases.count("Augusta Ada King") == 1  # de-duplicated


def test_flag_writes_needs_review_and_same_as_edge():
    # A candidate whose cosine lands in [flag, auto) is FLAGGED: a new node with
    # status=needs_review + a SAME_AS edge to the neighbour. The candidate name
    # is a lexically-DISTANT alias ("The Enchantress of Numbers" — Ada's
    # nickname) so rapidfuzz scores it low; cosine ~0.707 is the SOLE driver of
    # the flag-band decision (a near-name would auto-merge on real fuzzy alone).
    import math

    s = FakeStore()
    ada = Entity(name="Ada Lovelace", type="person")
    s.upsert_entities(SLUG, [ada])
    s.upsert_entity_embeddings(
        SLUG,
        [
            EntityEmbedding(
                slug=SLUG,
                entity_id=ada.id,
                vector=[1.0 / math.sqrt(2), 1.0 / math.sqrt(2)],  # cosine ~0.707
                model="f",
                dim=2,
            )
        ],
    )
    emb = FakeEmbedder()  # embed_query → [1.0, 0.0]
    resolver = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    minter = EntityMinter(store=s, embedder=emb, resolver=resolver, clock=lambda: CLOCK)
    flagged = minter.upsert(
        Entity(name="The Enchantress of Numbers", type="person"), source="b.py", slug=SLUG
    )
    assert flagged.status == "needs_review"
    got = s.get_entity(SLUG, flagged.id)
    assert got is not None and got.status == "needs_review"
    edges = s.list_entity_edges(SLUG, source_id=flagged.id)
    assert edges and edges[0].type == "SAME_AS" and edges[0].target_id == ada.id


def test_mint_writes_embedding():
    s = FakeStore()
    m = _minter(s)
    e = m.upsert(Entity(name="Ada Lovelace", type="person"), source="a.py", slug=SLUG)
    hits = s.entity_vector_search(SLUG, [1.0, 0.0], k=5)
    assert any(h.entity_id == e.id for h in hits)
