"""GraphDeltaIndexer — scoped retract + re-parse + reverse-dependency closure."""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.graph import GraphParseResult, _stable_id
from mewbo_graph.wiki.memory_types import FileManifest
from mewbo_graph.wiki.refresh import ChangeSet, GraphDeltaIndexer
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphEdge, GraphNode

from .conftest import FakeParser

SLUG = "org/repo"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _node(nid, typ, name, f):
    return GraphNode(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


def _seed_two_file_graph(store):
    """a.py defines verify(); b.py's caller() CALLS verify (cross-file edge)."""
    syn_verify = _stable_id(SLUG, "Function", "verify", "<external>", 0)
    store.upsert_nodes(
        SLUG,
        [
            _node("fileA", "File", "a.py", "a.py"),
            _node("nVerify", "Function", "verify", "a.py"),
            _node("fileB", "File", "b.py", "b.py"),
            _node("nCaller", "Function", "caller", "b.py"),
        ],
    )
    store.upsert_edges(
        SLUG,
        [
            GraphEdge(slug=SLUG, source="fileA", target="nVerify", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="fileB", target="nCaller", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="nCaller", target=syn_verify, type="CALLS"),
        ],
    )
    store.upsert_file_manifest(
        SLUG,
        [
            FileManifest(
                slug=SLUG, path="a.py", content_hash="hA",
                entity_keys=["a.py", "a.py#verify"],
            ),
            FileManifest(
                slug=SLUG, path="b.py", content_hash="hB",
                entity_keys=["b.py", "b.py#caller"],
            ),
        ],
    )


def test_modified_file_reparses_and_finds_reverse_dependents(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    # a.py re-parses to: keep verify, add newhelper
    reparse = GraphParseResult(
        nodes=[
            _node("fileA2", "File", "a.py", "a.py"),
            _node("nVerify2", "Function", "verify", "a.py"),
            _node("nHelper", "Function", "newhelper", "a.py"),
        ],
        edges=[
            GraphEdge(slug=SLUG, source="fileA2", target="nVerify2", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="fileA2", target="nHelper", type="CONTAINS"),
        ],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": reparse}))
    change = ChangeSet(
        added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"}
    )
    delta = indexer.apply(SLUG, root, change, commit="c2")

    assert "a.py#newhelper" in delta.added_keys
    assert "a.py#verify" in delta.modified_keys
    # reverse-dependency closure: b.py#caller CALLS verify → impacted
    assert "b.py#caller" in delta.affected
    # graph mutated: stale node gone, new node present
    ids = {n.node_id for n in store.query_graph(SLUG)}
    assert "nVerify" not in ids and "nHelper" in ids
    # manifest refreshed for a.py
    man = store.get_file_manifest(SLUG, "a.py")
    assert man.content_hash == "hA2"
    assert "a.py#newhelper" in man.entity_keys


def test_early_cutoff_when_reparse_identical(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    # identical re-parse (same entity_keys + same edges) → early cutoff
    reparse = GraphParseResult(
        nodes=[
            _node("fileA", "File", "a.py", "a.py"),
            _node("nVerify", "Function", "verify", "a.py"),
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA", target="nVerify", type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": reparse}))
    change = ChangeSet(added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"})
    delta = indexer.apply(SLUG, root, change, commit="c2")
    assert "a.py" in delta.early_cutoff_files
    assert delta.affected == frozenset()  # zero downstream work
    # manifest hash still advances (file content changed even if graph didn't)
    assert store.get_file_manifest(SLUG, "a.py").content_hash == "hA2"


def test_deleted_file_retracts_and_flags_dependents(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    indexer = GraphDeltaIndexer(store, parser=FakeParser({}))
    change = ChangeSet(added=[], modified=[], deleted=["a.py"], current_hashes={})
    delta = indexer.apply(SLUG, root, change, commit="c2")
    assert "a.py#verify" in delta.removed_keys
    assert "b.py#caller" in delta.affected  # caller of removed verify
    # a.py nodes retracted; manifest entry gone
    files = {n.file for n in store.query_graph(SLUG)}
    assert "a.py" not in files
    assert store.get_file_manifest(SLUG, "a.py") is None


def test_added_file_indexes_new_entities(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    reparse = GraphParseResult(
        nodes=[_node("fileC", "File", "c.py", "c.py"), _node("nNew", "Function", "fresh", "c.py")],
        edges=[GraphEdge(slug=SLUG, source="fileC", target="nNew", type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"c.py": reparse}))
    change = ChangeSet(added=["c.py"], modified=[], deleted=[], current_hashes={"c.py": "hC"})
    delta = indexer.apply(SLUG, root, change, commit="c2")
    assert "c.py#fresh" in delta.added_keys
    assert store.get_file_manifest(SLUG, "c.py").content_hash == "hC"
