"""ResolutionLadder + EntityResolver — the ONE generic two-threshold ladder.

``rapidfuzz`` ships with ``mewbo-graph[retrieval]`` (installed in the dev
workspace), so these tests exercise the REAL fuzzy backend: near-name pairs
score in (0,1) and can fire a merge/flag on lexical similarity alone. The
import-guard fallback (exact 1.0/0.0) is still in place for lean installs —
``fuzzy_ratio`` stays bounded on either backend. The ladder itself is
pure/strategy-injected, so the SAME class can later back insight dedup — none
of these tests couple it to entities.
"""
from __future__ import annotations

from mewbo_graph.entities import resolver as resolver_mod
from mewbo_graph.entities.resolver import (
    EntityResolver,
    LadderDecision,
    ResolutionLadder,
    fuzzy_ratio,
)
from mewbo_graph.entities.types import Entity, EntityEmbedding, EntityRecommendation

SLUG = "org/repo"


# ── fuzzy_ratio import-guard (real rapidfuzz backend in the dev workspace) ───


def test_fuzzy_ratio_is_bounded_unit_interval():
    score = fuzzy_ratio("ada lovelace", "ada  lovelace")
    assert 0.0 <= score <= 1.0


def test_fuzzy_ratio_identical_is_one_on_either_backend():
    assert fuzzy_ratio("acme inc", "acme inc") == 1.0


def test_fuzzy_ratio_uses_real_rapidfuzz_for_near_names():
    # With rapidfuzz installed (the retrieval extra) a near-name pair scores
    # in (0, 1) — NOT the exact 0.0/1.0 the lean-install fallback would give.
    assert resolver_mod._HAS_RAPIDFUZZ is True
    score = fuzzy_ratio("acme inc", "acme incorporated")
    assert 0.0 < score < 1.0
    # The guard still degrades gracefully when the dep is absent (lean install).
    assert resolver_mod.fuzzy_ratio.__doc__ is not None


# ── ResolutionLadder decision matrix (pure, strategy-injected) ───────────────


def _ladder(*, recommendations=None, auto=0.9, flag=0.6):
    candidates = {"new": [("c1", "Cand One"), ("c2", "Cand Two")]}
    scores = {("new", "c1"): 0.95, ("new", "c2"): 0.5}

    def block(key):
        return candidates.get(key, [])

    def score(key, cand_id):
        return scores[(key, cand_id)]

    def identity(cand_id):
        return cand_id

    return ResolutionLadder(
        block=block,
        score=score,
        identity=identity,
        auto_merge=auto,
        flag=flag,
        recommendations=recommendations or {},
    )


def test_decides_merge_above_auto_threshold():
    d = _ladder().decide("new")
    assert d == LadderDecision(action="merge", target_id="c1", score=0.95)


def test_decides_flag_in_band():
    d = _ladder(auto=0.99, flag=0.6).decide("new")
    assert d.action == "flag" and d.target_id == "c1"


def test_decides_new_below_flag():
    d = _ladder(auto=0.99, flag=0.97).decide("new")
    assert d.action == "new" and d.target_id is None


def test_decides_new_when_no_candidates():
    d = _ladder().decide("absent-key")
    assert d.action == "new" and d.target_id is None


def test_merge_recommendation_forces_pair_even_below_flag():
    # c2 scores 0.5 (< flag 0.6) but a merge prior for that pair forces merge.
    recs = {frozenset({"new", "c2"}): "merge"}
    d = _ladder(recommendations=recs).decide("new")
    # The merge prior short-circuits on the first matching candidate.
    assert d.action == "merge" and d.target_id == "c2"
    assert d.score >= 0.9  # prior lifts the score to >= auto_merge


def test_distinct_recommendation_forces_separation():
    # Even though c1 scores 0.95 >= auto, a distinct prior forbids the pair.
    recs = {frozenset({"new", "c1"}): "distinct"}
    d = _ladder(recommendations=recs).decide("new")
    assert d.target_id != "c1"  # c1 excluded; falls through to c2 band/new


# ── EntityResolver: ladder over entities (ANN block + hybrid score) ──────────


