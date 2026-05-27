"""Tests for WikiGraphNeighborsTool — wiki_graph_neighbors tool (graph_neighbors.py)."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphEdge, GraphNode, IndexingJob, QaAnswer

# ── Helpers ────────────────────────────────────────────────────────────────────

SLUG = "org/repo"


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _job(job_id: str = "job-gn", slug: str = SLUG) -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="scanning",
        scanned_count=0,
        total_count=0,
        current_file=None,
    )


def _qa(answer_id: str = "ans-gn", slug: str = SLUG) -> QaAnswer:
    return QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=[],
        model="test-model",
        blocks=[],
        slug=slug,
    )


def _node(node_id: str, name: str, ntype: str = "Function", file: str = "a.py") -> GraphNode:
    return GraphNode(slug=SLUG, node_id=node_id, type=ntype, name=name, file=file, range=(0, 100))


def _edge(source: str, target: str, etype: str = "CALLS") -> GraphEdge:
    return GraphEdge(slug=SLUG, source=source, target=target, type=etype)


def _seed_graph(store: JsonWikiStore) -> None:
    """Seed a small graph: f1 CALLS f2, f2 CALLS f3, cls1 CONTAINS f1."""
    store.upsert_nodes(
        SLUG,
        [
            _node("f1", "auth"),
            _node("f2", "verify"),
            _node("f3", "fetch_token"),
            _node("cls1", "AuthManager", "Class"),
        ],
    )
    store.upsert_edges(
        SLUG,
        [
            _edge("f1", "f2", "CALLS"),
            _edge("f2", "f3", "CALLS"),
            _edge("cls1", "f1", "CONTAINS"),
        ],
    )


def _run_neighbors(
    store: JsonWikiStore,
    session_id: str,
    tool_input: dict,
) -> MagicMock:
    from mewbo_graph.plugins.wiki.graph_neighbors import WikiGraphNeighbors, WikiGraphNeighborsTool

    runtime = _fake_runtime(store)
    tool = WikiGraphNeighborsTool(session_id=session_id)

    with patch.object(WikiGraphNeighbors, "_resolve_runtime", return_value=runtime):
        return asyncio.run(tool.handle(_make_action_step(tool_input)))


# ── Test 1: basic 1-hop any-direction ───────────────────────────────────────


def test_graph_neighbors_1hop_any_direction(tmp_path: Path) -> None:
    """1-hop traversal from f1 in any direction returns f2 and cls1."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn1"))
    store.attach_job_session("job-gn1", "sess-gn1")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn1", {"node_id": "f1"})

    assert "error" not in result.content
    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    # Seed node itself + direct neighbours
    assert "f1" in node_ids
    assert "f2" in node_ids  # f1 CALLS f2 (outgoing)
    assert "cls1" in node_ids  # cls1 CONTAINS f1 (incoming)
    # f3 is 2 hops away — must NOT appear with hops=1
    assert "f3" not in node_ids


# ── Test 2: out direction only ──────────────────────────────────────────────


def test_graph_neighbors_out_direction(tmp_path: Path) -> None:
    """direction='out' from cls1 yields only outgoing edges (cls1 CONTAINS f1)."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn2"))
    store.attach_job_session("job-gn2", "sess-gn2")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn2", {"node_id": "cls1", "direction": "out"})

    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f1" in node_ids
    # f2 is not directly connected from cls1 in "out" 1-hop
    assert "f2" not in node_ids


# ── Test 3: in direction only ────────────────────────────────────────────────


def test_graph_neighbors_in_direction(tmp_path: Path) -> None:
    """direction='in' from f2 returns who calls/targets f2 (f1 CALLS f2)."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn3"))
    store.attach_job_session("job-gn3", "sess-gn3")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn3", {"node_id": "f2", "direction": "in"})

    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f1" in node_ids
    # f3 is outgoing from f2 — must NOT appear with direction=in
    assert "f3" not in node_ids


# ── Test 4: edge_kind filter ─────────────────────────────────────────────────


