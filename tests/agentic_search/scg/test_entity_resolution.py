"""Tests for TypeAligner — map-time, type-level cross-source entity resolution.

Exercises the atomic class end-to-end against a seeded JSON store (no MongoDB
required) with an injected fake LLM. The contract is *abstain-by-default*:

* two near-identical ``entity_type`` nodes across sources -> exactly one
  ``RESOLVES_TO`` edge (``method="type_align"``) carrying evidence[];
* dissimilar types -> no edge (abstain);
* deterministic — same seed produces the same edge set every call;
* the optional LLM pass only fires for *ambiguous* (band) pairs and is
  injected (never a real model).

Instance-level ER is explicitly NOT covered here — the probe agent does that
natively online; this layer is the offline, type-level hypothesis writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.entity_resolution import TypeAligner
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import (
    CapabilityBinding,
    ScgEdge,
    ScgNode,
)

# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A fresh JSON-backed SCG store under a throwaway temp dir."""
    return JsonScgStore(root_dir=tmp_path / "scg")


def _entity(
    source_id: str,
    name: str,
    fields: list[str],
    doc: str = "",
) -> ScgNode:
    """An ``entity_type`` node whose field-overlap is modelled via bindings."""
    return ScgNode(
        source_key=f"{source_id}#{name}",
        kind="entity_type",
        source_id=source_id,
        name=name,
        doc=doc,
        bindings=[
            CapabilityBinding(field_key=f"{source_id}#{name}.{f}", mode="optional")
            for f in fields
        ],
    )


def _parsed_entity(
    store: JsonScgStore, source_id: str, name: str, fields: list[str]
) -> None:
    """Persist an ``entity_type`` as a fresh parser emits it: EMPTY bindings,
    one ``field`` node per field linked by a ``HAS_FIELD`` edge.

    This is the shape OpenAPI/MCP providers actually produce — the field set
    lives in the graph, not in ``entity_type.bindings``.
    """
    entity_key = f"{source_id}#{name}"
    nodes = [
        ScgNode(source_key=entity_key, kind="entity_type", source_id=source_id, name=name)
    ]
    edges: list[ScgEdge] = []
    for field in fields:
        field_key = f"{entity_key}.{field}"
        nodes.append(
            ScgNode(source_key=field_key, kind="field", source_id=source_id, name=field)
        )
        edges.append(ScgEdge(source=entity_key, target=field_key, kind="HAS_FIELD"))
    store.upsert_nodes(nodes)
    store.upsert_edges(edges)


class _RecordingLLM:
    """A deterministic fake LLM: returns a canned verdict, records prompts."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.verdict


# ── positive evidence -> one edge ───────────────────────────────────────────


def test_near_identical_types_emit_one_resolves_to_edge(store: JsonScgStore) -> None:
    """Jira.Issue <=> Linear.Ticket-style overlap yields one RESOLVES_TO edge."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "title", "status", "assignee"]),
            _entity("linear", "Issue", ["id", "title", "status", "assignee"]),
        ]
    )
    edges = TypeAligner(store=store).align(["jira", "linear"])

    assert len(edges) == 1
    edge = edges[0]
    assert edge.kind == "RESOLVES_TO"
    assert edge.method == "type_align"
    assert {edge.source, edge.target} == {"jira#Issue", "linear#Issue"}
    assert 0.0 < edge.weight <= 1.0
    assert edge.evidence  # positive evidence is recorded
    assert edge.binds is None or isinstance(edge.binds, tuple)


def test_persisted_edge_is_durable(store: JsonScgStore) -> None:
    """align() upserts the hypothesis edge into the store (durable, re-readable)."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "title", "status"]),
            _entity("linear", "Ticket", ["id", "title", "status"]),
        ]
    )
    TypeAligner(store=store).align(["jira", "linear"])
    persisted = store.list_edges(kind="RESOLVES_TO")
    assert len(persisted) == 1
    assert persisted[0].method == "type_align"


# ── parsed-graph path (HAS_FIELD field nodes, empty bindings) ───────────────


def test_parsed_graph_equivalent_types_emit_one_edge(store: JsonScgStore) -> None:
    """Two equivalent types whose fields come from HAS_FIELD nodes resolve.

    Regression: freshly-parsed OpenAPI/MCP sources leave ``bindings`` empty and
    expose fields as ``field`` nodes via ``HAS_FIELD`` edges. The aligner must
    derive the field set from those neighbours so type-ER fires on parsed
    sources — not only on the (later) binding-bearing shape.
    """
    _parsed_entity(store, "jira", "Issue", ["id", "title", "status", "assignee"])
    _parsed_entity(store, "linear", "Issue", ["id", "title", "status", "assignee"])
    edges = TypeAligner(store=store).align(["jira", "linear"])

    assert len(edges) == 1
    assert edges[0].kind == "RESOLVES_TO"
    assert edges[0].method == "type_align"
    assert {edges[0].source, edges[0].target} == {"jira#Issue", "linear#Issue"}
    assert edges[0].evidence  # field-overlap evidence derived from the graph


def test_parsed_graph_dissimilar_types_abstain(store: JsonScgStore) -> None:
    """Parsed types with no shared fields + unrelated names abstain (no LLM)."""
    _parsed_entity(store, "github", "Repository", ["owner", "stars", "language"])
    _parsed_entity(store, "stripe", "Invoice", ["amount", "currency", "due_date"])
    edges = TypeAligner(store=store).align(["github", "stripe"])
    assert edges == []


# ── abstain by default ──────────────────────────────────────────────────────


def test_dissimilar_types_abstain(store: JsonScgStore) -> None:
    """No shared fields and unrelated names -> no edge (abstain)."""
    store.upsert_nodes(
        [
            _entity("github", "Repository", ["owner", "stars", "language"]),
            _entity("stripe", "Invoice", ["amount", "currency", "due_date"]),
        ]
    )
    edges = TypeAligner(store=store).align(["github", "stripe"])
    assert edges == []


def test_no_llm_means_band_pairs_abstain(store: JsonScgStore) -> None:
    """Ambiguous (band) pairs abstain when no LLM is injected — NONE default."""
    # Partial overlap: a single shared field name -> ambiguous, not confident.
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "summary", "reporter"]),
            _entity("linear", "Ticket", ["id", "team", "cycle"]),
        ]
    )
    edges = TypeAligner(store=store).align(["jira", "linear"])
    assert edges == []


def test_same_source_pairs_are_never_aligned(store: JsonScgStore) -> None:
    """Type alignment is strictly CROSS-source; intra-source pairs are skipped."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "title", "status"]),
            _entity("jira", "Story", ["id", "title", "status"]),
        ]
    )
    edges = TypeAligner(store=store).align(["jira"])
    assert edges == []


