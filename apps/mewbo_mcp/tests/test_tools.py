"""Unit tests for the MCP tool implementations.

The HTTP boundary is stubbed via an injected ``httpx.MockTransport``
(``FakeRest`` in conftest). Each test asserts both the outbound REST
path/method/body AND the shaping of the returned MCP payload — no live
server is started.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mewbo_mcp import tools
from mewbo_mcp.rest import RestError


def run(coro):
    """Run an async tool body in a fresh event loop (repo convention)."""
    return asyncio.run(coro)


def new_fake(fake_rest):
    """Return a fresh FakeRest instance of the same type as the fixture."""
    return type(fake_rest)()


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


def test_create_session_auto_provisions_worktree(fake_rest):
    """Default path: read base branch → create-from-base worktree → session → query.

    Fix 2: worktree IS still provisioned server-side, but the caller receives
    only the minimal {session_id, status} shape (no worktree ids).
    """
    fake = (
        fake_rest
        .on("GET", "/api/v_projects/Assistant/branches", {"current_branch": "main"})
        .on(
            "POST",
            "/api/v_projects/Assistant/worktrees",
            {"project_id": "wt:1", "branch": "mewbo/add-x"},
            status=201,
        )
        .on("POST", "/api/sessions", {"session_id": "s1"})
        .on("POST", "/api/sessions/s1/query", {"accepted": True}, status=202)
    )
    result = run(
        tools.SessionTools(fake.client()).create(prompt="add x", repo="Assistant", title="add x")
    )
    assert result["session_id"] == "s1"
    assert result["status"] == "running"
    # Fix 2: worktree ids are NOT surfaced to the caller (system-owned lifecycle)
    assert "worktree_project_id" not in result
    assert "parent_project_id" not in result

    # create-from-base used the discovered base branch
    wt_req = fake.find("POST", "/api/v_projects/Assistant/worktrees")
    assert wt_req.json["base"] == "main"
    assert wt_req.json["branch"].startswith("mewbo/")

    # session pinned the managed worktree
    sess_req = fake.find("POST", "/api/sessions")
    assert sess_req.json["project"] == "managed:wt:1"

    # query carried the prompt
    q_req = fake.find("POST", "/api/sessions/s1/query")
    assert q_req.json["query"] == "add x"


def test_create_session_existing_branch_no_base(fake_rest):
    """When ``branch`` is given, target it (worktree without a base)."""
    fake = (
        fake_rest
        .on(
            "POST",
            "/api/v_projects/Assistant/worktrees",
            {"project_id": "wt:9", "branch": "feature/y"},
            status=201,
        )
        .on("POST", "/api/sessions", {"session_id": "s2"})
        .on("POST", "/api/sessions/s2/query", {"accepted": True}, status=202)
    )
    result = run(
        tools.SessionTools(fake.client()).create(
            prompt="work on y", repo="Assistant", branch="feature/y"
        )
    )
    assert result["session_id"] == "s2"
    wt_req = fake.find("POST", "/api/v_projects/Assistant/worktrees")
    assert wt_req.json == {"branch": "feature/y"}  # no base → existing branch
    # branches endpoint should NOT have been consulted
    assert "GET /api/v_projects/Assistant/branches" not in fake.paths()


def test_create_session_existing_worktree_pins_managed_id(fake_rest):
    """When ``worktree`` is given, pin it directly without creating one."""
    fake = (
        fake_rest
        .on("POST", "/api/sessions", {"session_id": "s3"})
        .on("POST", "/api/sessions/s3/query", {"accepted": True}, status=202)
    )
    result = run(
        tools.SessionTools(fake.client()).create(
            prompt="resume", repo="Assistant", worktree="abc123"
        )
    )
    assert result["session_id"] == "s3"
    assert "POST /api/v_projects/Assistant/worktrees" not in fake.paths()
    sess_req = fake.find("POST", "/api/sessions")
    assert sess_req.json["project"] == "managed:abc123"


def test_create_session_integrations_become_mcp_tools_allowlist(fake_rest):
    """``integrations`` maps to context.mcp_tools on both session + query."""
    fake = (
        fake_rest
        .on("POST", "/api/sessions", {"session_id": "s4"})
        .on("POST", "/api/sessions/s4/query", {"accepted": True}, status=202)
    )
    run(
        tools.SessionTools(fake.client()).create(
            prompt="go",
            integrations=["shell", "file_edit"],
            mode="plan",
        )
    )
    sess_req = fake.find("POST", "/api/sessions")
    assert sess_req.json["context"]["mcp_tools"] == ["shell", "file_edit"]
    q_req = fake.find("POST", "/api/sessions/s4/query")
    assert q_req.json["context"]["mcp_tools"] == ["shell", "file_edit"]
    assert q_req.json["mode"] == "plan"


def test_create_session_forwards_token_as_x_api_key(fake_rest):
    """The caller's token is forwarded verbatim as X-API-Key on every call."""
    fake = (
        fake_rest
        .on("POST", "/api/sessions", {"session_id": "s5"})
        .on("POST", "/api/sessions/s5/query", {"accepted": True}, status=202)
    )
    run(tools.SessionTools(fake.client(token="mk_secret")).create(prompt="hi"))
    for req in fake.requests:
        assert req.headers.get("x-api-key") == "mk_secret"
        assert "authorization" not in req.headers  # we never resend the bearer


def test_create_session_missing_session_id_raises(fake_rest):
    """A session POST without a session_id is a hard error."""
    fake = fake_rest.on("POST", "/api/sessions", {})
    with pytest.raises(RestError):
        run(tools.SessionTools(fake.client()).create(prompt="hi"))


def test_create_session_single_tag_applied(fake_rest):
    """A single tag is forwarded as session_tag on the create POST."""
    fake = (
        fake_rest
        .on("POST", "/api/sessions", {"session_id": "s6"})
        .on("POST", "/api/sessions/s6/query", {"accepted": True}, status=202)
    )
    run(tools.SessionTools(fake.client()).create(prompt="hi", tag="mine"))
    assert fake.find("POST", "/api/sessions").json["session_tag"] == "mine"


def test_create_session_idempotency_key_sets_session_tag(fake_rest):
    """idempotency_key tags the session so a retry is identifiable/reapable."""
    fake = (
        fake_rest
        .on("POST", "/api/sessions", {"session_id": "s6b"})
        .on("POST", "/api/sessions/s6b/query", {"accepted": True}, status=202)
    )
    run(tools.SessionTools(fake.client()).create(prompt="hi", idempotency_key="run-42"))
    assert fake.find("POST", "/api/sessions").json["session_tag"] == "run-42"


# ---------------------------------------------------------------------------
# send_followup / interrupt
# ---------------------------------------------------------------------------


def test_send_followup_posts_message(fake_rest):
    fake = fake_rest.on("POST", "/api/sessions/s1/message", {"enqueued": True}, status=202)
    result = run(
        tools.SessionTools(fake.client()).send_followup(session_id="s1", message="also do z")
    )
    assert result == {"session_id": "s1", "status": "enqueued"}
    assert fake.find("POST", "/api/sessions/s1/message").json == {"text": "also do z"}


