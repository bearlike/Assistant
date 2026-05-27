"""Round-trip + validation tests for the multiplex memory wire types.

These mirror ``test_types.py`` conventions: extra-forbid enforcement,
JSON round-trips, and the deterministic ``node_id`` derivation that the
3-tier dedup exact-match tier relies on.
"""
from __future__ import annotations

import hashlib

import pydantic
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


def _prov(source: str = "indexer") -> MemoryProvenance:
    return MemoryProvenance(
        author_agent="wiki-indexer",
        source=source,
        created_at="2026-06-05T00:00:00Z",
    )


def _node(
    content: str = "AuthService verifies bearer tokens", slug: str = "org/repo"
) -> MemoryNode:
    return MemoryNode(slug=slug, content=content, provenance=_prov())


# ── node_id derivation ──────────────────────────────────────────────────────


def test_memory_node_id_is_sha1_of_slug_and_normalized_content() -> None:
    node = _node(content="AuthService verifies tokens", slug="org/repo")
    expected = hashlib.sha1(
        b"org/repo|authservice verifies tokens"
    ).hexdigest()[:16]
    assert node.node_id == expected


def test_memory_node_id_ignores_supplied_node_id() -> None:
    # A caller-supplied node_id must be overridden by the derived one so the
    # exact-dup tier cannot be poisoned by a bogus id.
    node = MemoryNode(
        slug="org/repo",
        node_id="deadbeefdeadbeef",
        content="AuthService verifies tokens",
        provenance=_prov(),
    )
    assert node.node_id != "deadbeefdeadbeef"
    assert node.node_id == MemoryNode.compute_node_id("org/repo", "AuthService verifies tokens")


def test_memory_node_id_normalizes_case_and_whitespace() -> None:
    a = _node(content="Hello World")
    b = _node(content="  hello   world  ")
    # Whitespace *inside* the string is preserved; only edges are stripped and
    # case is lowered — so leading/trailing differs collapse, internal does not.
    assert a.node_id == _node(content="hello world").node_id
    assert b.node_id == _node(content="hello   world").node_id


def test_memory_node_content_capped_at_200_chars() -> None:
    with pytest.raises(pydantic.ValidationError):
        _node(content="x" * 201)


def test_memory_node_extra_forbidden() -> None:
    with pytest.raises(pydantic.ValidationError):
        MemoryNode(
            slug="org/repo",
            content="ok",
            provenance=_prov(),
            bogus_field=1,
        )


def test_memory_node_roundtrip() -> None:
    node = _node()
    dumped = node.model_dump(mode="json", by_alias=True)
    reparsed = MemoryNode.model_validate(dumped)
    assert reparsed == node


def test_memory_node_defaults() -> None:
    node = _node()
    assert node.kind == "propositional"
    assert node.corpus == "code"
    assert node.labels == []
    assert node.anchor_checked_at is None


def test_memory_node_invalid_kind_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        MemoryNode(slug="s", content="c", kind="bogus", provenance=_prov())


# ── provenance ──────────────────────────────────────────────────────────────


def test_provenance_rejects_unknown_source() -> None:
    with pytest.raises(pydantic.ValidationError):
        MemoryProvenance(
            author_agent="x", source="hacker", created_at="2026-06-05T00:00:00Z"
        )


def test_provenance_accepts_known_sources() -> None:
    for src in ("indexer", "qa", "on_demand"):
        assert _prov(src).source == src


# ── edges ───────────────────────────────────────────────────────────────────


def test_memory_edge_defaults() -> None:
    edge = MemoryEdge(
        slug="org/repo",
        source="abc123",
        target="auth.py#AuthService",
        type="ANCHORS",
        valid_at="2026-06-05T00:00:00Z",
    )
    assert edge.weight == 1.0
    assert edge.invalid_at is None


def test_memory_edge_invalid_type_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        MemoryEdge(
            slug="s", source="a", target="b", type="OWNS", valid_at="t"
        )


# ── embedding mirrors Embedding ─────────────────────────────────────────────


def test_memory_embedding_fields() -> None:
    emb = MemoryEmbedding(
        slug="org/repo", node_id="abc123", vector=[0.1, 0.2], model="m", dim=2
    )
    assert emb.dim == 2
    assert emb.vector == [0.1, 0.2]


# ── DocPageNote ─────────────────────────────────────────────────────────────


def test_doc_page_note_defaults() -> None:
    note = DocPageNote(
        slug="org/repo",
        page_id="overview",
        title="Overview",
        content_hash="h0",
        page_type="concept",
    )
    assert note.anchor_keys == []
    assert note.staleness_score == 0.0
    assert note.staleness_reason == "clean"
    assert note.generation_policy == "keep"
    assert note.last_indexed_commit is None


def test_doc_page_note_invalid_page_type_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        DocPageNote(
            slug="s", page_id="p", title="t", content_hash="h", page_type="blog"
        )


def test_doc_page_note_roundtrip() -> None:
    note = DocPageNote(
        slug="org/repo",
        page_id="auth",
        title="Auth",
        content_hash="h1",
        page_type="subsystem",
        anchor_keys=["auth.py#AuthService", "auth.py#verify"],
        generation_policy="regenerate",
    )
    reparsed = DocPageNote.model_validate(note.model_dump(mode="json", by_alias=True))
    assert reparsed == note


# ── FileManifest ────────────────────────────────────────────────────────────


def test_file_manifest_defaults() -> None:
    man = FileManifest(slug="org/repo", path="auth.py", content_hash="h2")
    assert man.entity_keys == []
    assert man.last_indexed_commit is None


# ── MemoryFilter ────────────────────────────────────────────────────────────


def test_memory_filter_defaults_exclude_invalidated() -> None:
    flt = MemoryFilter()
    assert flt.exclude_invalidated is True
    assert flt.corpus is None
    assert flt.source is None
    assert flt.kind is None
    assert flt.labels is None


def test_memory_filter_rejects_bad_source() -> None:
    with pytest.raises(pydantic.ValidationError):
        MemoryFilter(source="nope")


def test_memory_filter_matches_node_by_facets() -> None:
    node = MemoryNode(
        slug="org/repo",
        content="claim",
        kind="prescriptive",
        corpus="code",
        labels=["security", "auth"],
        provenance=_prov("qa"),
    )
    assert MemoryFilter().matches_node(node) is True
    assert MemoryFilter(corpus="code").matches_node(node) is True
    assert MemoryFilter(corpus="db").matches_node(node) is False
    assert MemoryFilter(source="qa").matches_node(node) is True
    assert MemoryFilter(source="indexer").matches_node(node) is False
    assert MemoryFilter(kind="prescriptive").matches_node(node) is True
    assert MemoryFilter(kind="propositional").matches_node(node) is False
    # labels filter is subset semantics
    assert MemoryFilter(labels=["auth"]).matches_node(node) is True
    assert MemoryFilter(labels=["auth", "missing"]).matches_node(node) is False
