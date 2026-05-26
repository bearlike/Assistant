"""WikiBuildGraphTool tests."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import IndexingJob
from mewbo_core.builtin_plugins.wiki import build_graph as build_graph_mod
from mewbo_core.builtin_plugins.wiki.build_graph import WikiBuildGraphTool

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_python_repo"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    # Create the job + attach the session.
    job = IndexingJob(jobId="j1", slug="x/y", status="scanning",
                     scannedCount=0, totalCount=0, currentFile=None)
    store.create_job(job)
    store.attach_job_session("j1", "sess-1")
    # Stash the submission so finalize can read it later (irrelevant here).
    store.save_job_submission("j1", {"slug": "x/y", "language": "en", "model": "x"})
    # Copy fixture into the clone dir.
    clone_dir = tmp_path / "clones" / "j1"
    clone_dir.mkdir(parents=True)
    for src in FIXTURE.iterdir():
        if src.is_file():
            (clone_dir / src.name).write_bytes(src.read_bytes())
    return store, "sess-1", clone_dir


def _stub_runtime(store):
    return MagicMock(wiki_store=store)


def test_build_graph_persists_nodes_edges_and_embeddings(setup):
    store, session_id, _clone_dir = setup
    runtime = _stub_runtime(store)
    fake_embedder = MagicMock()
    fake_embedder.embed_nodes.return_value = []  # noop for this test

    tool = WikiBuildGraphTool(session_id=session_id)
    fake_step = MagicMock(tool_input={})
    with patch.object(build_graph_mod, "_resolve_runtime", return_value=runtime), \
         patch.object(build_graph_mod, "_make_embedder", return_value=fake_embedder):
        import asyncio
        result = asyncio.run(tool.handle(fake_step))
    # nodes/edges persisted
    nodes = store.query_graph("x/y")
    assert len(nodes) > 0
    # result content includes counts and language list
    body = str(result.content)
    assert "nodeCount" in body
    assert "edgeCount" in body
    assert "languages" in body
    assert "python" in body


def test_build_graph_emits_embeddings_when_enabled(setup, monkeypatch):
    store, session_id, _ = setup
    runtime = _stub_runtime(store)
    # Stub embedder so it produces one Embedding per node.
    from mewbo_api.wiki.types import Embedding
    class _StubEmbedder:
        model = "stub-model"

        def embed_nodes(self, items, slug=""):
            return [Embedding(slug=slug, node_id=nid, vector=[0.1, 0.2, 0.3], model="m", dim=3)
                    for nid, _ in items]

    tool = WikiBuildGraphTool(session_id=session_id)
    fake_step = MagicMock(tool_input={})
    with patch.object(build_graph_mod, "_resolve_runtime", return_value=runtime), \
         patch.object(build_graph_mod, "_make_embedder", return_value=_StubEmbedder()):
        import asyncio
        asyncio.run(tool.handle(fake_step))

    # Embeddings persisted
    hits = store.vector_search("x/y", qvec=[0.1, 0.2, 0.3], k=5)
    assert len(hits) > 0


def test_build_graph_unknown_session_returns_internal_error():
    tool = WikiBuildGraphTool(session_id="sess-unknown")
    fake_step = MagicMock(tool_input={})
    shutil.rmtree("/tmp/build-graph-unknown-test", ignore_errors=True)
    runtime = MagicMock(wiki_store=JsonWikiStore(root_dir="/tmp/build-graph-unknown-test-2"))
    with patch.object(build_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(fake_step))
    assert "internal" in str(result.content)


def test_build_graph_handles_embedding_disabled(setup, monkeypatch):
    """When wiki.embedding.enabled is False, skip embeddings but still build graph."""
    store, session_id, _ = setup
    runtime = _stub_runtime(store)
    monkeypatch.setattr(build_graph_mod, "_embeddings_enabled", lambda: False)

    tool = WikiBuildGraphTool(session_id=session_id)
    fake_step = MagicMock(tool_input={})
    with patch.object(build_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(fake_step))
    # nodes/edges persisted
    nodes = store.query_graph("x/y")
    assert len(nodes) > 0
    body = str(result.content)
    assert "embeddedCount\": 0" in body or "'embeddedCount': 0" in body