def test_non_entity_type_nodes_are_ignored(store: JsonScgStore) -> None:
    """Only ``entity_type`` nodes participate; capabilities/fields are skipped."""
    store.upsert_nodes(
        [
            ScgNode(
                source_key="jira#search",
                kind="capability",
                source_id="jira",
                name="search",
            ),
            ScgNode(
                source_key="linear#search",
                kind="capability",
                source_id="linear",
                name="search",
            ),
        ]
    )
    edges = TypeAligner(store=store).align(["jira", "linear"])
    assert edges == []


# ── injected LLM disambiguation (band only) ─────────────────────────────────


def test_band_pair_promoted_by_llm_yes(store: JsonScgStore) -> None:
    """An ambiguous pair the LLM affirms is promoted to a RESOLVES_TO edge."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "summary", "reporter"]),
            _entity("linear", "Ticket", ["id", "team", "cycle"]),
        ]
    )
    llm = _RecordingLLM("yes")
    edges = TypeAligner(store=store, llm=llm).align(["jira", "linear"])

    assert len(edges) == 1
    assert edges[0].kind == "RESOLVES_TO"
    assert edges[0].method == "type_align"
    assert llm.prompts  # the band pair was escalated to the LLM
    assert any("llm" in ev.lower() for ev in edges[0].evidence)


def test_band_pair_rejected_by_llm_no(store: JsonScgStore) -> None:
    """An ambiguous pair the LLM rejects stays abstained (no edge)."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "summary", "reporter"]),
            _entity("linear", "Ticket", ["id", "team", "cycle"]),
        ]
    )
    llm = _RecordingLLM("no")
    edges = TypeAligner(store=store, llm=llm).align(["jira", "linear"])
    assert edges == []
    assert llm.prompts  # LLM was consulted, it declined


def test_confident_pairs_skip_the_llm(store: JsonScgStore) -> None:
    """High-overlap pairs are decided by heuristic alone — the LLM is not called."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "title", "status", "assignee"]),
            _entity("linear", "Issue", ["id", "title", "status", "assignee"]),
        ]
    )
    llm = _RecordingLLM("no")  # would veto if consulted
    edges = TypeAligner(store=store, llm=llm).align(["jira", "linear"])
    assert len(edges) == 1  # heuristic-confident, LLM never asked
    assert llm.prompts == []


# ── determinism ─────────────────────────────────────────────────────────────


def test_alignment_is_deterministic(store: JsonScgStore) -> None:
    """Same seed -> identical edge set (source/target/kind/weight) every run."""
    store.upsert_nodes(
        [
            _entity("jira", "Issue", ["id", "title", "status", "assignee"]),
            _entity("linear", "Issue", ["id", "title", "status", "assignee"]),
        ]
    )
    aligner = TypeAligner(store=store)
    first = aligner.align(["jira", "linear"])
    second = aligner.align(["jira", "linear"])

    def _key(es: list) -> list[tuple[str, str, str, float]]:
        return sorted((e.source, e.target, e.kind, e.weight) for e in es)

    assert _key(first) == _key(second)


def test_edge_orientation_is_stable(store: JsonScgStore) -> None:
    """source/target ordering is canonical (sorted) regardless of input order."""
    store.upsert_nodes(
        [
            _entity("linear", "Issue", ["id", "title", "status"]),
            _entity("jira", "Issue", ["id", "title", "status"]),
        ]
    )
    edges = TypeAligner(store=store).align(["linear", "jira"])
    assert len(edges) == 1
    # Canonical orientation: lexicographically smaller source_key first.
    assert edges[0].source < edges[0].target


def test_empty_sources_returns_no_edges(store: JsonScgStore) -> None:
    """An empty / single-source request yields nothing, never raises."""
    assert TypeAligner(store=store).align([]) == []
    store.upsert_nodes([_entity("jira", "Issue", ["id"])])
    assert TypeAligner(store=store).align(["jira"]) == []
