"""MemoryReconciler — drift ladder over the affected-entity set."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mewbo_graph.wiki.memory_types import (
    MemoryEdge,
    MemoryNode,
    MemoryProvenance,
)
from mewbo_graph.wiki.refresh import GraphDelta, MemoryReconciler
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphNode

SLUG = "org/repo"
T0 = "2026-06-05T00:00:00Z"
REFRESH_AT = "2026-06-05T12:00:00Z"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


class FakeEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed_query(self, text):
        return list(self.vectors.get(text, [1.0, 0.0]))


class FakeLLM:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def invoke(self, prompt):  # noqa: ARG002
        self.calls += 1
        return SimpleNamespace(content=self.verdict)


def _prov():
    return MemoryProvenance(author_agent="a", source="indexer", created_at=T0)


def _mem(content, **kw):
    return MemoryNode(slug=SLUG, content=content, provenance=_prov(), **kw)


def _anchor(node_id, key):
    return MemoryEdge(slug=SLUG, source=node_id, target=key, type="ANCHORS", valid_at=T0)


def _code(nid, name, doc=""):
    return GraphNode(
        slug=SLUG, node_id=nid, type="Function", name=name,
        file="auth.py", range=(0, 9), docstring=doc,
    )


def _reconcile_removed(store, **kw):
    rec = _reconciler(store, **kw)
    return rec.reconcile(SLUG, _delta(removed=["auth.py#verify"]), refresh_started_at=REFRESH_AT)


def _delta(*, added=(), modified=(), removed=()):
    keys = set(added) | set(modified) | set(removed)
    return GraphDelta(
        added_keys=frozenset(added),
        modified_keys=frozenset(modified),
        removed_keys=frozenset(removed),
        affected=frozenset(keys),
        early_cutoff_files=(),
    )


def _reconciler(store, **kw):
    return MemoryReconciler(store=store, **kw)


# ── removed entity ──────────────────────────────────────────────────────────


def test_removed_entity_invalidates_orphan_memory(store) -> None:
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    res = _reconcile_removed(store)
    assert m.node_id in res.invalidated
    # no live anchors remain
    assert store.list_memory_edges(SLUG, node_id=m.node_id) == []


def test_removed_entity_keeps_memory_with_other_live_anchor(store) -> None:
    m = _mem("claim spanning two files")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(
        SLUG, [_anchor(m.node_id, "auth.py#verify"), _anchor(m.node_id, "store.py#save")]
    )
    res = _reconcile_removed(store)
    assert m.node_id in res.kept
    live = {e.target for e in store.list_memory_edges(SLUG, node_id=m.node_id)}
    assert live == {"store.py#save"}


# ── modified entity drift ladder ────────────────────────────────────────────


def test_modified_high_similarity_keeps(store) -> None:
    store.upsert_nodes(SLUG, [_code("nV", "verify", "checks token")])
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    emb = FakeEmbedder({"verify checks the token": [1.0, 0.0], "verify checks token": [1.0, 0.0]})
    res = _reconciler(store, embedder=emb).reconcile(
        SLUG, _delta(modified=["auth.py#verify"]), refresh_started_at=REFRESH_AT
    )
    assert m.node_id in res.kept
    assert len(store.list_memory_edges(SLUG, node_id=m.node_id)) == 1


def test_modified_low_similarity_invalidates(store) -> None:
    store.upsert_nodes(SLUG, [_code("nV", "verify", "now does something unrelated")])
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    emb = FakeEmbedder(
        {"verify checks the token": [1.0, 0.0], "verify now does something unrelated": [0.0, 1.0]}
    )
    res = _reconciler(store, embedder=emb).reconcile(
        SLUG, _delta(modified=["auth.py#verify"]), refresh_started_at=REFRESH_AT
    )
    assert m.node_id in res.invalidated
    assert store.list_memory_edges(SLUG, node_id=m.node_id) == []


def test_modified_band_llm_outdated_invalidates(store) -> None:
    store.upsert_nodes(SLUG, [_code("nV", "verify", "partially changed")])
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    emb = FakeEmbedder(
        {"verify checks the token": [1.0, 0.0], "verify partially changed": [0.8, 0.6]}
    )
    llm = FakeLLM("OUTDATED")
    res = _reconciler(store, embedder=emb, llm=llm).reconcile(
        SLUG, _delta(modified=["auth.py#verify"]), refresh_started_at=REFRESH_AT
    )
    assert llm.calls == 1
    assert m.node_id in res.invalidated
    assert m.node_id in res.revalidated


def test_modified_band_llm_valid_keeps(store) -> None:
    store.upsert_nodes(SLUG, [_code("nV", "verify", "partially changed")])
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    emb = FakeEmbedder(
        {"verify checks the token": [1.0, 0.0], "verify partially changed": [0.8, 0.6]}
    )
    res = _reconciler(store, embedder=emb, llm=FakeLLM("VALID")).reconcile(
        SLUG, _delta(modified=["auth.py#verify"]), refresh_started_at=REFRESH_AT
    )
    assert m.node_id in res.kept
    assert len(store.list_memory_edges(SLUG, node_id=m.node_id)) == 1


def test_band_without_llm_defaults_to_keep(store) -> None:
    store.upsert_nodes(SLUG, [_code("nV", "verify", "partially changed")])
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    emb = FakeEmbedder(
        {"verify checks the token": [1.0, 0.0], "verify partially changed": [0.8, 0.6]}
    )
    res = _reconciler(store, embedder=emb).reconcile(  # no llm → NONE default
        SLUG, _delta(modified=["auth.py#verify"]), refresh_started_at=REFRESH_AT
    )
    assert m.node_id in res.kept


# ── safety + idempotency ────────────────────────────────────────────────────


def test_override_memory_is_immutable(store) -> None:
    m = _mem("curated rule", labels=["override"])
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    res = _reconcile_removed(store)
    assert m.node_id in res.kept
    # user-curated anchor preserved despite the entity being removed
    assert len(store.list_memory_edges(SLUG, node_id=m.node_id)) == 1


def test_idempotent_second_pass_is_noop(store) -> None:
    store.upsert_nodes(SLUG, [_code("nV", "verify", "partially changed")])
    m = _mem("verify checks the token")
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(SLUG, [_anchor(m.node_id, "auth.py#verify")])
    emb = FakeEmbedder(
        {"verify checks the token": [1.0, 0.0], "verify partially changed": [0.8, 0.6]}
    )
    llm = FakeLLM("VALID")
    rec = _reconciler(store, embedder=emb, llm=llm)
    delta = _delta(modified=["auth.py#verify"])
    rec.reconcile(SLUG, delta, refresh_started_at=REFRESH_AT)
    rec.reconcile(SLUG, delta, refresh_started_at=REFRESH_AT)
    assert llm.calls == 1  # second pass skipped via anchor_checked_at
