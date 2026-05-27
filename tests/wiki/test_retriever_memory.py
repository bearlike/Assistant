"""HybridRetriever memory multiplexing — MultiplexExpander + memory_expand."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_graph.wiki.memory_types import (
    MemoryEdge,
    MemoryEmbedding,
    MemoryFilter,
    MemoryNode,
    MemoryProvenance,
)
from mewbo_graph.wiki.retriever import HybridRetriever, MultiplexExpander
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphEdge, GraphNode

SLUG = "x/y"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _embedder(qvec):
    emb = MagicMock()
    emb.embed_nodes.return_value = [MagicMock(vector=qvec)]
    return emb


def _prov():
    return MemoryProvenance(author_agent="a", source="indexer", created_at="2026-06-05T00:00:00Z")


def _gn(nid: str, name: str, f: str) -> GraphNode:
    return GraphNode(slug=SLUG, node_id=nid, type="Function", name=name, file=f, range=(0, 9))


def _seed_code(store):
    store.upsert_nodes(SLUG, [_gn("fV", "verify", "auth.py"), _gn("fS", "save", "store.py")])


def _seed_memory(store, node, vec, anchor, invalid=False):
    store.upsert_memory_nodes(SLUG, [node])
    emb = MemoryEmbedding(slug=SLUG, node_id=node.node_id, vector=vec, model="m", dim=len(vec))
    store.upsert_memory_embeddings(SLUG, [emb])
    store.upsert_memory_edges(
        SLUG,
        [
            MemoryEdge(
                slug=SLUG, source=node.node_id, target=anchor, type="ANCHORS",
                valid_at="2026-06-05T00:00:00Z",
                invalid_at="2026-06-06T00:00:00Z" if invalid else None,
            )
        ],
    )


# ── backward compatibility ──────────────────────────────────────────────────


def test_memory_expand_false_is_unchanged(store) -> None:
    _seed_code(store)
    r = HybridRetriever(store=store, embedder=_embedder([1.0, 0.0]))
    hits = r.search(SLUG, "verify", k=5, sources="graph")
    assert all(h.kind in {"page", "node"} for h in hits)


# ── memory seeding + expansion ──────────────────────────────────────────────


def test_memory_seed_surfaces_memory_hit(store) -> None:
    _seed_code(store)
    node = MemoryNode(slug=SLUG, content="Sessions expire after one hour", provenance=_prov())
    _seed_memory(store, node, [1.0, 0.0], "auth.py#verify")
    r = HybridRetriever(store=store, embedder=_embedder([1.0, 0.0]))
    hits = r.search(SLUG, "session expiry", k=10, sources="pages", memory_expand=True)
    mem_hits = [h for h in hits if h.kind == "memory"]
    assert mem_hits and mem_hits[0].id == node.node_id
    assert "expire" in mem_hits[0].snippet


def test_memory_expansion_pulls_anchored_code_node(store) -> None:
    _seed_code(store)
    node = MemoryNode(slug=SLUG, content="verify validates the signature", provenance=_prov())
    _seed_memory(store, node, [1.0, 0.0], "auth.py#verify")
    r = HybridRetriever(store=store, embedder=_embedder([1.0, 0.0]))
    hits = r.search(SLUG, "signature", k=10, sources="pages", memory_expand=True)
    ids = {(h.kind, h.id) for h in hits}
    # the anchored code node is reachable purely via the memory layer
    assert ("node", "fV") in ids


def test_memory_filter_excludes_other_corpus(store) -> None:
    _seed_code(store)
    node = MemoryNode(slug=SLUG, content="DB rows are immutable", corpus="db", provenance=_prov())
    _seed_memory(store, node, [1.0, 0.0], "auth.py#verify")
    r = HybridRetriever(store=store, embedder=_embedder([1.0, 0.0]))
    hits = r.search(
        SLUG, "rows", k=10, sources="pages", memory_expand=True,
        memory_filters=MemoryFilter(corpus="code"),
    )
    assert not [h for h in hits if h.kind == "memory"]


def test_invalidated_memory_excluded(store) -> None:
    _seed_code(store)
    node = MemoryNode(slug=SLUG, content="Stale fact about verify", provenance=_prov())
    _seed_memory(store, node, [1.0, 0.0], "auth.py#verify", invalid=True)
    r = HybridRetriever(store=store, embedder=_embedder([1.0, 0.0]))
    hits = r.search(SLUG, "verify", k=10, sources="pages", memory_expand=True)
    assert not [h for h in hits if h.kind == "memory"]


# ── expander unit: hub damping ──────────────────────────────────────────────


def test_expander_hub_damps_high_degree_anchor(store) -> None:
    # fV is a hub (many neighbours); fS is not. Same memory similarity → the
    # hub's expansion contribution is damped below the non-hub's.
    nodes = [_gn("fV", "verify", "auth.py"), _gn("fS", "save", "store.py")]
    nodes += [_gn(f"n{i}", f"n{i}", "x.py") for i in range(6)]
    store.upsert_nodes(SLUG, nodes)
    store.upsert_edges(
        SLUG, [GraphEdge(slug=SLUG, source="fV", target=f"n{i}", type="CALLS") for i in range(6)]
    )
    hub = MemoryNode(slug=SLUG, content="claim hub", provenance=_prov())
    nonhub = MemoryNode(slug=SLUG, content="claim nonhub", provenance=_prov())
    _seed_memory(store, hub, [1.0, 0.0], "auth.py#verify")
    _seed_memory(store, nonhub, [1.0, 0.0], "store.py#save")

    expander = MultiplexExpander(store=store, hub_degree=3, expansion_hops=1)
    hits = expander.expand(SLUG, [1.0, 0.0], k=10)
    by_id = {h.id: h.score for h in hits if h.kind == "node"}
    assert by_id["fV"] < by_id["fS"]  # hub damped below non-hub
