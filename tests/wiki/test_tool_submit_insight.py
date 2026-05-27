"""Tests for WikiSubmitInsightTool — the in-session insight write surface."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphNode, IndexingJob, QaAnswer

SLUG = "org/repo"


def _gn(nid: str, typ: str, name: str) -> GraphNode:
    return GraphNode(slug=SLUG, node_id=nid, type=typ, name=name, file="auth.py", range=(0, 9))


def _store(tmp_path: Path) -> JsonWikiStore:
    s = JsonWikiStore(root_dir=tmp_path)
    s.upsert_nodes(SLUG, [_gn("cA", "Class", "AuthService"), _gn("fV", "Function", "verify")])
    return s


def _job_runtime(store: JsonWikiStore, job_id="job-i", sess="sess-i") -> SimpleNamespace:
    job = IndexingJob(
        job_id=job_id, slug=SLUG, status="finalizing",
        scanned_count=1, total_count=1, current_file=None,
    )
    store.create_job(job)
    store.attach_job_session(job_id, sess)
    return SimpleNamespace(wiki_store=store)


def _qa_runtime(store: JsonWikiStore, ans_id="ans-i", sess="sess-q") -> SimpleNamespace:
    ans = QaAnswer(
        answer_id=ans_id, from_page_id="overview", summary_sources=[],
        model="m", blocks=[], slug=SLUG,
    )
    store.save_qa(ans)
    store.attach_qa_session(ans_id, sess)
    return SimpleNamespace(wiki_store=store)


def _step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _run(tool, tool_input, runtime):
    import mewbo_graph.plugins.wiki.submit_insight as mod

    with patch.object(mod, "_resolve_runtime", return_value=runtime), patch(
        "mewbo_graph.wiki.embedder.make_embedder_or_none", return_value=None
    ):
        return asyncio.run(tool.handle(_step(tool_input)))


# ── tests ────────────────────────────────────────────────────────────────────


def test_submit_insight_persists_memory_with_anchor(tmp_path: Path) -> None:
    from mewbo_graph.plugins.wiki.submit_insight import WikiSubmitInsightTool

    store = _store(tmp_path)
    runtime = _job_runtime(store)
    tool = WikiSubmitInsightTool(session_id="sess-i")
    res = _run(
        tool,
        {"content": "AuthService verifies bearer tokens", "anchors": ["auth.py#AuthService"]},
        runtime,
    )
    payload = json.loads(res.content)
    assert payload["ok"] is True
    claim = payload["claims"][0]
    assert claim["action"] == "created"
    node = store.get_memory_node(SLUG, claim["node_id"])
    assert node is not None
    assert node.provenance.source == "indexer"
    edges = store.list_memory_edges(SLUG, node_id=node.node_id)
    anchors = [e.target for e in edges if e.type == "ANCHORS"]
    assert anchors == ["auth.py#AuthService"]


def test_submit_insight_qa_ctx_sets_source_qa(tmp_path: Path) -> None:
    from mewbo_graph.plugins.wiki.submit_insight import WikiSubmitInsightTool

    store = _store(tmp_path)
    runtime = _qa_runtime(store)
    tool = WikiSubmitInsightTool(session_id="sess-q")
    res = _run(
        tool, {"content": "Verify checks the signature", "anchors": ["auth.py#verify"]}, runtime
    )
    claim = json.loads(res.content)["claims"][0]
    node = store.get_memory_node(SLUG, claim["node_id"])
    assert node is not None
    assert node.provenance.source == "qa"


def test_submit_insight_drops_unresolved_anchor(tmp_path: Path) -> None:
    from mewbo_graph.plugins.wiki.submit_insight import WikiSubmitInsightTool

    store = _store(tmp_path)
    runtime = _job_runtime(store)
    tool = WikiSubmitInsightTool(session_id="sess-i")
    res = _run(tool, {"content": "claim", "anchors": ["ghost.py#Nope"]}, runtime)
    claim = json.loads(res.content)["claims"][0]
    assert claim["anchors"] == []
    assert any("ghost.py#Nope" in w for w in claim["warnings"])


def test_submit_insight_rejects_overlong_content(tmp_path: Path) -> None:
    from mewbo_graph.plugins.wiki.submit_insight import WikiSubmitInsightTool

    store = _store(tmp_path)
    runtime = _job_runtime(store)
    tool = WikiSubmitInsightTool(session_id="sess-i")
    res = _run(tool, {"content": "x" * 250}, runtime)
    assert "validation" in res.content
    assert store.query_memory(SLUG) == []


def test_submit_insight_disabled_flag(tmp_path: Path) -> None:
    import mewbo_graph.plugins.wiki.submit_insight as mod
    from mewbo_graph.plugins.wiki.submit_insight import WikiSubmitInsightTool

    store = _store(tmp_path)
    runtime = _job_runtime(store)
    tool = WikiSubmitInsightTool(session_id="sess-i")
    with patch.object(mod, "_resolve_runtime", return_value=runtime), patch.object(
        mod, "_memory_enabled", return_value=False
    ):
        res = asyncio.run(tool.handle(_step({"content": "claim"})))
    assert "disabled" in res.content
    assert store.query_memory(SLUG) == []


def test_submit_insight_no_ctx_errors(tmp_path: Path) -> None:
    from mewbo_graph.plugins.wiki.submit_insight import WikiSubmitInsightTool

    store = _store(tmp_path)
    runtime = SimpleNamespace(wiki_store=store)  # no job/qa session attached
    tool = WikiSubmitInsightTool(session_id="unknown-sess")
    res = _run(tool, {"content": "claim"}, runtime)
    assert "error" in res.content
