"""RefreshOrchestrator — plan-then-act over the four deterministic stages."""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.graph import GraphParseResult, _stable_id
from mewbo_graph.wiki.memory_types import (
    FileManifest,
    MemoryEdge,
    MemoryNode,
    MemoryProvenance,
)
from mewbo_graph.wiki.refresh import GraphDeltaIndexer, RefreshOrchestrator
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    GraphEdge,
    ScopePreview,
    SourceRef,
    WikiPage,
    make_graph_node,
)

from .conftest import FakeEmbedder, FakeParser

SLUG = "org/repo"


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _node(nid, typ, name, f):
    return make_graph_node(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


def _page(page_id, sources):
    return WikiPage(
        id=page_id, title=page_id.title(),
        frontmatter=Frontmatter(
            title=page_id.title(), slug=page_id,
            relevantSources=[SourceRef(path=p) for p in sources],
        ),
        body=f"# {page_id}", toc=[], nav=[],
    )


def _seed(store):
    """auth.py defines verify(); a memory + a doc page anchor to it."""
    syn = _stable_id(SLUG, "Function", "verify", "<external>", 0)
    store.upsert_nodes(
        SLUG,
        [
            _node("fileA", "File", "auth.py", "auth.py"),
            _node("nVerify", "Function", "verify", "auth.py"),
            _node("fileB", "File", "b.py", "b.py"),
            _node("nCaller", "Function", "caller", "b.py"),
        ],
    )
    store.upsert_edges(SLUG, [GraphEdge(slug=SLUG, source="nCaller", target=syn, type="CALLS")])
    store.upsert_file_manifest(
        SLUG,
        [
            FileManifest(slug=SLUG, path="auth.py", content_hash="hA",
                         entity_keys=["auth.py", "auth.py#verify"]),
            FileManifest(slug=SLUG, path="b.py", content_hash="hB",
                         entity_keys=["b.py", "b.py#caller"]),
        ],
    )
    m = MemoryNode(
        slug=SLUG, content="verify checks the bearer token",
        provenance=MemoryProvenance(author_agent="a", source="indexer", created_at="t0"),
    )
    store.upsert_memory_nodes(SLUG, [m])
    store.upsert_memory_edges(
        SLUG, [MemoryEdge(slug=SLUG, source=m.node_id, target="auth.py#verify",
                          type="ANCHORS", valid_at="t0")]
    )
    store.save_page(SLUG, _page("auth", ["auth.py"]))
    return m


def _orchestrator(store, results):
    return RefreshOrchestrator(
        store=store,
        graph_indexer=GraphDeltaIndexer(store, parser=FakeParser(results)),
        clock=lambda: "2026-06-05T12:00:00Z",
    )


def _write(tmp_path, rel, content):
    root = tmp_path / "clone"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return root


def test_noop_when_nothing_changed(store, tmp_path) -> None:
    _seed(store)
    # working tree hashes match the manifest → no change
    root = tmp_path / "clone"
    (root).mkdir()
    # files not provided → detector sees only deletions unless we pass them;
    # pass empty file list with matching manifest is "all deleted", so instead
    # seed files whose hash equals the manifest is impractical — use the empty
    # manifest path: a fresh slug with no manifest and no files = empty.
    orch = _orchestrator(store, {})
    report = orch.refresh("fresh/slug", root, [], commit="c1")
    assert report.is_noop


def test_refresh_runs_all_stages_and_aggregates(store, tmp_path) -> None:
    _seed(store)
    # mutate auth.py on disk so the content hash differs from the manifest
    root = _write(tmp_path, "auth.py", "def verify(): ...  # changed")
    reparse = GraphParseResult(
        nodes=[
            _node("fileA2", "File", "auth.py", "auth.py"),
            _node("nVerify2", "Function", "verify", "auth.py"),
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA2", target="nVerify2", type="CONTAINS")],
        skipped=[],
    )
    orch = _orchestrator(store, {"auth.py": reparse})
    files = [p for p in root.rglob("*") if p.is_file()]
    report = orch.refresh(SLUG, root, files, commit="c2")

    assert not report.is_noop
    # graph: auth.py modified; b.py#caller reverse-dependent
    assert "auth.py#verify" in report.graph.modified_keys
    assert "b.py#caller" in report.graph.affected
    # docs: the auth page anchored to auth.py is now stale
    assert "auth" in report.pages_to_regenerate
    # scope preview is populated
    sp = report.scope_preview()
    assert sp.files_modified == 1
    assert sp.affected_entities >= 1
    # manifest advanced for auth.py
    assert store.get_file_manifest(SLUG, "auth.py").content_hash != "hA"


def test_from_store_threads_its_embedder_into_the_graph_delta(store, tmp_path) -> None:
    """``from_store``'s embedder reaches the graph stage, not just memory/docs.

    It was already handed to the memory reconciler and the doc planner while
    the stage that actually RE-PARSES code got none — so a refresh built this
    way rewrote the graph and left every changed symbol unvectorised. Driving
    the real composer end to end is what pins that wiring.
    """
    _seed(store)
    root = _write(tmp_path, "auth.py", "def verify(): ...  # changed")
    reparse = GraphParseResult(
        nodes=[
            _node("fileA2", "File", "auth.py", "auth.py"),
            _node("nVerify2", "Function", "verify", "auth.py"),
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA2", target="nVerify2", type="CONTAINS")],
        skipped=[],
    )
    orch = RefreshOrchestrator.from_store(
        store, slug="acme/repo",
        parser=FakeParser({"auth.py": reparse}),
        embedder=FakeEmbedder(),
        clock=lambda: "2026-06-05T12:00:00Z",
    )

    orch.refresh(SLUG, root, [p for p in root.rglob("*") if p.is_file()], commit="c2")

    vectorised = {e.node_id for e in store.vector_search(SLUG, [1.0, 0.0], k=100)}
    assert {"fileA2", "nVerify2"} <= vectorised


def _reparse_one() -> GraphParseResult:
    """One file's worth of re-parsed nodes, shared by the default-path tests."""
    return GraphParseResult(
        nodes=[
            _node("fileA2", "File", "auth.py", "auth.py"),
            _node("nVerify2", "Function", "verify", "auth.py"),
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA2", target="nVerify2", type="CONTAINS")],
        skipped=[],
    )


def test_from_store_resolves_an_embedder_when_the_caller_passes_none(
    store, tmp_path, monkeypatch
) -> None:
    """The DEFAULT construction embeds — passing no embedder is not "skip embedding".

    Every stage but the graph delta degrades harmlessly without an embedder, so
    threading the caller's ``None`` straight down reads as correct and passes
    every test that injects a fake. It is not correct here: the graph stage has
    already DELETED the previous vectors by the time it would embed, so no
    embedder means "strip the vectors off everything this refresh touched"
    rather than "leave them alone". Since this classmethod is the only way a
    production caller builds the composer, an unresolved default would ship the
    whole feature inert while the suite stayed green.
    """
    fake = FakeEmbedder()
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.make_embedder_or_none", lambda model=None: fake
    )
    _seed(store)
    root = _write(tmp_path, "auth.py", "def verify(): ...  # changed")

    orch = RefreshOrchestrator.from_store(
        store, slug="acme/repo",
        parser=FakeParser({"auth.py": _reparse_one()}),
        clock=lambda: "2026-06-05T12:00:00Z",
    )
    orch.refresh(SLUG, root, [p for p in root.rglob("*") if p.is_file()], commit="c2")

    vectorised = {e.node_id for e in store.vector_search(SLUG, [1.0, 0.0], k=100)}
    assert {"fileA2", "nVerify2"} <= vectorised


def test_from_store_honours_the_operator_switch_and_resolves_nothing_when_off(
    store, tmp_path, monkeypatch
) -> None:
    """``wiki.embedding.enabled=false`` means no embedder, not a broken one.

    The switch is read by the full index too; a refresh that resolved an
    embedder anyway would be the one caller ignoring an operator's setting.
    """
    resolved: list[bool] = []

    def _resolve() -> FakeEmbedder:
        resolved.append(True)
        return FakeEmbedder()

    monkeypatch.setattr("mewbo_graph.wiki.embedder.Embedder.enabled", staticmethod(lambda: False))
    monkeypatch.setattr("mewbo_graph.wiki.embedder.make_embedder_or_none", _resolve)
    _seed(store)
    root = _write(tmp_path, "auth.py", "def verify(): ...  # changed")

    orch = RefreshOrchestrator.from_store(
        store, slug="acme/repo",
        parser=FakeParser({"auth.py": _reparse_one()}),
        clock=lambda: "2026-06-05T12:00:00Z",
    )
    orch.refresh(SLUG, root, [p for p in root.rglob("*") if p.is_file()], commit="c2")

    assert resolved == [], "the switch is off — nothing should have been constructed"
    assert store.vector_search(SLUG, [1.0, 0.0], k=100) == []


def test_scope_preview_keys(store, tmp_path) -> None:
    """The preview is a MODEL, and its wire aliases are what the console reads.

    Asserting on the serialised aliases rather than the Python attributes is the
    point: the same payload rides the SSE event and the job snapshot, so a
    renamed alias is a silently blank panel, not a validation error anyone sees.
    """
    _seed(store)
    root = _write(tmp_path, "auth.py", "changed")
    orch = _orchestrator(store, {"auth.py": GraphParseResult(nodes=[], edges=[], skipped=[])})
    files = [p for p in root.rglob("*") if p.is_file()]
    sp = orch.refresh(SLUG, root, files, commit="c2").scope_preview()
    assert isinstance(sp, ScopePreview)
    wire = sp.model_dump(by_alias=True)
    assert set(wire) == {
        "filesAdded", "filesModified", "filesDeleted", "earlyCutoffFiles",
        "affectedEntities", "memoryKept", "memoryInvalidated", "memoryRevalidated",
        "pagesKeep", "pagesEdit", "pagesRegenerate", "newPages", "llmCalls",
    }
    assert wire["filesModified"] == 1