def test_interrupt_session_posts_interrupt(fake_rest):
    fake = fake_rest.on("POST", "/api/sessions/s1/interrupt", {"interrupted": True}, status=202)
    result = run(tools.SessionTools(fake.client()).interrupt(session_id="s1"))
    assert result == {"session_id": "s1", "status": "interrupted"}


# ---------------------------------------------------------------------------
# list_sessions (client-side filters)
# ---------------------------------------------------------------------------


def _sessions_payload():
    # Mirrors the real GET /api/sessions shape (summarize_session): each entry
    # carries a ``context`` dict. Session "a" is a plain config-project session
    # (context.project == raw name). Session "w" is a worktree session created
    # via create_session: the API stores context.project as "managed:<id>" and
    # context.repo as the parent repo name (see backend _populate_worktree_context).
    return {
        "sessions": [
            {
                "session_id": "a",
                "status": "completed",
                "created_at": "2026-06-01",
                "context": {"project": "Assistant"},
            },
            {
                "session_id": "b",
                "status": "running",
                "created_at": "2026-06-05",
                "context": {"project": "Other"},
            },
            {
                "session_id": "w",
                "status": "running",
                "created_at": "2026-06-04",
                "context": {
                    "project": "managed:wt:abc:mewbo-add-x",
                    "repo": "Assistant",
                    "branch": "mewbo/add-x",
                },
            },
        ]
    }


def test_list_sessions_filters_by_status_project_since(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions", _sessions_payload())
    by_status = run(tools.SessionTools(fake.client()).list_sessions(status="running"))
    assert {s["session_id"] for s in by_status["sessions"]} == {"b", "w"}

    fake3 = new_fake(fake_rest).on("GET", "/api/sessions", _sessions_payload())
    by_since = run(tools.SessionTools(fake3.client()).list_sessions(since="2026-06-03"))
    assert {s["session_id"] for s in by_since["sessions"]} == {"b", "w"}


def test_list_sessions_project_matches_config_and_worktree(fake_rest):
    """project= matches both a plain config session AND a worktree session.

    The worktree session stores context.project="managed:..." + context.repo=
    "Assistant"; filtering by "Assistant" must catch it via context.repo, not
    miss it because context.project is the managed ref.
    """
    fake = fake_rest.on("GET", "/api/sessions", _sessions_payload())
    out = run(tools.SessionTools(fake.client()).list_sessions(project="Assistant"))
    assert {s["session_id"] for s in out["sessions"]} == {"a", "w"}

    fake2 = new_fake(fake_rest).on("GET", "/api/sessions", _sessions_payload())
    other = run(tools.SessionTools(fake2.client()).list_sessions(project="Other"))
    assert {s["session_id"] for s in other["sessions"]} == {"b"}


# ---------------------------------------------------------------------------
# get_session_history — the four tiers
# ---------------------------------------------------------------------------


def _history_events():
    return {
        "running": False,
        "events": [
            {"type": "user", "ts": "t0", "payload": {"text": "q1"}},
            {
                "type": "tool_result",
                "ts": "t1",
                "payload": {
                    "tool_id": "shell",
                    "operation": "get",
                    "summary": "ran ls",
                    "success": True,
                    "tool_input": {"cmd": "ls"},
                    "result": "file1\nfile2",
                },
            },
            {
                "type": "llm_call_end",
                "ts": "t2",
                "payload": {"depth": 0, "input_tokens": 100, "output_tokens": 12},
            },
            {"type": "assistant", "ts": "t3", "payload": {"text": "a1"}},
            {"type": "user", "ts": "t4", "payload": {"text": "q2"}},
            {"type": "completion", "ts": "t5", "payload": {"done_reason": "stop"}},
        ],
    }


def test_history_overview(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _history_events())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="overview"))
    assert out["turn_count"] == 2
    assert out["step_count"] == 1
    assert out["total_input_tokens"] == 100
    assert out["total_output_tokens"] == 12
    assert out["running"] is False
    # Title comes from the FIRST turn's user text (stable as the session grows);
    # the last turn here is a bare completion so its assistant_text is empty.
    assert out["title"] == "q1"
    assert out["summary"] == ""


def test_history_turns(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _history_events())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="turns"))
    assert [t["index"] for t in out["turns"]] == [1, 2]
    assert out["turns"][0]["user_text"] == "q1"
    assert out["turns"][0]["assistant_text"] == "a1"
    assert out["turns"][0]["step_count"] == 1
    assert out["turns"][1]["done_reason"] == "stop"


def test_history_steps_requires_turn_and_omits_full_result(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _history_events())
    out = run(
        tools.SessionTools(fake.client()).history(session_id="s1", level="steps", turn=1)
    )
    assert out["turn"] == 1
    assert len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["tool_id"] == "shell"
    assert step["summary"] == "ran ls"
    assert "result" not in step  # steps tier never returns full results


def test_history_full_includes_result_and_agents(fake_rest):
    fake = (
        fake_rest
        .on("GET", "/api/sessions/s1/events", _history_events())
        .on("GET", "/api/sessions/s1/agents", {"agents": [], "running": False})
    )
    out = run(
        tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1)
    )
    assert out["steps"][0]["result"] == "file1\nfile2"
    assert out["steps"][0]["tool_input"] == {"cmd": "ls"}
    # full tier references the agent tree rather than inlining it (no extra fetch)
    assert "get_agent_tree" in out["agents"]
    assert "GET /api/sessions/s1/agents" not in fake.paths()


def _fat_turn_events(n_steps: int, result_len: int):
    """One turn with ``n_steps`` tool_result steps; the first carries a fat result."""
    events: list = [{"type": "user", "ts": "t0", "payload": {"text": "go"}}]
    for i in range(n_steps):
        events.append(
            {
                "type": "tool_result",
                "ts": f"s{i}",
                "payload": {
                    "tool_id": "shell",
                    "operation": "run",
                    "summary": f"step {i}",
                    "success": True,
                    "tool_input": {"i": i},
                    "result": ("X" * result_len) if i == 0 else "ok",
                },
            }
        )
    events.append({"type": "assistant", "ts": "tA", "payload": {"text": "done"}})
    return {"running": False, "events": events}


def test_history_full_pages_steps_and_truncates_fat_result(fake_rest):
    """#42: the full tier caps steps to FULL_STEPS_PAGE and trims a fat field.

    A turn with 25 steps + one 10k-char result must return at most 20 steps,
    ``next_step_offset:20``, the true ``step_count:25``, and the giant result
    truncated to ≤ STEP_FIELD_TRUNC + the marker.
    """
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _fat_turn_events(25, 10_000))
    out = run(
        tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1)
    )
    assert out["step_count"] == 25  # true total, not the page size
    assert out["step_offset"] == 0
    assert out["next_step_offset"] == 20  # more steps remain
    assert len(out["steps"]) == 20  # capped to one page
    # The 10k result is trimmed by the MCP backstop (API didn't run here).
    fat = out["steps"][0]["result"]
    assert isinstance(fat, str)
    assert "truncated, 10000 chars" in fat
    assert len(fat) <= tools.SessionTools.STEP_FIELD_TRUNC + 40
    # The events GET opts into the API's field-capping via ?truncate=1.
    assert fake.find("GET", "/api/sessions/s1/events").params.get("truncate") == "1"


