"""InsightIngestor — the DRY write core behind all three insight surfaces.

Exercises the full ingest path through real store + provider + deduper code;
only the I/O boundaries (embedder, LLM) are stubbed, per the repo's testing
philosophy.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mewbo_graph.wiki.embedder import Embedder
from mewbo_graph.wiki.memory import (
    InsightCondenser,
    InsightDeduper,
    InsightIngestor,
)
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.structure_provider import CodeStructureProvider
from mewbo_graph.wiki.types import Embedding, GraphNode

SLUG = "org/repo"
CLOCK = "2026-06-05T12:00:00Z"


# ── I/O stubs ───────────────────────────────────────────────────────────────


class FakeEmbedder:
    """Deterministic text→vector stub returning code-shaped Embedding rows."""

    def __init__(self, vectors=None, *, fail=False):
        self.vectors = vectors or {}
        self.fail = fail

    def _vec(self, text):
        return list(self.vectors.get(text, [1.0, 0.0]))

    def embed_nodes(self, items, *, slug=""):
        if self.fail:
            raise RuntimeError("no embedding backend")
        out = []
        for nid, t in items:
            v = self._vec(t)
            out.append(Embedding(slug=slug, node_id=nid, vector=v, model="fake", dim=len(v)))
        return out

    def embed_query(self, text):
        if self.fail:
            raise RuntimeError("no embedding backend")
        return self._vec(text)

    @staticmethod
    def cosine(a, b):
        return Embedder.cosine(a, b)


class FakeLLM:
    """LLM stub: returns a fixed string, or raises when ``boom`` is set."""

    def __init__(self, response="new", *, boom=False):
        self.response = response
        self.boom = boom
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        if self.boom:
            raise RuntimeError("llm down")
        return SimpleNamespace(content=self.response)


# ── fixtures / builders ─────────────────────────────────────────────────────


def _gn(nid: str, typ: str, name: str, f: str) -> GraphNode:
    return GraphNode(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


@pytest.fixture
def store(tmp_path):
    s = JsonWikiStore(root_dir=tmp_path / "wiki")
    # Seed code nodes so explicit anchors resolve.
    s.upsert_nodes(
        SLUG,
        [
            _gn("cA", "Class", "AuthService", "auth.py"),
            _gn("fV", "Function", "verify", "auth.py"),
            _gn("fS", "Function", "save", "store.py"),
        ],
    )
    return s


def _ingestor(store, *, embedder=None, llm=None, condenser=None):
    embedder = embedder or FakeEmbedder()
    provider = CodeStructureProvider(store)
    deduper = InsightDeduper(store=store, llm=llm)
    return InsightIngestor(
        store=store,
        embedder=embedder,
        provider=provider,
        deduper=deduper,
        condenser=condenser,
        clock=lambda: CLOCK,
    )


# ── new-note path ───────────────────────────────────────────────────────────


def test_ingest_creates_node_embedding_and_anchor(store) -> None:
    ing = _ingestor(store)
    res = ing.ingest(SLUG, "AuthService verifies bearer tokens", anchors=["auth.py#AuthService"])
    [claim] = res.claims
    assert claim.action == "created"
    node = store.get_memory_node(SLUG, claim.node_id)
    assert node is not None
    # ANCHORS edge present + valid_at stamped from the injected clock
    edges = store.list_memory_edges(SLUG, node_id=claim.node_id)
    anchors = [e for e in edges if e.type == "ANCHORS"]
    assert [e.target for e in anchors] == ["auth.py#AuthService"]
    assert anchors[0].valid_at == CLOCK
    assert node.provenance.created_at == CLOCK
    # embedding stored
    assert store.memory_vector_search(SLUG, [1.0, 0.0], k=1)


def test_ingest_drops_unresolved_anchor_with_warning(store) -> None:
    ing = _ingestor(store)
    res = ing.ingest(SLUG, "claim", anchors=["auth.py#AuthService", "ghost.py#Nope"])
    [claim] = res.claims
    assert claim.action == "created"
    assert claim.anchors == ["auth.py#AuthService"]
    assert any("ghost.py#Nope" in w for w in claim.warnings)


def test_ingest_caps_anchors(store) -> None:
    # 10 anchors, all resolving to the same real node — capped to 8.
    ing = _ingestor(store)
    res = ing.ingest(SLUG, "claim", anchors=["auth.py#AuthService"] * 10)
    [claim] = res.claims
    assert any("capped" in w for w in claim.warnings)


def test_ingest_rejects_overlong_content_without_condense(store) -> None:
    ing = _ingestor(store)
    res = ing.ingest(SLUG, "x" * 250)
    [claim] = res.claims
    assert claim.action == "rejected"
    assert store.query_memory(SLUG) == []


# ── dedup ladder ────────────────────────────────────────────────────────────


def test_exact_dedup_merges_and_unions_anchors(store) -> None:
    ing = _ingestor(store)
    ing.ingest(SLUG, "AuthService verifies tokens", anchors=["auth.py#AuthService"])
    res = ing.ingest(SLUG, "AuthService verifies tokens", anchors=["auth.py#verify"])
    [claim] = res.claims
    assert claim.action == "merged"
    assert claim.tier == "exact"
    assert len(store.query_memory(SLUG)) == 1
    edges = store.list_memory_edges(SLUG, node_id=claim.node_id)
    anchors = {e.target for e in edges if e.type == "ANCHORS"}
    assert anchors == {"auth.py#AuthService", "auth.py#verify"}


def test_fuzzy_dedup_merges(store) -> None:
    ing = _ingestor(store)
    ing.ingest(SLUG, "AuthService verifies bearer tokens", anchors=["auth.py#AuthService"])
    # identical token set, different normalized string (trailing period) → fuzzy
    res = ing.ingest(SLUG, "AuthService verifies bearer tokens.", anchors=["auth.py#verify"])
    [claim] = res.claims
    assert claim.action == "merged"
    assert claim.tier == "fuzzy"
    assert len(store.query_memory(SLUG)) == 1


def test_llm_dedup_links_related_note(store) -> None:
    emb = FakeEmbedder(
        vectors={
            "Sessions expire after one hour": [1.0, 0.0],
            "Token lifetime is sixty minutes": [0.96, 0.05],
        }
    )
    ing = _ingestor(store, embedder=emb, llm=FakeLLM("link"))
    ing.ingest(SLUG, "Sessions expire after one hour", anchors=["auth.py#AuthService"])
    res = ing.ingest(SLUG, "Token lifetime is sixty minutes", anchors=["auth.py#verify"])
    [claim] = res.claims
    assert claim.action == "linked"
    assert len(store.query_memory(SLUG)) == 2
    edges = store.list_memory_edges(SLUG, node_id=claim.node_id)
    relates = [e for e in edges if e.type == "RELATES"]
    assert len(relates) == 1


def test_llm_dedup_defaults_to_new_on_llm_failure(store) -> None:
    emb = FakeEmbedder(
        vectors={
            "Sessions expire after one hour": [1.0, 0.0],
            "Token lifetime is sixty minutes": [0.96, 0.05],
        }
    )
    ing = _ingestor(store, embedder=emb, llm=FakeLLM(boom=True))
    ing.ingest(SLUG, "Sessions expire after one hour", anchors=["auth.py#AuthService"])
    res = ing.ingest(SLUG, "Token lifetime is sixty minutes", anchors=["auth.py#verify"])
    [claim] = res.claims
    assert claim.action == "created"
    assert len(store.query_memory(SLUG)) == 2


# ── non-fatal embedding ─────────────────────────────────────────────────────


def test_embedding_failure_is_nonfatal(store) -> None:
    ing = _ingestor(store, embedder=FakeEmbedder(fail=True))
    res = ing.ingest(SLUG, "claim", anchors=["auth.py#AuthService"])
    [claim] = res.claims
    assert claim.action == "created"
    assert store.get_memory_node(SLUG, claim.node_id) is not None
    assert store.memory_vector_search(SLUG, [1.0, 0.0], k=5) == []
    assert any("BM25" in w or "embedding" in w for w in claim.warnings)


# ── condense + auto-anchor ──────────────────────────────────────────────────


def test_condense_splits_raw_into_atomic_claims(store) -> None:
    two = "- AuthService verifies tokens\n- Tokens expire after one hour"
    condenser = InsightCondenser(FakeLLM(two))
    ing = _ingestor(store, condenser=condenser)
    res = ing.ingest(SLUG, raw="Auth notes blob", anchors=["auth.py#AuthService"], condense=True)
    assert len(res.claims) == 2
    assert all(c.action == "created" for c in res.claims)
    assert len(store.query_memory(SLUG)) == 2


def test_condense_auto_anchors_via_code_embeddings(store) -> None:
    # code embedding for fV (auth.py#verify) at [1,0]; claim embeds to [1,0] too
    code_emb = Embedding(slug=SLUG, node_id="fV", vector=[1.0, 0.0], model="m", dim=2)
    store.upsert_embeddings(SLUG, [code_emb])
    emb = FakeEmbedder(vectors={"AuthService verifies tokens": [1.0, 0.0]})
    condenser = InsightCondenser(FakeLLM("AuthService verifies tokens"))
    ing = _ingestor(store, embedder=emb, condenser=condenser)
    res = ing.ingest(SLUG, raw="blob", condense=True)  # no explicit anchors
    [claim] = res.claims
    assert claim.anchors == ["auth.py#verify"]


def test_condense_falls_back_to_single_claim_without_condenser(store) -> None:
    ing = _ingestor(store)  # no condenser
    res = ing.ingest(SLUG, raw="One short claim", condense=True)
    [claim] = res.claims
    assert claim.action == "created"


# ── shared factory ──────────────────────────────────────────────────────────


def test_from_store_factory(store) -> None:
    # inject embedder to avoid touching litellm; factory wires the rest
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), clock=lambda: CLOCK)
    res = ing.ingest(SLUG, "AuthService verifies tokens", anchors=["auth.py#AuthService"])
    [claim] = res.claims
    assert claim.action == "created"
    assert store.get_memory_node(SLUG, claim.node_id) is not None


def test_null_embedder_yields_bm25_fallback(store) -> None:
    from mewbo_graph.wiki.memory import _NullEmbedder

    ing = InsightIngestor.from_store(store, embedder=_NullEmbedder(), clock=lambda: CLOCK)
    res = ing.ingest(SLUG, "Storage persists pages", anchors=["store.py#save"])
    [claim] = res.claims
    assert claim.action == "created"
    assert store.memory_vector_search(SLUG, [1.0, 0.0], k=5) == []


def test_merge_with_new_identity_retires_old_node(store) -> None:
    # LLM-merge a crisper note onto a longer one → survivor gets a new node_id;
    # the superseded old node must be deleted (not left orphaned in the store).
    emb = FakeEmbedder(
        vectors={
            "Sessions are expired after exactly sixty minutes": [1.0, 0.0],
            "Sessions expire in one hour": [0.97, 0.04],
        }
    )
    ing = _ingestor(store, embedder=emb, llm=FakeLLM("merge"))
    r1 = ing.ingest(
        SLUG, "Sessions are expired after exactly sixty minutes",
        anchors=["auth.py#AuthService"],
    )
    old_id = r1.claims[0].node_id
    res = ing.ingest(SLUG, "Sessions expire in one hour", anchors=["auth.py#verify"])
    [claim] = res.claims
    assert claim.action == "merged"
    assert claim.node_id != old_id  # survivor is the crisper note → new identity
    assert store.get_memory_node(SLUG, old_id) is None  # old node retired
    assert len(store.query_memory(SLUG)) == 1
    # union of anchors carried onto the survivor
    edges = store.list_memory_edges(SLUG, node_id=claim.node_id)
    anchors = {e.target for e in edges if e.type == "ANCHORS"}
    assert anchors == {"auth.py#AuthService", "auth.py#verify"}


# ── deduper delegates to the ONE shared ResolutionLadder (DRY) ───────────────


def test_deduper_classify_delegates_to_resolution_ladder(store) -> None:
    """The fuzzy/LLM tiers now route through the generic ResolutionLadder.

    Behavior is pinned by the regression suite above; this characterizes the
    structural seam (one ladder, two callers) so the delegation can't silently
    regress to a hand-rolled second ladder.
    """
    from mewbo_graph.entities.resolver import ResolutionLadder
    from mewbo_graph.wiki.memory import InsightDeduper
    from mewbo_graph.wiki.memory_types import MemoryNode, MemoryProvenance

    deduper = InsightDeduper(store=store)
    candidate = MemoryNode(
        slug=SLUG,
        content="AuthService verifies tokens",
        provenance=MemoryProvenance(
            author_agent="t", source="indexer", created_at=CLOCK
        ),
    )
    ladder, by_id = deduper._build_ladder(SLUG, candidate, [1.0, 0.0])
    assert isinstance(ladder, ResolutionLadder)
    assert isinstance(by_id, dict)