class FakeStore:
    """Minimal in-memory store exposing the entity surface EntityResolver needs.

    The real ``WikiStoreBase`` methods land in the separate integration pass;
    this fake pins the exact contract the resolver assumes.
    """

    def __init__(self):
        self._entities: dict[str, dict[str, Entity]] = {}
        self._embeddings: dict[str, dict[str, EntityEmbedding]] = {}
        self._recs: dict[str, list[EntityRecommendation]] = {}

    def upsert_entities(self, slug, entities):
        bucket = self._entities.setdefault(slug, {})
        for e in entities:
            bucket[e.id] = e

    def upsert_entity_embeddings(self, slug, items):
        bucket = self._embeddings.setdefault(slug, {})
        for it in items:
            bucket[it.entity_id] = it

    def query_entities(self, slug, *, filt=None):
        out = list(self._entities.get(slug, {}).values())
        return out if filt is None else [e for e in out if filt.matches(e)]

    def get_entity(self, slug, entity_id):
        return self._entities.get(slug, {}).get(entity_id)

    def entity_vector_search(self, slug, qvec, k=10):
        from mewbo_graph.wiki.embedder import Embedder

        pool = list(self._embeddings.get(slug, {}).values())
        scored = [(emb, Embedder.cosine(qvec, emb.vector)) for emb in pool]
        scored.sort(key=lambda t: t[1], reverse=True)
        return [emb for emb, _ in scored[:k]]

    def save_entity_recommendation(self, slug, rec):
        self._recs.setdefault(slug, []).append(rec)

    def get_entity_recommendations(self, slug):
        return list(self._recs.get(slug, []))


class FakeEmbedder:
    model = "fake"

    def __init__(self, vectors):
        self._v = vectors

    def embed_query(self, text):
        return list(self._v.get(text, [0.0, 0.0]))

    def embed_nodes(self, items, *, slug=""):
        return [
            EntityEmbedding(
                slug=slug,
                entity_id=nid,
                vector=self._v.get(t, [0.0, 0.0]),
                model="fake",
                dim=2,
            )
            for nid, t in items
        ]


def test_resolver_merges_on_high_cosine():
    s = FakeStore()
    ada = Entity(name="Ada Lovelace", type="person")
    s.upsert_entities(SLUG, [ada])
    s.upsert_entity_embeddings(
        SLUG, [EntityEmbedding(slug=SLUG, entity_id=ada.id, vector=[1.0, 0.0], model="f", dim=2)]
    )
    emb = FakeEmbedder({"Augusta Ada King": [1.0, 0.0]})  # identical vector
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, Entity(name="Augusta Ada King", type="person"))
    assert decision.action == "merge" and decision.target_id == ada.id


def test_resolver_distinct_names_low_fuzzy_low_cosine_is_new():
    # Genuinely distinct names score ~0 on rapidfuzz, so with orthogonal
    # vectors the decision is driven purely by cosine → NEW (no false merge).
    s = FakeStore()
    globex = Entity(name="Globex Corporation", type="organization")
    s.upsert_entities(SLUG, [globex])
    s.upsert_entity_embeddings(
        SLUG, [EntityEmbedding(slug=SLUG, entity_id=globex.id, vector=[0.0, 1.0], model="f", dim=2)]
    )
    emb = FakeEmbedder({"Initech": [1.0, 0.0]})  # orthogonal → cosine 0
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, Entity(name="Initech", type="organization"))
    # cosine 0, fuzzy ~0.16 (distinct names) → both below flag → NEW.
    assert decision.action == "new" and decision.target_id is None


def test_resolver_merges_near_name_on_real_fuzzy():
    # "JPMorgan" ~ "JP Morgan" → rapidfuzz ratio ~0.94 (>= auto_merge), so the
    # ladder merges on lexical similarity ALONE, even with an orthogonal vector.
    s = FakeStore()
    jpm = Entity(name="JP Morgan", type="organization")
    s.upsert_entities(SLUG, [jpm])
    s.upsert_entity_embeddings(
        SLUG, [EntityEmbedding(slug=SLUG, entity_id=jpm.id, vector=[0.0, 1.0], model="f", dim=2)]
    )
    emb = FakeEmbedder({"JPMorgan": [1.0, 0.0]})  # orthogonal → cosine 0; fuzzy wins
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, Entity(name="JPMorgan", type="organization"))
    assert decision.action == "merge" and decision.target_id == jpm.id