def test_history_full_second_page_has_no_next_offset(fake_rest):
    """#42: paging from step_offset returns the tail and omits next_step_offset."""
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _fat_turn_events(25, 10))
    out = run(
        tools.SessionTools(fake.client()).history(
            session_id="s1", level="full", turn=1, step_offset=20
        )
    )
    assert out["step_offset"] == 20
    assert len(out["steps"]) == 5  # steps 20..24
    assert "next_step_offset" not in out  # no more steps after this page


def test_history_invalid_level_raises(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _history_events())
    with pytest.raises(ValueError):
        run(tools.SessionTools(fake.client()).history(session_id="s1", level="bogus"))


def test_history_steps_without_turn_raises(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _history_events())
    with pytest.raises(ValueError):
        run(tools.SessionTools(fake.client()).history(session_id="s1", level="steps"))


def test_history_turn_out_of_range_raises(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _history_events())
    with pytest.raises(ValueError):
        run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=99))


# ---------------------------------------------------------------------------
# wiki tools
# ---------------------------------------------------------------------------


def test_list_wiki_projects(fake_rest):
    fake = fake_rest.on("GET", "/v1/wiki/projects", [{"slug": "assistant"}])
    out = run(tools.WikiTools(fake.client()).list_projects())
    assert out == [{"slug": "assistant"}]


def test_read_wiki_page(fake_rest):
    fake = fake_rest.on(
        "GET", "/v1/wiki/projects/assistant/pages/intro", {"id": "intro", "body": "# Hi"}
    )
    out = run(tools.WikiTools(fake.client()).read_page(project="assistant", page_id="intro"))
    assert out["body"] == "# Hi"


def test_read_wiki_structure(fake_rest):
    """read_wiki_structure defaults to compact stats; detail=full returns nodes+edges."""
    graph = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b"}]}
    fake = fake_rest.on("GET", "/v1/wiki/projects/assistant/graph", graph)
    # default tier = stats — never the full (potentially hundreds-of-KB) dump
    out = run(tools.WikiTools(fake.client()).read_structure(project="assistant"))
    assert out["stats"]["nodeCount"] == 2
    assert out["stats"]["edgeCount"] == 1
    assert "nodes" not in out
    # full tier returns the node list + the edges among them
    full = run(tools.WikiTools(fake.client()).read_structure(project="assistant", detail="full"))
    assert full["node_count"] == 2
    assert full["edges"] == [{"source": "a", "target": "b"}]
    fake.find("GET", "/v1/wiki/projects/assistant/graph")  # path/method asserted


def test_read_wiki_structure_cytoscape_layer_filter(fake_rest):
    """#63: nodes/edges are Cytoscape-shaped — ``layer`` lives under ``data``.

    The layer filter must read ``n['data']['layer']`` (not top-level), and the
    ``full`` tier must resolve edges via ``data.source``/``data.target``.
    """
    graph = {
        "nodes": [
            {"data": {"id": "code1", "layer": "code"}},
            {"data": {"id": "ent1", "layer": "entity"}},
        ],
        "edges": [{"data": {"id": "e1", "source": "code1", "target": "ent1"}}],
    }
    fake = fake_rest.on("GET", "/v1/wiki/projects/assistant/graph", graph)
    # layer="entity" keeps exactly the entity node (its layer is under data).
    nodes_out = run(
        tools.WikiTools(fake.client()).read_structure(
            project="assistant", detail="nodes", layer="entity"
        )
    )
    assert nodes_out["node_count"] == 1
    assert nodes_out["nodes"] == [{"data": {"id": "ent1", "layer": "entity"}}]
    # full tier resolves the edge via data.source/target — its endpoints are kept.
    full = run(tools.WikiTools(fake.client()).read_structure(project="assistant", detail="full"))
    assert full["node_count"] == 2
    assert full["edge_count"] == 1
    assert full["edges"] == [{"data": {"id": "e1", "source": "code1", "target": "ent1"}}]
    # derived per-layer stats read the layer from under data too.
    assert full["stats"]["perLayer"] == {"code": 1, "entity": 1}


def test_submit_insight_condense_posts_raw(fake_rest):
    """condense=True sends the text as `raw` so the server decomposes it."""
    result = {"ok": True, "claims": [{"action": "created", "node_id": "n1", "content": "c"}]}
    fake = fake_rest.on("POST", "/v1/wiki/projects/assistant/insights", result, status=201)
    out = run(
        tools.WikiTools(fake.client()).submit_insight(
            project="assistant", insight="auth notes",
            anchors=["auth.py#AuthService"], labels=["auth"],
        )
    )
    assert out["ok"] is True
    req = fake.find("POST", "/v1/wiki/projects/assistant/insights")
    assert req.json["raw"] == "auth notes"
    assert "content" not in req.json
    assert req.json["condense"] is True
    assert req.json["anchors"] == ["auth.py#AuthService"]
    assert req.json["labels"] == ["auth"]


def test_submit_insight_no_condense_posts_content(fake_rest):
    """condense=False sends the text verbatim as a single `content` claim."""
    result = {"ok": True, "claims": [{"action": "created", "node_id": "n1", "content": "c"}]}
    fake = fake_rest.on("POST", "/v1/wiki/projects/assistant/insights", result, status=201)
    run(
        tools.WikiTools(fake.client()).submit_insight(
            project="assistant", insight="AuthService verifies tokens",
            condense=False,
        )
    )
    req = fake.find("POST", "/v1/wiki/projects/assistant/insights")
    assert req.json["content"] == "AuthService verifies tokens"
    assert "raw" not in req.json
    assert req.json["condense"] is False


def test_submit_insight_forwards_token(fake_rest):
    result = {"ok": True, "claims": []}
    fake = fake_rest.on("POST", "/v1/wiki/projects/assistant/insights", result)
    run(
        tools.WikiTools(fake.client(token="mk_caller")).submit_insight(
            project="assistant", insight="x"
        )
    )
    req = fake.find("POST", "/v1/wiki/projects/assistant/insights")
    assert req.headers.get("x-api-key") == "mk_caller"


def test_get_agent_tree(fake_rest):
    """get_agent_tree returns the sub-agent tree dict for the session."""
    tree = {"agents": [{"agent_id": "x", "status": "running"}], "running": True, "total_steps": 3}
    fake = fake_rest.on("GET", "/api/sessions/s1/agents", tree)
    out = run(tools.SessionTools(fake.client()).agent_tree(session_id="s1"))
    assert out == tree
    fake.find("GET", "/api/sessions/s1/agents")


# -- ask_wiki: SSE-streamed start (real endpoint shape) + blocks snapshot -----


