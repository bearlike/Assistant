"""WikiQueryGraphTool tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki import query_graph as query_graph_mod
from mewbo_graph.plugins.wiki.query_graph import WikiQueryGraphTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphEdge, GraphNode, IndexingJob


@pytest.fixture
def setup(tmp_path):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    job = IndexingJob(jobId="j1", slug="x/y", status="scanning",
                     scannedCount=0, totalCount=0, currentFile=None)
    store.create_job(job)
    store.attach_job_session("j1", "sess-1")
    store.upsert_nodes("x/y", [
        GraphNode(slug="x/y", node_id="f1", type="Function", name="auth",
                  file="a.py", range=(0, 100), docstring="check token"),
        GraphNode(slug="x/y", node_id="f2", type="Function", name="store",
                  file="a.py", range=(0, 100), docstring=None),
        GraphNode(slug="x/y", node_id="c1", type="Class", name="Engine",
                  file="b.py", range=(0, 100), docstring=None),
    ])
    store.upsert_edges("x/y", [
        GraphEdge(slug="x/y", source="f1", target="c1", type="CALLS"),
    ])
    return store, "sess-1"


def test_query_returns_all_when_no_filter(setup):
    store, sid = setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={})
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(step))
    content = str(result.content)
    assert "f1" in content and "f2" in content and "c1" in content


def test_query_filters_by_type(setup):
    store, sid = setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={"node_type": "Class"})
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(step))
    content = str(result.content)
    assert "Engine" in content
    assert "auth" not in content


def test_query_filters_by_name_match(setup):
    store, sid = setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={"name_match": "AUT"})  # case-insensitive
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(step))
    content = str(result.content)
    assert "auth" in content


def test_query_returns_neighbors(setup):
    store, sid = setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={"neighbors_of": "f1"})
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(step))
    content = str(result.content)
    assert "Engine" in content   # c1
    assert "auth" not in content # f1 itself not returned


def test_query_validates_unknown_kwarg(setup):
    store, sid = setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={"bogus": "value"})
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        import asyncio
        result = asyncio.run(tool.handle(step))
    content = str(result.content)
    assert "validation" in content