def test_resolver_cosine_lands_candidate_in_flag_band():
    # A moderate cosine (in [flag, auto)) flags rather than merges — pure-cosine path.
    import math

    s = FakeStore()
    acme = Entity(name="Acme Incorporated", type="organization")
    s.upsert_entities(SLUG, [acme])
    # 45-degree vector → cosine ~0.707 against the query, which sits in [0.6, 0.9).
    s.upsert_entity_embeddings(
        SLUG,
        [
            EntityEmbedding(
                slug=SLUG,
                entity_id=acme.id,
                vector=[1.0 / math.sqrt(2), 1.0 / math.sqrt(2)],
                model="f",
                dim=2,
            )
        ],
    )
    emb = FakeEmbedder({"Acme Inc": [1.0, 0.0]})
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, Entity(name="Acme Inc", type="organization"))
    assert decision.action == "flag" and decision.target_id == acme.id


def test_resolver_new_when_no_candidate():
    s = FakeStore()
    emb = FakeEmbedder({})
    r = EntityResolver(store=s, embedder=emb)
    decision = r.resolve(SLUG, Entity(name="Nobody", type="person"))
    assert decision.action == "new" and decision.target_id is None


def test_resolver_distinct_prior_blocks_a_merge():
    s = FakeStore()
    ada = Entity(name="Ada Lovelace", type="person")
    other = Entity(name="Augusta Ada King", type="person")
    s.upsert_entities(SLUG, [ada])
    s.upsert_entity_embeddings(
        SLUG, [EntityEmbedding(slug=SLUG, entity_id=ada.id, vector=[1.0, 0.0], model="f", dim=2)]
    )
    s.save_entity_recommendation(
        SLUG,
        EntityRecommendation(action="distinct", subjects=[other.id, ada.id], rationale="not same"),
    )
    emb = FakeEmbedder({"Augusta Ada King": [1.0, 0.0]})  # would otherwise merge
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, other)
    assert decision.target_id != ada.id  # distinct prior forbids the pair


# ── Recommendations carrying "<name>|<type>" subjects (page-writer format) ───
#
# The page-writer surfaces a prose-only entity as
# ``subjects=["<name>|<type>"]`` (see wiki-page-writer.md), NOT as raw entity
# ids. The resolver must normalize each subject to its deterministic id before
# folding it into the ladder priors, or merge/distinct biases never match.


def test_merge_prior_with_name_type_subjects_biases_distinct_entities():
    # Two genuinely distinct names (low cosine + low fuzzy → would resolve NEW),
    # but a ``merge`` recommendation phrased in "<name>|<type>" subjects must
    # bias them into a merge.
    s = FakeStore()
    initech = Entity(name="Initech", type="organization")
    s.upsert_entities(SLUG, [initech])
    s.upsert_entity_embeddings(
        SLUG,
        [EntityEmbedding(slug=SLUG, entity_id=initech.id, vector=[0.0, 1.0], model="f", dim=2)],
    )
    s.save_entity_recommendation(
        SLUG,
        EntityRecommendation(
            action="merge",
            subjects=["Globex Corporation|organization", "Initech|organization"],
            rationale="same legal entity post-acquisition",
        ),
    )
    emb = FakeEmbedder({"Globex Corporation": [1.0, 0.0]})  # orthogonal → cosine 0
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, Entity(name="Globex Corporation", type="organization"))
    assert decision.action == "merge" and decision.target_id == initech.id


def test_distinct_prior_with_name_type_subjects_forces_separation():
    # Identical vectors would auto-merge, but a ``distinct`` recommendation in
    # "<name>|<type>" subject form must forbid the pair.
    s = FakeStore()
    ada = Entity(name="Ada Lovelace", type="person")
    other = Entity(name="Augusta Ada King", type="person")
    s.upsert_entities(SLUG, [ada])
    s.upsert_entity_embeddings(
        SLUG, [EntityEmbedding(slug=SLUG, entity_id=ada.id, vector=[1.0, 0.0], model="f", dim=2)]
    )
    s.save_entity_recommendation(
        SLUG,
        EntityRecommendation(
            action="distinct",
            subjects=["Augusta Ada King|person", "Ada Lovelace|person"],
            rationale="different people",
        ),
    )
    emb = FakeEmbedder({"Augusta Ada King": [1.0, 0.0]})  # would otherwise merge
    r = EntityResolver(store=s, embedder=emb, auto_merge=0.9, flag=0.6)
    decision = r.resolve(SLUG, other)
    assert decision.target_id != ada.id  # distinct prior forbids the pair