def _qa_meta_sse(answer_id: str) -> str:
    """Build a QA-start SSE body matching the real endpoint.

    The real route yields a ``_SSE_PRIMER`` comment frame, then ``_to_sse``
    frames: ``id: <idx>\\nevent: <type>\\ndata: <json>\\n\\n``. The first real
    event is ``meta`` carrying ``answerId``.
    """
    primer = ":" + (" " * 16) + "\n\n"
    meta = (
        "id: 0\n"
        "event: meta\n"
        f'data: {{"answerId": "{answer_id}", "model": "m", "fromPageId": ""}}\n\n'
    )
    # A following block_open frame the parser must NOT need to consume.
    extra = 'id: 1\nevent: block_open\ndata: {"index": 0}\n\n'
    return primer + meta + extra


def _sequence_poller(sequence):
    """Return a snapshot handler walking *sequence* plus its read counter.

    Each call returns the next snapshot (clamping at the last), so a test can
    model an incrementally-filling QA snapshot. ``poll_state["n"]`` records how
    many reads happened, letting tests assert the poll/stability behavior.
    """
    poll_state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        idx = min(poll_state["n"], len(sequence) - 1)
        poll_state["n"] += 1
        return httpx.Response(200, json=sequence[idx])

    return handler, poll_state


def test_ask_wiki_resets_event_state_between_frames(fake_rest):
    """A non-meta data frame before the meta frame must not yield a bad answerId.

    Exercises the blank-line frame reset (fix: a blank line terminates an SSE
    frame, so a prior ``event:`` does not bleed into the next ``data:``). Here a
    ``summary_ready`` frame with no answerId precedes the real ``meta`` frame.
    """
    primer = ":" + (" " * 8) + "\n\n"
    summary = "id: 0\nevent: summary_ready\ndata: {\"sources\": []}\n\n"
    meta = (
        "id: 1\nevent: meta\n"
        'data: {"answerId": "ansR", "model": "m", "fromPageId": ""}\n\n'
    )
    sse = primer + summary + meta
    stable = {"status": "complete", "blocks": [{"kind": "p", "text": "ok"}]}
    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", sse)
        .on("GET", "/v1/wiki/qa/ansR", stable)
    )
    out = run(
        tools.WikiTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).ask(
            project="assistant", question="q"
        )
    )
    assert out["answer_id"] == "ansR"
    assert out["answer"] == "ok"


def test_ask_wiki_polls_until_status_complete(fake_rest):
    """ask_wiki streams the meta answerId, then polls until snapshot status is terminal."""
    p_block = {"kind": "p", "text": "Because reasons."}
    sources_block = {"kind": "sources", "items": ["src://a", "src://b"]}
    # Poll sequence: running (empty) → running (partial) → complete. The snapshot
    # ``status`` is the authoritative terminal signal.
    sequence = [
        {"answerId": "ans1", "status": "running", "blocks": []},
        {"answerId": "ans1", "status": "running", "blocks": [p_block]},
        {"answerId": "ans1", "status": "complete", "blocks": [p_block, sources_block]},
    ]
    qa_snapshot, poll_state = _sequence_poller(sequence)
    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", _qa_meta_sse("ans1"))
        .on_handler("GET", "/v1/wiki/qa/ans1", qa_snapshot)
    )
    out = run(
        tools.WikiTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).ask(
            project="assistant",
            question="why?",
        )
    )
    assert out["answer_id"] == "ans1"
    assert out["answer"] == "Because reasons."
    assert out["citations"] == ["src://a", "src://b"]
    assert out["status"] == "complete"
    assert poll_state["n"] == 3  # terminates exactly on the complete-status read
    # The streamed POST body carried slug + question (and no empty model).
    qa_req = fake.find("POST", "/v1/wiki/qa")
    assert qa_req.json["slug"] == "assistant"
    assert qa_req.json["question"] == "why?"
    assert "model" not in qa_req.json


def test_ask_wiki_running_status_not_marked_complete(fake_rest):
    """A ``running`` snapshot is never terminal — even with a premature sources block.

    Reproduces the truncation bug: the snapshot already carries a ``sources``
    block while ``status`` is still ``running``. Because ``status`` is
    authoritative, the tool keeps polling until it flips to ``complete`` rather
    than returning a truncated body mislabelled complete.
    """
    sources = {"kind": "sources", "items": ["s://x"]}
    p_block = {"kind": "p", "text": "Full answer."}
    sequence = [
        {"answerId": "ansP", "status": "running", "blocks": [sources]},
        {"answerId": "ansP", "status": "complete", "blocks": [p_block, sources]},
    ]
    qa_snapshot, poll_state = _sequence_poller(sequence)
    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", _qa_meta_sse("ansP"))
        .on_handler("GET", "/v1/wiki/qa/ansP", qa_snapshot)
    )
    out = run(
        tools.WikiTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).ask(
            project="assistant",
            question="why?",
        )
    )
    assert out["status"] == "complete"
    assert out["answer"] == "Full answer."  # not the premature sources-only partial
    assert poll_state["n"] == 2


def test_ask_wiki_omits_model_when_none(fake_rest):
    """model=None means the body carries no model so the server uses its default."""
    stable = {"status": "complete", "blocks": [{"kind": "p", "text": "hi"}]}
    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", _qa_meta_sse("ansX"))
        .on("GET", "/v1/wiki/qa/ansX", stable)
    )
    run(
        tools.WikiTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).ask(
            project="assistant", question="q", model=None
        )
    )
    assert "model" not in fake.find("POST", "/v1/wiki/qa").json


def test_ask_wiki_forwards_model_when_given(fake_rest):
    """A truthy model is forwarded in the QA body."""
    stable = {"status": "complete", "blocks": [{"kind": "p", "text": "hi"}]}
    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", _qa_meta_sse("ansM"))
        .on("GET", "/v1/wiki/qa/ansM", stable)
    )
    run(
        tools.WikiTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).ask(
            project="assistant", question="q", model="gpt-x"
        )
    )
    assert fake.find("POST", "/v1/wiki/qa").json["model"] == "gpt-x"


def test_ask_wiki_returns_partial_on_timeout(fake_rest):
    """A QA whose blocks never render returns status 'running' rather than hanging."""
    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", _qa_meta_sse("ans2"))
        .on("GET", "/v1/wiki/qa/ans2", {"answerId": "ans2", "blocks": []})
    )
    out = run(
        tools.WikiTools(fake.client(), timeout_s=0.0, poll_interval_s=0.0).ask(
            project="assistant", question="slow?"
        )
    )
    assert out["status"] == "running"
    assert out["answer"] == ""
    assert out["citations"] == []


def test_ask_wiki_raises_when_no_meta_answer_id(fake_rest):
    """If the SSE stream never yields a meta answerId, ask_wiki raises."""
    primer_only = ":" + (" " * 8) + "\n\nevent: heartbeat\ndata: {}\n\n"
    fake = fake_rest.on_sse("POST", "/v1/wiki/qa", primer_only)
    with pytest.raises(RestError):
        run(tools.WikiTools(fake.client(), timeout_s=1.0).ask(project="assistant", question="q"))


