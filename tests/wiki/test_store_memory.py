"""Memory / doc-note / manifest store methods — JSON and Mongo in lockstep.

Parameterized across both backends so they behave identically (the same
contract the existing graph methods hold). Mongo path uses mongomock — no
real MongoDB required.
"""
from __future__ import annotations

import mongomock
import pytest
from mewbo_graph.wiki.memory_types import (
    DocPageNote,
    FileManifest,
    MemoryEdge,
    MemoryEmbedding,
    MemoryFilter,
    MemoryNode,
    MemoryProvenance,
)
from mewbo_graph.wiki.types import GraphEdge, GraphNode

SLUG = "org/repo"


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path):
    if request.param == "json":
        from mewbo_graph.wiki.store import JsonWikiStore

        return JsonWikiStore(root_dir=tmp_path / "wiki")
    from mewbo_graph.wiki.store import MongoWikiStore

    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki_mem")


# ── builders ────────────────────────────────────────────────────────────────


def _prov(source: str = "indexer") -> MemoryProvenance:
    return MemoryProvenance(
        author_agent="wiki-indexer", source=source, created_at="2026-06-05T00:00:00Z"
    )


def _mnode(content: str, *, slug: str = SLUG, **kw) -> MemoryNode:
    return MemoryNode(slug=slug, content=content, provenance=_prov(), **kw)


def _anchors(node_id: str, entity_key: str, *, invalid_at: str | None = None) -> MemoryEdge:
    return MemoryEdge(
        slug=SLUG,
        source=node_id,
        target=entity_key,
        type="ANCHORS",
        valid_at="2026-06-05T00:00:00Z",
        invalid_at=invalid_at,
    )


def _emb(node_id: str, vector: list[float]) -> MemoryEmbedding:
    return MemoryEmbedding(slug=SLUG, node_id=node_id, vector=vector, model="m", dim=len(vector))


# ── memory nodes ────────────────────────────────────────────────────────────


def test_memory_node_upsert_get_and_dedup_by_node_id(store) -> None:
    assert store.get_memory_node(SLUG, "missing") is None

    n1 = _mnode("AuthService verifies tokens")
    n2 = _mnode("Storage persists pages")
    store.upsert_memory_nodes(SLUG, [n1, n2])

    got = store.get_memory_node(SLUG, n1.node_id)
    assert got is not None and got.content == "AuthService verifies tokens"
    assert len(store.query_memory(SLUG)) == 2

    # Re-upsert identical content (same derived node_id) → no duplicate row.
    store.upsert_memory_nodes(SLUG, [_mnode("AuthService verifies tokens")])
    assert len(store.query_memory(SLUG)) == 2


def test_query_memory_applies_node_facets(store) -> None:
    store.upsert_memory_nodes(
        SLUG,
        [
            _mnode("a", kind="propositional", corpus="code"),
            _mnode("b", kind="prescriptive", corpus="code"),
            _mnode("c", kind="propositional", corpus="db"),
        ],
    )
    assert len(store.query_memory(SLUG, filt=MemoryFilter())) == 3
    assert len(store.query_memory(SLUG, filt=MemoryFilter(kind="prescriptive"))) == 1
    assert len(store.query_memory(SLUG, filt=MemoryFilter(corpus="db"))) == 1


# ── memory edges ────────────────────────────────────────────────────────────


def test_memory_edges_upsert_dedup_and_node_filter(store) -> None:
    n = _mnode("claim")
    store.upsert_memory_nodes(SLUG, [n])
    e1 = _anchors(n.node_id, "auth.py#AuthService")
    e2 = _anchors(n.node_id, "auth.py#verify")
    store.upsert_memory_edges(SLUG, [e1, e2])
    # dedup by (source,target,type): re-upsert e1 → still 2 edges total
    store.upsert_memory_edges(SLUG, [_anchors(n.node_id, "auth.py#AuthService")])

    edges = store.list_memory_edges(SLUG, node_id=n.node_id)
    assert len(edges) == 2
    assert {e.target for e in edges} == {"auth.py#AuthService", "auth.py#verify"}