def test_graph_neighbors_edge_kind_filter(tmp_path: Path) -> None:
    """edge_kind='CONTAINS' from cls1 returns only CONTAINS-connected nodes."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn4"))
    store.attach_job_session("job-gn4", "sess-gn4")
    _seed_graph(store)

    result = _run_neighbors(
        store, "sess-gn4", {"node_id": "cls1", "edge_kind": "CONTAINS", "direction": "out"}
    )

    payload = ast.literal_eval(result.content)
    # Edge list should only have CONTAINS edges
    edge_kinds = {e["kind"] for e in payload["edges"]}
    assert edge_kinds == {"CONTAINS"}
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f1" in node_ids


# ── Test 5: 2-hop traversal ──────────────────────────────────────────────────


def test_graph_neighbors_2hop(tmp_path: Path) -> None:
    """hops=2 from f1 in out direction reaches f3 (f1→f2→f3)."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn5"))
    store.attach_job_session("job-gn5", "sess-gn5")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn5", {"node_id": "f1", "direction": "out", "hops": 2})

    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f2" in node_ids
    assert "f3" in node_ids
    assert payload["hops_reached"] >= 2


# ── Test 6: limit truncates the result ──────────────────────────────────────


def test_graph_neighbors_limit_truncates(tmp_path: Path) -> None:
    """limit=1 from f1 (any direction) causes truncation."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn6"))
    store.attach_job_session("job-gn6", "sess-gn6")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn6", {"node_id": "f1", "limit": 1})

    payload = ast.literal_eval(result.content)
    # Seed node counts towards limit — with limit=1 we may truncate
    assert payload["truncated"] is True


# ── Test 7: isolated node returns just itself ────────────────────────────────


def test_graph_neighbors_isolated_node(tmp_path: Path) -> None:
    """A node with no edges returns just itself in the nodes list, no edges."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn7"))
    store.attach_job_session("job-gn7", "sess-gn7")
    # Only a single isolated node
    store.upsert_nodes(SLUG, [_node("solo", "standalone")])

    result = _run_neighbors(store, "sess-gn7", {"node_id": "solo"})

    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "solo" in node_ids
    assert payload["edges"] == []
    assert payload["truncated"] is False


# ── Test 8: session resolves via QA ctx too ──────────────────────────────────


def test_graph_neighbors_resolves_via_qa_ctx(tmp_path: Path) -> None:
    """Session attached to a QA answer (not a job) also resolves the graph."""
    store = _store(tmp_path)
    store.save_qa(_qa("ans-gn8", slug=SLUG))
    store.attach_qa_session("ans-gn8", "sess-gn8")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn8", {"node_id": "f1"})

    assert "error" not in result.content
    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f1" in node_ids


# ── Test 9: unknown session → for_session returns error (via direct call) ────


def test_graph_neighbors_for_session_unknown_returns_error(tmp_path: Path) -> None:
    """Session with no attached job or QA → for_session returns error MockSpeaker.

    BUG NOTE: WikiGraphNeighborsTool.handle() checks isinstance(view, dict)
    but for_session returns MockSpeaker (from _err_result). This means the
    tool handle would crash on AttributeError rather than return gracefully.
    We test for_session directly to cover the error branches.
    """
    from mewbo_graph.plugins.wiki.graph_neighbors import WikiGraphNeighbors

    store = _store(tmp_path)
    runtime = _fake_runtime(store)

    with patch.object(WikiGraphNeighbors, "_resolve_runtime", return_value=runtime):
        result = WikiGraphNeighbors.for_session("sess-unknown-gn")

    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "internal"


# ── Test 10: validation error on bad hops ───────────────────────────────────


def test_graph_neighbors_validation_error_on_bad_hops(tmp_path: Path) -> None:
    """hops > 3 (max) → validation error returned."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn10"))
    store.attach_job_session("job-gn10", "sess-gn10")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn10", {"node_id": "f1", "hops": 10})

    assert "error" in result.content
    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "validation"


# ── Test 11: node wire shape is correct ─────────────────────────────────────


def test_graph_neighbors_wire_shape(tmp_path: Path) -> None:
    """Each node in the response has the required wire fields."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn11"))
    store.attach_job_session("job-gn11", "sess-gn11")
    store.upsert_nodes(
        SLUG,
        [
            GraphNode(
                slug=SLUG,
                node_id="n1",
                type="Function",
                name="hello",
                file="lib.py",
                range=(10, 20),
                docstring="Says hello",
            ),
        ],
    )

    result = _run_neighbors(store, "sess-gn11", {"node_id": "n1"})

    payload = ast.literal_eval(result.content)
    assert len(payload["nodes"]) == 1
    node = payload["nodes"][0]
    assert node["node_id"] == "n1"
    assert node["name"] == "hello"
    assert node["type"] == "Function"
    assert node["file"] == "lib.py"
    assert "range" in node
    assert "docstring" in node