def test_ask_wiki_transport_timeout_returns_resumable_handle(fake_rest):
    """#41: a transport ReadTimeout mid-poll degrades to the resumable answer_id.

    The START SSE yields the answer_id; the snapshot GET raises ``httpx.ReadTimeout``
    (the front-proxy/httpx cut). ``poll_or_handle`` must return
    ``{answer_id, status:'running'}`` — the resumable handle — NOT raise.
    """

    def _timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=_request)

    fake = (
        fake_rest
        .on_sse("POST", "/v1/wiki/qa", _qa_meta_sse("ansTO"))
        .on_handler("GET", "/v1/wiki/qa/ansTO", _timeout)
    )
    out = run(
        tools.WikiTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).ask(
            project="assistant", question="slow?"
        )
    )
    assert out["answer_id"] == "ansTO"  # resumable handle preserved
    assert out["status"] == "running"
    assert out["answer"] == ""


# ---------------------------------------------------------------------------
# integrations
# ---------------------------------------------------------------------------


def test_list_integrations_merges_tools_and_plugins(fake_rest):
    fake = (
        fake_rest
        .on("GET", "/api/tools", {"tools": [{"tool_id": "shell"}]})
        .on("GET", "/api/plugins", {"plugins": [{"name": "wiki"}]})
    )
    out = run(tools.IntegrationTools(fake.client()).discover(project="Assistant"))
    # tools are projected compact (tool_id + name); plugins keep just their name
    assert out["tools"] == [{"tool_id": "shell", "name": "shell"}]
    assert out["tool_count"] == 1
    assert out["plugins"] == [{"name": "wiki"}]
    # project scope passed through as a query param on /api/tools
    tools_req = fake.find("GET", "/api/tools")
    assert tools_req.params.get("project") == "Assistant"


# ---------------------------------------------------------------------------
# RestError propagation
# ---------------------------------------------------------------------------


def test_rest_error_surfaces_api_message(fake_rest):
    fake = fake_rest.on(
        "POST", "/api/sessions/s1/message", {"message": "No active run."}, status=404
    )
    with pytest.raises(RestError) as exc:
        run(tools.SessionTools(fake.client()).send_followup(session_id="s1", message="x"))
    assert "No active run." in str(exc.value)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Agentic Search ("Mewbo Search")
# ---------------------------------------------------------------------------

_WS = "/api/agentic_search/workspaces"
_RUNS = "/api/agentic_search/runs"


def _workspaces_payload():
    """Two workspaces — one carries console-only fields (instructions/history)."""
    return {
        "workspaces": [
            {
                "id": "ws-eng",
                "name": "Engineering",
                "desc": "eng docs + chat",
                "sources": ["notion", "slack"],
                "instructions": "internal-only prompt — must not leak to MCP",
                "past_queries": [{"q": "old query", "results": 3}],
                "created": "Jun 05, 2026",
            },
            {
                "id": "ws-design",
                "name": "Design",
                "desc": "",
                "sources": ["figma"],
                "past_queries": [],
            },
        ]
    }


def _run_payload(status="completed"):
    """A normalized RunPayload as POST /runs (echo) returns it under ``run``."""
    return {
        "run_id": "run-1",
        "session_id": "sess-1",
        "workspace_id": "ws-eng",
        "query": "deploy process",
        "status": status,
        "total_ms": 1234,
        "answer": {
            "tldr": "Deploys go through CI.",
            "bullets": [
                {"text": "CI gates every merge.", "cites": ["r1"]},
                {"text": "Rollback is one click.", "cites": ["r2"]},
            ],
            "confidence": 0.8,
            "sources_count": 2,
        },
        "results": [
            {
                "id": "r1", "source": "notion", "kind": "docs", "relevance": 0.9,
                "title": "Deploy Runbook", "url": "https://n/r1",
                "snippet": "Step 1: open the runbook.", "author": "Ada",
                "timestamp": "2026-06-01",
                "insight": {"label": "Key", "body": "CI is required."},
                "refs": [{"title": "CI", "url": "https://n/ci", "kind": "doc"}],
            },
            {
                "id": "r2", "source": "slack", "kind": "threads", "relevance": 0.7,
                "title": "rollback thread", "url": "https://s/r2",
                "snippet": "just click rollback.", "author": "Bo",
                "timestamp": "2026-06-02",
            },
        ],
        # Console-only surface the MCP projection must drop:
        "trace": [
            {"id": "t1", "agent_id": "a1", "name": "Notion", "source_id": "notion",
             "slot": 0, "lines": [{"t_ms": 1, "text": "searching"}]},
        ],
        "related_questions": ["How do I roll back a deploy?"],
        "related_people": [{"name": "Ada", "role": "SRE", "initials": "A", "color": 0}],
    }


def _post_runs_response(status="completed"):
    """POST /runs back-compat envelope: {run, run_id, session_id, status}."""
    payload = _run_payload(status)
    return {
        "run": payload,
        "run_id": payload["run_id"],
        "session_id": payload["session_id"],
        "status": status,
    }


def test_list_search_workspaces_drops_console_only_fields(fake_rest):
    """Discovery is compact: ids/names/sources/count, no instructions or history."""
    fake = fake_rest.on("GET", _WS, _workspaces_payload())
    out = run(tools.SearchTools(fake.client()).list_workspaces())
    eng = out["workspaces"][0]
    assert eng == {
        "id": "ws-eng",
        "name": "Engineering",
        "desc": "eng docs + chat",
        "sources": ["notion", "slack"],
        "recent_query_count": 1,
    }
    # instructions (untrusted) and full past_queries never reach the consumer.
    assert "instructions" not in eng
    assert "past_queries" not in eng


def test_search_resolves_by_name_and_returns_answer_tier(fake_rest):
    """search(workspace=<name>) → resolves id → POST /runs → compact answer tier."""
    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("completed"))
    )
    out = run(
        tools.SearchTools(fake.client()).search(query="deploy process", workspace="Engineering")
    )

    # The run was scoped to the resolved workspace id + carried the query.
    body = fake.find("POST", _RUNS).json
    assert body["workspace_id"] == "ws-eng"
    assert body["query"] == "deploy process"
    assert "project" not in body

    assert out["status"] == "completed"
    assert out["workspace_name"] == "Engineering"
    assert out["answer"]["tldr"] == "Deploys go through CI."
    # Bullets keep their cite ids so they resolve against the result index.
    assert out["answer"]["bullets"][0]["cites"] == ["r1"]
    assert out["result_count"] == 2
    assert out["related_questions"] == ["How do I roll back a deploy?"]

    # answer tier: compact results only — no snippet/insight/refs.
    first = out["results"][0]
    assert first == {
        "id": "r1", "source": "notion", "kind": "docs",
        "title": "Deploy Runbook", "url": "https://n/r1", "relevance": 0.9,
    }
    # Console-only surface is omitted entirely.
    assert "trace" not in out
    assert "related_people" not in out