def test_list_memory_edges_excludes_invalidated_by_default(store) -> None:
    n = _mnode("claim")
    store.upsert_memory_nodes(SLUG, [n])
    store.upsert_memory_edges(SLUG, [_anchors(n.node_id, "auth.py#A")])
    # invalidate by re-upserting same edge key with invalid_at set
    store.upsert_memory_edges(
        SLUG, [_anchors(n.node_id, "auth.py#A", invalid_at="2026-06-06T00:00:00Z")]
    )
    assert store.list_memory_edges(SLUG, node_id=n.node_id) == []
    assert len(store.list_memory_edges(SLUG, node_id=n.node_id, include_invalidated=True)) == 1


def test_memories_anchored_to_reverse_lookup(store) -> None:
    a = _mnode("claim a")
    b = _mnode("claim b")
    store.upsert_memory_nodes(SLUG, [a, b])
    store.upsert_memory_edges(
        SLUG,
        [
            _anchors(a.node_id, "auth.py#AuthService"),
            _anchors(b.node_id, "auth.py#AuthService"),
            _anchors(b.node_id, "store.py#Store", invalid_at="2026-06-06T00:00:00Z"),
        ],
    )
    hits = set(store.memories_anchored_to(SLUG, ["auth.py#AuthService"]))
    assert hits == {a.node_id, b.node_id}
    # invalidated anchor excluded by default, included on request
    assert store.memories_anchored_to(SLUG, ["store.py#Store"]) == []
    assert store.memories_anchored_to(
        SLUG, ["store.py#Store"], include_invalidated=True
    ) == [b.node_id]


# ── memory embeddings + vector search ───────────────────────────────────────


def test_memory_vector_search_ranks_by_cosine(store) -> None:
    a = _mnode("claim a")
    b = _mnode("claim b")
    store.upsert_memory_nodes(SLUG, [a, b])
    store.upsert_memory_embeddings(
        SLUG, [_emb(a.node_id, [1.0, 0.0]), _emb(b.node_id, [0.0, 1.0])]
    )
    hits = store.memory_vector_search(SLUG, [1.0, 0.0], k=2)
    assert hits[0].node_id == a.node_id


def test_memory_vector_search_respects_filter_and_validity(store) -> None:
    a = _mnode("claim a", corpus="code")
    b = _mnode("claim b", corpus="db")
    store.upsert_memory_nodes(SLUG, [a, b])
    store.upsert_memory_embeddings(
        SLUG, [_emb(a.node_id, [1.0, 0.0]), _emb(b.node_id, [0.9, 0.1])]
    )
    # both anchored, but only `a` is live
    store.upsert_memory_edges(
        SLUG,
        [
            _anchors(a.node_id, "auth.py#A"),
            _anchors(b.node_id, "db.py#B", invalid_at="2026-06-06T00:00:00Z"),
        ],
    )
    # corpus facet keeps only `a`
    hits = store.memory_vector_search(SLUG, [1.0, 0.0], k=5, filt=MemoryFilter(corpus="code"))
    assert {h.node_id for h in hits} == {a.node_id}
    # exclude_invalidated drops `b` (no live anchor) even without a corpus facet
    hits2 = store.memory_vector_search(SLUG, [1.0, 0.0], k=5, filt=MemoryFilter())
    assert {h.node_id for h in hits2} == {a.node_id}


def test_memory_embeddings_dedup_by_node_id(store) -> None:
    n = _mnode("claim")
    store.upsert_memory_nodes(SLUG, [n])
    store.upsert_memory_embeddings(SLUG, [_emb(n.node_id, [1.0, 0.0])])
    store.upsert_memory_embeddings(SLUG, [_emb(n.node_id, [0.0, 1.0])])
    hits = store.memory_vector_search(SLUG, [0.0, 1.0], k=5)
    assert len(hits) == 1