# ── Test 12: for_session returns error when runtime is None ─────────────────


def test_wiki_graph_neighbors_for_session_no_runtime(tmp_path: Path) -> None:
    """for_session with _resolve_runtime returning None yields an internal error.

    This covers the None-runtime branch in for_session, distinct from test 9
    which returns a real runtime but with no session attachment.
    """
    from mewbo_graph.plugins.wiki.graph_neighbors import WikiGraphNeighbors

    with patch.object(WikiGraphNeighbors, "_resolve_runtime", return_value=None):
        result = WikiGraphNeighbors.for_session("sess-no-runtime")

    payload = ast.literal_eval(result.content)
    assert payload["error"]["code"] == "internal"


# ── Test 13: edge_kind filter with no match returns seed node + no edges ─────


def test_graph_neighbors_edge_kind_no_match(tmp_path: Path) -> None:
    """edge_kind='EXTENDS' when no EXTENDS edges exist → nodes=[seed], edges=[]."""
    store = _store(tmp_path)
    store.create_job(_job("job-gn14"))
    store.attach_job_session("job-gn14", "sess-gn14")
    _seed_graph(store)

    result = _run_neighbors(store, "sess-gn14", {"node_id": "f1", "edge_kind": "EXTENDS"})

    payload = ast.literal_eval(result.content)
    # Only seed node present; no EXTENDS edges found
    assert payload["edges"] == []
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f1" in node_ids


# ── Test 14: BFS — already-visited neighbour skipped (line 159) ─────────────


def test_graph_neighbors_already_visited_not_duplicated(tmp_path: Path) -> None:
    """A node reachable via two paths is only included once in the result.

    Graph: f1→f2, f3→f2 (diamond).  With hops=1 starting from f1, f2 is
    the neighbour.  If somehow f2 also appears in the frontier (2-hop), the
    already-visited branch (line 159) fires and skips it.
    Use hops=2 so we exercise the depth_left queue exhaustion too.
    """
    from mewbo_graph.plugins.wiki.graph_neighbors import WikiGraphNeighbors

    store = _store(tmp_path)
    # Diamond: f1→f2, f1→f3, f3→f2 (f2 reachable in 2 ways)
    store.upsert_nodes(
        SLUG,
        [
            _node("f1", "root"),
            _node("f2", "shared"),
            _node("f3", "middle"),
        ],
    )
    store.upsert_edges(
        SLUG,
        [
            _edge("f1", "f2", "CALLS"),
            _edge("f1", "f3", "CALLS"),
            _edge("f3", "f2", "CALLS"),  # f2 reachable again via f3
        ],
    )

    gn = WikiGraphNeighbors(slug=SLUG, store=store)
    from mewbo_graph.plugins.wiki.graph_neighbors import WikiGraphNeighborsArgs

    result = gn.traverse(WikiGraphNeighborsArgs(node_id="f1", direction="out", hops=2))

    node_ids = [n["node_id"] for n in result["nodes"]]
    # f2 must appear only once
    assert node_ids.count("f2") == 1
    # All three reachable nodes are present
    assert set(node_ids) == {"f1", "f2", "f3"}


# ── Test 15: BFS depth_left=0 skips further traversal ───────────────────────


def test_graph_neighbors_bfs_depth_exhausted(tmp_path: Path) -> None:
    """With hops=1, depth_left reaches 0 before we go deeper (line 154).

    A node at depth 2 must NOT appear with hops=1 even in 'any' direction.
    """
    store = _store(tmp_path)
    store.create_job(_job("job-gn16"))
    store.attach_job_session("job-gn16", "sess-gn16")
    # Chain: f1→f2→f3
    store.upsert_nodes(
        SLUG,
        [
            _node("f1", "root"),
            _node("f2", "mid"),
            _node("f3", "deep"),
        ],
    )
    store.upsert_edges(
        SLUG,
        [
            _edge("f1", "f2", "CALLS"),
            _edge("f2", "f3", "CALLS"),
        ],
    )

    result = _run_neighbors(store, "sess-gn16", {"node_id": "f1", "hops": 1, "direction": "out"})

    payload = ast.literal_eval(result.content)
    node_ids = {n["node_id"] for n in payload["nodes"]}
    assert "f3" not in node_ids  # depth_left=0 path fires; f3 is skipped
    assert "f2" in node_ids