def test_search_full_detail_includes_result_content(fake_rest):
    """detail='full' adds snippet/insight/refs to each result."""
    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("completed"))
    )
    out = run(
        tools.SearchTools(fake.client()).search(
            query="deploy", workspace="ws-eng", detail="full"
        )
    )
    first = out["results"][0]
    assert first["snippet"] == "Step 1: open the runbook."
    assert first["insight"] == {"label": "Key", "body": "CI is required."}
    assert first["refs"] == [{"title": "CI", "url": "https://n/ci", "kind": "doc"}]
    # A result without refs/insight stays lean even in full mode.
    assert "insight" not in out["results"][1]
    assert "refs" not in out["results"][1]


def test_search_resolves_by_id(fake_rest):
    """An exact id match wins without needing the name."""
    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("completed"))
    )
    run(tools.SearchTools(fake.client()).search(query="q", workspace="ws-design"))
    assert fake.find("POST", _RUNS).json["workspace_id"] == "ws-design"


def test_search_forwards_project(fake_rest):
    """A project scope is forwarded to POST /runs for source→tool scoping."""
    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("completed"))
    )
    run(tools.SearchTools(fake.client()).search(query="q", workspace="ws-eng", project="Assistant"))
    assert fake.find("POST", _RUNS).json["project"] == "Assistant"


def test_search_unknown_workspace_raises(fake_rest):
    """An unmatched workspace ref raises with the available names."""
    fake = fake_rest.on("GET", _WS, _workspaces_payload())
    with pytest.raises(ValueError) as exc:
        run(tools.SearchTools(fake.client()).search(query="q", workspace="Nope"))
    assert "Engineering" in str(exc.value)
    # No run was started for an unresolved workspace.
    assert f"POST {_RUNS}" not in fake.paths()


def test_search_ambiguous_name_raises(fake_rest):
    """A name matching multiple workspaces raises rather than guessing."""
    dup = {
        "workspaces": [
            {"id": "a", "name": "Dup", "sources": []},
            {"id": "b", "name": "dup", "sources": []},
        ]
    }
    fake = fake_rest.on("GET", _WS, dup)
    with pytest.raises(ValueError):
        run(tools.SearchTools(fake.client()).search(query="q", workspace="Dup"))


def test_search_invalid_detail_raises(fake_rest):
    """An unknown detail tier is rejected before any REST call."""
    with pytest.raises(ValueError):
        run(tools.SearchTools(fake_rest.client()).search(query="q", workspace="x", detail="bogus"))


def test_search_async_polls_until_terminal(fake_rest):
    """An async runner returns 'running'; search polls GET /runs/<id> to terminal."""
    sequence = [
        {"run": {"run_id": "run-1", "status": "running", "payload": None}},
        {"run": {"run_id": "run-1", "status": "completed", "payload": _run_payload()}},
    ]
    snapshot, poll_state = _sequence_poller(sequence)
    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("running"))
        .on_handler("GET", f"{_RUNS}/run-1", snapshot)
    )
    out = run(
        tools.SearchTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).search(
            query="q", workspace="ws-eng"
        )
    )
    assert out["status"] == "completed"
    assert out["answer"]["tldr"] == "Deploys go through CI."
    assert poll_state["n"] == 2  # polled once more after the initial 'running' read


def test_search_async_timeout_returns_running_partial(fake_rest):
    """A run that never settles returns status 'running' rather than hanging."""
    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("running"))
        .on("GET", f"{_RUNS}/run-1", {"run": {"status": "running", "payload": None}})
    )
    out = run(
        tools.SearchTools(fake.client(), timeout_s=0.0, poll_interval_s=0.0).search(
            query="q", workspace="ws-eng"
        )
    )
    assert out["status"] == "running"
    assert out["answer"]["tldr"] == ""  # no payload yet → empty synthesis
    assert out["results"] == []


def test_search_transport_timeout_returns_resumable_handle(fake_rest):
    """#41: a ReadTimeout mid-poll degrades to the resumable run_id, not a raise.

    POST /runs returns ``running`` + run_id; the snapshot GET raises
    ``httpx.ReadTimeout``. ``poll_or_handle`` must surface
    ``{run_id, status:'running'}`` so the caller can resume via ``get_search_run``.
    """

    def _timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=_request)

    fake = (
        fake_rest
        .on("GET", _WS, _workspaces_payload())
        .on("POST", _RUNS, _post_runs_response("running"))
        .on_handler("GET", f"{_RUNS}/run-1", _timeout)
    )
    out = run(
        tools.SearchTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).search(
            query="q", workspace="ws-eng"
        )
    )
    assert out["run_id"] == "run-1"  # resumable handle preserved
    assert out["status"] == "running"
    assert out["results"] == []


def test_get_search_run_shapes_snapshot(fake_rest):
    """get_search_run reads the durable RunRecord and shapes its payload."""
    fake = fake_rest.on(
        "GET", f"{_RUNS}/run-1",
        {"run": {"status": "completed", "payload": _run_payload()}},
    )
    out = run(tools.SearchTools(fake.client()).get_run(run_id="run-1"))
    assert out["status"] == "completed"
    assert out["run_id"] == "run-1"
    assert out["result_count"] == 2
    # No workspace name is known on a bare snapshot read.
    assert "workspace_name" not in out


def test_get_search_run_invalid_detail_raises(fake_rest):
    """An unknown detail tier is rejected before any REST call."""
    with pytest.raises(ValueError):
        run(tools.SearchTools(fake_rest.client()).get_run(run_id="run-1", detail="bogus"))


# ---------------------------------------------------------------------------
# structured_query
# ---------------------------------------------------------------------------