# ── doc notes ───────────────────────────────────────────────────────────────


def _doc(page_id: str = "overview", **kw) -> DocPageNote:
    base = dict(slug=SLUG, page_id=page_id, title="T", content_hash="h", page_type="concept")
    base.update(kw)
    return DocPageNote(**base)


def test_doc_notes_crud(store) -> None:
    assert store.get_doc_note(SLUG, "overview") is None
    store.upsert_doc_notes(SLUG, [_doc("overview"), _doc("auth", page_type="subsystem")])
    assert store.get_doc_note(SLUG, "auth").page_type == "subsystem"
    assert len(store.list_doc_notes(SLUG)) == 2
    # upsert overwrites by page_id
    store.upsert_doc_notes(SLUG, [_doc("overview", staleness_score=0.9)])
    assert store.get_doc_note(SLUG, "overview").staleness_score == 0.9
    assert len(store.list_doc_notes(SLUG)) == 2
    assert store.delete_doc_note(SLUG, "overview") is True
    assert store.delete_doc_note(SLUG, "overview") is False
    assert len(store.list_doc_notes(SLUG)) == 1


# ── file manifest ───────────────────────────────────────────────────────────


def _man(path: str, content_hash: str = "h", **kw) -> FileManifest:
    return FileManifest(slug=SLUG, path=path, content_hash=content_hash, **kw)


def test_file_manifest_crud(store) -> None:
    assert store.get_file_manifest(SLUG, "auth.py") is None
    store.upsert_file_manifest(
        SLUG,
        [
            _man("auth.py", "h1", entity_keys=["auth.py#AuthService"]),
            _man("store.py", "h2"),
        ],
    )
    assert store.get_file_manifest(SLUG, "auth.py").content_hash == "h1"
    assert len(store.list_file_manifest(SLUG)) == 2
    # overwrite by path
    store.upsert_file_manifest(SLUG, [_man("auth.py", "h1b")])
    assert store.get_file_manifest(SLUG, "auth.py").content_hash == "h1b"
    assert len(store.list_file_manifest(SLUG)) == 2
    assert store.delete_file_manifest(SLUG, "store.py") is True
    assert store.delete_file_manifest(SLUG, "store.py") is False
    assert len(store.list_file_manifest(SLUG)) == 1


# ── scoped graph deletes (used by GraphDeltaIndexer) ────────────────────────


def _gn(nid: str, typ: str, name: str, f: str, rng: tuple[int, int]) -> GraphNode:
    return GraphNode(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=rng)


def _seed_graph(store) -> None:
    store.upsert_nodes(
        SLUG,
        [
            _gn("fA", "File", "auth.py", "auth.py", (0, 10)),
            _gn("nA", "Function", "verify", "auth.py", (1, 5)),
            _gn("fB", "File", "store.py", "store.py", (0, 10)),
            _gn("nB", "Function", "save", "store.py", (1, 5)),
        ],
    )
    store.upsert_edges(
        SLUG,
        [
            GraphEdge(slug=SLUG, source="fA", target="nA", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="nA", target="nB", type="CALLS"),
            GraphEdge(slug=SLUG, source="fB", target="nB", type="CONTAINS"),
        ],
    )


def test_delete_nodes_by_file(store) -> None:
    _seed_graph(store)
    removed = store.delete_nodes_by_file(SLUG, "auth.py")
    assert removed == 2
    remaining = {n.node_id for n in store.query_graph(SLUG)}
    assert remaining == {"fB", "nB"}


def test_delete_edges_by_source_file(store) -> None:
    _seed_graph(store)
    # edges sourced from auth.py nodes: fA->nA (CONTAINS), nA->nB (CALLS)
    removed = store.delete_edges_by_source_file(SLUG, "auth.py")
    assert removed == 2
    remaining = {(e.source, e.target, e.type) for e in store.list_edges(SLUG)}
    assert remaining == {("fB", "nB", "CONTAINS")}
