"""WikiQueryGraphTool tests."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki import query_graph as query_graph_mod
from mewbo_graph.plugins.wiki.query_graph import WikiQueryGraphTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphEdge, IndexingJob, QaAnswer, make_graph_node


@pytest.fixture
def setup(tmp_path):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    job = IndexingJob(jobId="j1", slug="x/y", status="scanning",
                     scannedCount=0, totalCount=0, currentFile=None)
    store.create_job(job)
    store.attach_job_session("j1", "sess-1")
    store.upsert_nodes("x/y", [
        make_graph_node(slug="x/y", node_id="f1", type="Function", name="auth",
                  file="a.py", range=(0, 100), docstring="check token"),
        make_graph_node(slug="x/y", node_id="f2", type="Function", name="store",
                  file="a.py", range=(0, 100), docstring=None),
        make_graph_node(slug="x/y", node_id="c1", type="Class", name="Engine",
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


# ── Access-trail recording — navigation is not grounding ────────────────


@pytest.fixture
def qa_setup(tmp_path):
    """A QA session over a seed node with 50 neighbours (an unbounded record floods)."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.save_qa(QaAnswer(answerId="qa1", fromPageId="overview", summarySources=[],
                           model="m", blocks=[], slug="x/y"))
    store.attach_qa_session("qa1", "sess-qa")
    nodes = [make_graph_node(slug="x/y", node_id="S", type="Function", name="seed",
                       file="a.py", range=(0, 1))]
    edges = []
    for i in range(50):
        nodes.append(make_graph_node(slug="x/y", node_id=f"n{i}", type="Function",
                               name=f"f{i}", file="a.py", range=(0, 1)))
        edges.append(GraphEdge(slug="x/y", source="S", target=f"n{i}", type="CALLS"))
    store.upsert_nodes("x/y", nodes)
    store.upsert_edges("x/y", edges)
    return store, "sess-qa"


def test_query_graph_records_only_seed_not_full_result(qa_setup):
    """A neighbours query records ONLY the seed node, not the ~50-node walk."""
    store, sid = qa_setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={"neighbors_of": "S", "limit": 50})
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(step))

    access = [e for e in store.load_qa_events("qa1") if e["type"] == "access"]
    assert len(access) == 1
    recs = access[0]["records"]
    assert len(recs) == 1  # the seed, NOT the 50 neighbours
    assert recs[0]["ref"] == "graph:S"
    assert recs[0]["score"] is None and recs[0]["op"] == "nav"


def test_query_graph_filter_records_nothing(qa_setup):
    """A plain type/name filter is navigation with no entry node — records nothing."""
    store, sid = qa_setup
    runtime = MagicMock(wiki_store=store)
    tool = WikiQueryGraphTool(session_id=sid)
    step = MagicMock(tool_input={"node_type": "Function", "limit": 50})
    with patch.object(query_graph_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(tool.handle(step))

    assert [e for e in store.load_qa_events("qa1") if e["type"] == "access"] == []


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