def test_structured_query_fast_completion_returns_output(fake_rest):
    """A fast POST that already carries the object returns it (status completed)."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    response = {"run_id": "s7:r1", "status": "completed", "workspace": "wiki",
                "output": {"name": "Ada"}}
    fake = fake_rest.on("POST", "/v1/structured", response)
    out = run(
        tools.StructuredQueryTools(fake.client()).query(
            query="Who?", schema=schema, workspace="wiki", tools=["wiki_search"]
        )
    )
    assert out["status"] == "completed"
    assert out["output"] == {"name": "Ada"}
    assert out["run_id"] == "s7:r1"
    req = fake.find("POST", "/v1/structured")
    assert req.json == {
        "query": "Who?",
        "schema": schema,
        "workspace": "wiki",
        "tools": ["wiki_search"],
    }


def test_structured_query_polls_run_until_terminal(fake_rest):
    """A running POST is bounded-polled on GET /v1/structured/<run_id> until terminal."""
    schema = {"type": "object", "properties": {}}
    sequence = [
        {"run_id": "s8:r1", "status": "running"},
        {"run_id": "s8:r1", "status": "completed", "output": {"ok": True}},
    ]
    poll_state = {"n": 0}

    def _poller(_req):
        idx = min(poll_state["n"], len(sequence) - 1)
        poll_state["n"] += 1
        return httpx.Response(200, json=sequence[idx])

    fake = (
        fake_rest
        .on("POST", "/v1/structured", {"run_id": "s8:r1", "status": "running"})
        .on_handler("GET", "/v1/structured/s8:r1", _poller)
    )
    out = run(
        tools.StructuredQueryTools(
            fake.client(), timeout_s=5.0, poll_interval_s=0.0
        ).query(query="hi", schema=schema)
    )
    assert out["status"] == "completed"
    assert out["output"] == {"ok": True}
    assert out["run_id"] == "s8:r1"


def test_get_structured_run_fetches_by_id(fake_rest):
    """get_structured_run resumes a run by id (GET /v1/structured/<run_id>)."""
    fake = fake_rest.on(
        "GET", "/v1/structured/s9:r1", {"run_id": "s9:r1", "status": "completed",
                                        "output": {"x": 1}}
    )
    out = run(tools.StructuredQueryTools(fake.client()).get_run(run_id="s9:r1"))
    assert out == {"run_id": "s9:r1", "status": "completed", "output": {"x": 1}}


def test_structured_query_omits_optional_fields(fake_rest):
    """workspace/tools are only sent when provided (keeps the body minimal)."""
    schema = {"type": "object", "properties": {}}
    fake = fake_rest.on("POST", "/v1/structured", {"workspace": None, "output": {}})
    run(tools.StructuredQueryTools(fake.client()).query(query="hi", schema=schema))
    req = fake.find("POST", "/v1/structured")
    assert req.json == {"query": "hi", "schema": schema}


# ---------------------------------------------------------------------------
# Gold-standard seams: re-engage / no-op, spin-up guard, cleanup, discovery,
# resumable wiki answer, clean tool-call summaries, HTML-safe errors
# ---------------------------------------------------------------------------


def test_send_followup_surfaces_run_id_on_reengage(fake_rest):
    """An idle session re-engages: the API returns a run_id, surfaced by the tool."""
    fake = fake_rest.on(
        "POST", "/api/sessions/s1/message", {"enqueued": True, "run_id": "s1:r2"}, status=200
    )
    out = run(tools.SessionTools(fake.client()).send_followup(session_id="s1", message="more"))
    assert out == {"session_id": "s1", "status": "enqueued", "run_id": "s1:r2"}


def test_interrupt_idle_is_graceful_no_op(fake_rest):
    """interrupt on an idle session is a 200 no-op (interrupted: false)."""
    fake = fake_rest.on("POST", "/api/sessions/s1/interrupt", {"interrupted": False}, status=200)
    out = run(tools.SessionTools(fake.client()).interrupt(session_id="s1"))
    assert out == {"session_id": "s1", "status": "no_active_run"}


def test_agent_tree_initializing_during_spinup(fake_rest):
    """A failed /agents call during spin-up yields {status: initializing}, not an error."""
    fake = fake_rest.on("GET", "/api/sessions/s1/agents", {"message": "starting"}, status=503)
    out = run(tools.SessionTools(fake.client()).agent_tree(session_id="s1"))
    assert out == {"status": "initializing"}


def test_cleanup_worktree_method_removed():
    """Fix 2: SessionTools no longer exposes cleanup_worktree (system-owned lifecycle)."""
    assert not hasattr(tools.SessionTools, "cleanup_worktree"), (
        "cleanup_worktree must be removed from SessionTools (knob-minimization Fix 2)"
    )


def test_list_projects_surfaces_identity_and_drops_noise(fake_rest):
    """list_projects keeps name/project_id/repo/aliases; drops non-discovery fields."""
    payload = {
        "projects": [
            {
                "name": "Assistant",
                "source": "config",
                "path": "/secret/host/path",
                "repo": {"host": "git.hurricane.home", "owner": "bearlike", "name": "Assistant"},
                "aliases": ["git.hurricane.home/bearlike/Assistant", "bearlike/Assistant"],
            }
        ]
    }
    fake = fake_rest.on("GET", "/api/projects", payload)
    out = run(tools.ProjectTools(fake.client()).list_projects())
    row = out["projects"][0]
    assert row["name"] == "Assistant"
    assert row["repo"]["owner"] == "bearlike"
    assert "bearlike/Assistant" in row["aliases"]
    assert "path" not in row  # host paths are not discovery signal


def test_get_wiki_answer_resumes_by_id(fake_rest):
    """get_wiki_answer fetches a snapshot by answer_id (resume a timed-out ask)."""
    snap = {
        "status": "complete",
        "blocks": [
            {"kind": "p", "text": "Done."},
            {"kind": "sources", "items": ["s://1"]},
        ],
    }
    fake = fake_rest.on("GET", "/v1/wiki/qa/ansZ", snap)
    out = run(tools.WikiTools(fake.client()).get_answer(answer_id="ansZ"))
    assert out["answer_id"] == "ansZ"
    assert out["answer"] == "Done."
    assert out["citations"] == ["s://1"]
    assert out["status"] == "complete"


def test_history_overview_renders_tool_call_turn_from_steps(fake_rest):
    """A tool-call-only turn renders '→ called <tool>', never the leaked sentinel.

    Guards #44.2: the assistant text is the upstream-sanitized placeholder plus a
    model-leaked tool-call serialization; the summary must come from the steps.
    """
    events = {
        "running": False,
        "status": "completed",
        "events": [
            {"type": "user", "payload": {"text": "do it"}, "ts": "t0"},
            {"type": "tool_result", "payload": {"tool_id": "wiki_load_grounder"}, "ts": "t1"},
            {
                "type": "assistant",
                "payload": {"text": "(no content)call:default_api:wiki_load_grounder{}"},
                "ts": "t2",
            },
        ],
    }
    fake = fake_rest.on("GET", "/api/sessions/s1/events", events)
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="overview"))
    assert out["summary"] == "→ called wiki_load_grounder"
    assert "(no content)" not in out["summary"]
    assert "default_api" not in out["summary"]
    assert out["status"] == "completed"  # authoritative status from the events meta


def test_error_message_drops_raw_html_body():
    """A raw Werkzeug HTML 404 page is never dumped; a terse hint is added instead."""
    from mewbo_mcp.rest import _error_message

    resp = httpx.Response(404, text="<!doctype html><title>404 Not Found</title><body>x</body>")
    msg = _error_message(resp)
    # HTML is never surfaced
    assert "<html" not in msg.lower()
    assert "<!doctype" not in msg.lower()
    assert "404 not found" not in msg.lower()
    # Status code is present; a terse hint helps the caller
    assert "404" in msg


def test_error_message_reads_structured_envelope():
    """A structured {error:{reason}} envelope surfaces its reason verbatim."""
    from mewbo_mcp.rest import _error_message

    resp = httpx.Response(400, json={"error": {"code": 400, "reason": "project not found"}})
    assert _error_message(resp) == "REST API returned 400: project not found"


def test_error_message_empty_body_adds_hint():
    """Fix 3: an empty response body gets a terse hint rather than a bare status code."""
    from mewbo_mcp.rest import _error_message

    resp = httpx.Response(503)
    msg = _error_message(resp)
    assert "503" in msg
    # A hint is present so the caller has an actionable message
    assert len(msg) > len("REST API returned 503")


# ---------------------------------------------------------------------------
# Fix 3 — structured_query: completed/failed terminal, run_id top-level,
#          poll timeout returns running, 422 error carries run_id recovery
# ---------------------------------------------------------------------------


def test_structured_query_completed_is_terminal(fake_rest):
    """'completed' status is treated as terminal; output is returned immediately."""
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    response = {"run_id": "sq:r1", "status": "completed", "output": {"x": "hello"}}
    fake = fake_rest.on("POST", "/v1/structured", response)
    out = run(tools.StructuredQueryTools(fake.client()).query(query="q", schema=schema))
    assert out["status"] == "completed"
    assert out["output"] == {"x": "hello"}
    # run_id is always a top-level field
    assert out["run_id"] == "sq:r1"


def test_structured_query_failed_is_terminal(fake_rest):
    """'failed' status is treated as terminal (no further polling)."""
    schema = {"type": "object", "properties": {}}
    response = {"run_id": "sq:r2", "status": "failed", "error": "model did not emit"}
    fake = fake_rest.on("POST", "/v1/structured", response)
    out = run(tools.StructuredQueryTools(fake.client()).query(query="q", schema=schema))
    assert out["status"] == "failed"
    assert out["run_id"] == "sq:r2"


def test_structured_query_run_id_always_top_level(fake_rest):
    """run_id is always surfaced as a top-level field, never buried in prose."""
    schema = {"type": "object", "properties": {}}
    # POST returns a running snapshot with a run_id
    fake = (
        fake_rest
        .on("POST", "/v1/structured", {"run_id": "sq:r3", "status": "running"})
        .on("GET", "/v1/structured/sq:r3", {"run_id": "sq:r3", "status": "completed",
                                             "output": {"done": True}})
    )
    out = run(
        tools.StructuredQueryTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).query(
            query="q", schema=schema
        )
    )
    assert out["run_id"] == "sq:r3"
    assert out["status"] == "completed"


def test_structured_query_poll_timeout_returns_running_with_run_id(fake_rest):
    """A poll timeout returns {run_id, status:'running'} so caller can resume."""
    schema = {"type": "object", "properties": {}}
    fake = (
        fake_rest
        .on("POST", "/v1/structured", {"run_id": "sq:r4", "status": "running"})
        .on("GET", "/v1/structured/sq:r4", {"run_id": "sq:r4", "status": "running"})
    )
    out = run(
        tools.StructuredQueryTools(fake.client(), timeout_s=0.0, poll_interval_s=0.0).query(
            query="q", schema=schema
        )
    )
    # Timed out → running partial, but run_id MUST be present for recovery
    assert out["status"] == "running"
    assert out["run_id"] == "sq:r4"


def test_structured_query_transport_timeout_returns_resumable_handle(fake_rest):
    """#41: a ReadTimeout mid-poll degrades to the resumable run_id, not a raise.

    POST returns ``running`` + run_id; the snapshot GET raises ``httpx.ReadTimeout``.
    The tool must return ``{run_id, status:'running'}`` (resume via
    ``get_structured_run``), NOT propagate the transport error with no id.
    """
    schema = {"type": "object", "properties": {}}

    def _timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=_request)

    fake = (
        fake_rest
        .on("POST", "/v1/structured", {"run_id": "sq:r7", "status": "running"})
        .on_handler("GET", "/v1/structured/sq:r7", _timeout)
    )
    out = run(
        tools.StructuredQueryTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).query(
            query="q", schema=schema
        )
    )
    assert out["run_id"] == "sq:r7"  # resumable handle preserved
    assert out["status"] == "running"


def test_get_structured_run_unknown_id_raises_404(fake_rest):
    """#64 contract-lock: an unknown run id surfaces as a 404 RestError.

    The API 404s an unknown id; the MCP must propagate a ``RestError`` with
    ``status_code == 404`` (the server's ``_enveloped`` then maps it to
    ``not_found`` / ``retryable:false``), NOT fabricate a phantom-idle 200. This
    catches a future API regression to 200-idle at the MCP boundary. No MCP-side
    existence pre-check — we rely entirely on the API 404.
    """
    fake = fake_rest.on(
        "GET", "/v1/structured/unknown:r1",
        {"error": {"code": 404, "reason": "run unknown:r1 not found"}},
        status=404,
    )
    with pytest.raises(RestError) as exc_info:
        run(tools.StructuredQueryTools(fake.client()).get_run(run_id="unknown:r1"))
    assert exc_info.value.status_code == 404


def test_long_running_timeout_budgets_below_proxy_ceiling(fake_rest):
    """#41: every long-running tool's default budget < the transport/proxy ceiling.

    The poll budget MUST be the tightest ceiling so the tool ALWAYS returns the
    resumable handle as ``status:'running'`` before httpx (30s read) or the
    shorter front-proxy can cut the connection and strand the caller.
    """
    ceiling = tools.PROXY_CEILING_S
    client = fake_rest.client()
    for cls in (tools.WikiTools, tools.SearchTools, tools.StructuredQueryTools):
        assert cls(client).timeout_s < ceiling, cls.__name__


def test_structured_query_422_raises_rest_error_with_reason(fake_rest):
    """Fix 3: a 422 GET (model didn't emit) raises RestError with the reason string.

    The _enveloped decorator at the server layer converts this into a structured
    {error:{code, reason, retryable}} envelope. The run_id from the initial POST
    is already in the caller's possession at that point, enabling recovery via
    get_structured_run.
    """
    from mewbo_mcp.rest import RestError

    schema = {"type": "object", "properties": {}}
    fake = (
        fake_rest
        .on("POST", "/v1/structured", {"run_id": "sq:r5", "status": "running"})
        .on(
            "GET", "/v1/structured/sq:r5",
            {"error": {"code": 422, "reason": "model did not emit a structured response"}},
            status=422,
        )
    )
    # At the tools layer a 422 GET propagates as RestError (server's _enveloped wraps it)
    with pytest.raises(RestError) as exc_info:
        run(
            tools.StructuredQueryTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).query(
                query="q", schema=schema
            )
        )
    # The error reason must be actionable, surfacing the API's reason text
    assert "model did not emit" in str(exc_info.value)


def test_structured_query_awaiting_approval_not_terminal(fake_rest):
    """'awaiting_approval' is NOT treated as terminal (removed from TERMINAL set)."""
    schema = {"type": "object", "properties": {}}
    # awaiting_approval → then completed
    sequence = [
        {"run_id": "sq:r6", "status": "awaiting_approval"},
        {"run_id": "sq:r6", "status": "completed", "output": {"x": 1}},
    ]
    call_count = {"n": 0}

    def _poller(_req):
        idx = min(call_count["n"], len(sequence) - 1)
        call_count["n"] += 1
        return httpx.Response(200, json=sequence[idx])

    fake = (
        fake_rest
        .on("POST", "/v1/structured", {"run_id": "sq:r6", "status": "running"})
        .on_handler("GET", "/v1/structured/sq:r6", _poller)
    )
    out = run(
        tools.StructuredQueryTools(fake.client(), timeout_s=5.0, poll_interval_s=0.0).query(
            query="q", schema=schema
        )
    )
    # Must keep polling past awaiting_approval, reaching completed
    assert out["status"] == "completed"
    assert out["output"] == {"x": 1}
    assert call_count["n"] == 2  # polled twice: awaiting_approval → completed
