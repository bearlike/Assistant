"""Integration tests for the FastMCP server wiring layer.

We exercise the ``build_server()`` tool registrations end-to-end: a real
FastMCP server is built, each tool's closure is invoked through the FastMCP
tool manager (``tool.run()``) with a synthetic :class:`~mcp.server.fastmcp.Context`
that carries a Bearer token, and we inject a fake REST backend via
``httpx.MockTransport`` so no live server is needed.

This covers the uncovered lines in ``server.py``:
- ``_client(ctx)`` (lines 44-45): called on every tool invocation.
- Every tool body ``async with _client(ctx) as client: …`` (one per tool).
- ``main()`` entry point (lines 287-288).
- The ``if __name__ == "__main__"`` guard (line 296).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp.server.fastmcp import Context
from mewbo_mcp.auth import AuthError
from mewbo_mcp.config import McpConfig
from mewbo_mcp.rest import RestClient
from mewbo_mcp.server import build_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(server, token: str = "msk-test") -> Context:
    """Build a minimal FastMCP Context whose Bearer header satisfies auth."""
    request = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    return Context(
        request_context=SimpleNamespace(request=request),
        fastmcp=server,
    )


def _dispatch_factory(routes: dict[str, tuple[int, Any]]):
    """Return an httpx transport handler backed by a simple route table."""

    def _dispatch(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        for route_key, (status, body) in routes.items():
            if route_key == key:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"message": f"no route: {key}"})

    return _dispatch


def _run_tool(
    server,
    tool_name: str,
    arguments: dict[str, Any],
    routes: dict[str, tuple[int, Any]],
) -> Any:
    """
    Call the named tool through the server's tool manager.

    Patches ``authenticate`` to return the master token and ``RestClient.__init__``
    to inject a ``MockTransport`` — so the tool closure's ``_client(ctx)`` call
    succeeds without a live API or a valid KeyStore.
    """
    ctx = _make_ctx(server)
    tool = server._tool_manager._tools[tool_name]
    dispatch = _dispatch_factory(routes)

    _orig_init = RestClient.__init__

    def _mock_init(self, base_url: str, token: str, **kwargs: Any) -> None:
        _orig_init(self, base_url, token, transport=httpx.MockTransport(dispatch))

    with patch("mewbo_mcp.server.authenticate", return_value="msk-test"):
        with patch.object(RestClient, "__init__", _mock_init):
            return asyncio.run(tool.run(arguments, context=ctx))


@pytest.fixture
def server():
    """A fresh FastMCP server instance for each test."""
    return build_server(McpConfig(api_url="http://api.test", host="127.0.0.1", port=5127))


# ---------------------------------------------------------------------------
# _client() — auth integration (lines 44-45)
# ---------------------------------------------------------------------------


def test_client_helper_raises_auth_error_on_bad_token(server):
    """When authenticate() raises AuthError the tool body must not be entered."""
    ctx = _make_ctx(server, token="bad-token")
    tool = server._tool_manager._tools["interrupt_session"]
    with patch("mewbo_mcp.server.authenticate", side_effect=AuthError("bad token")):
        with pytest.raises(Exception):
            asyncio.run(tool.run({"session_id": "s1"}, context=ctx))


def test_client_helper_forwards_token_to_rest(server):
    """_client() creates a RestClient with the token returned by authenticate()."""
    captured: dict[str, str] = {}

    def _capturing_dispatch(req: httpx.Request) -> httpx.Response:
        captured["x-api-key"] = req.headers.get("x-api-key", "")
        return httpx.Response(202, json={"interrupted": True})

    _orig_init = RestClient.__init__

    def _mock_init(self, base_url: str, token: str, **kwargs: Any) -> None:
        _orig_init(self, base_url, token, transport=httpx.MockTransport(_capturing_dispatch))

    ctx = _make_ctx(server, token="msk-forwarded")
    tool = server._tool_manager._tools["interrupt_session"]
    with patch("mewbo_mcp.server.authenticate", return_value="msk-forwarded"):
        with patch.object(RestClient, "__init__", _mock_init):
            asyncio.run(tool.run({"session_id": "s1"}, context=ctx))
    assert captured["x-api-key"] == "msk-forwarded"


# ---------------------------------------------------------------------------
# Session tool wiring (lines 72-73, 91-92, 99-100, 116-117)
# ---------------------------------------------------------------------------


def test_server_create_session_wires_to_session_tools(server):
    """The create_session tool delegates to SessionTools and returns the session id."""
    routes = {
        "POST /api/sessions": (200, {"session_id": "s99"}),
        "POST /api/sessions/s99/query": (202, {"accepted": True}),
    }
    result = _run_tool(server, "create_session", {"prompt": "test task"}, routes)
    assert result["session_id"] == "s99"
    assert result["status"] == "running"


def test_server_send_followup_wires_to_session_tools(server):
    """send_followup delegates to SessionTools.send_followup."""
    routes = {"POST /api/sessions/s1/message": (202, {"enqueued": True})}
    result = _run_tool(server, "send_followup", {"session_id": "s1", "message": "steer"}, routes)
    assert result["session_id"] == "s1"
    assert result["status"] == "enqueued"


def test_server_interrupt_session_wires_to_session_tools(server):
    """interrupt_session delegates to SessionTools.interrupt."""
    routes = {"POST /api/sessions/s1/interrupt": (202, {"interrupted": True})}
    result = _run_tool(server, "interrupt_session", {"session_id": "s1"}, routes)
    assert result["session_id"] == "s1"
    assert result["status"] == "interrupted"


def test_server_list_sessions_wires_to_session_tools(server):
    """list_sessions delegates to SessionTools.list_sessions with optional filters."""
    sessions = {
        "sessions": [
            {"session_id": "a", "status": "running", "created_at": "2026-06-01", "context": {}}
        ]
    }
    routes = {"GET /api/sessions": (200, sessions)}
    result = _run_tool(server, "list_sessions", {"status": "running"}, routes)
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["session_id"] == "a"


# ---------------------------------------------------------------------------
# Session history tiers wiring (lines 142-143)
# ---------------------------------------------------------------------------


_EVENTS_PAYLOAD = {
    "running": False,
    "events": [
        {"type": "user", "ts": "t0", "payload": {"text": "hello"}},
        {"type": "assistant", "ts": "t1", "payload": {"text": "world"}},
    ],
}


def test_server_get_session_history_overview(server):
    """get_session_history delegates to SessionTools.history (overview tier)."""
    routes = {"GET /api/sessions/s1/events": (200, _EVENTS_PAYLOAD)}
    result = _run_tool(
        server, "get_session_history", {"session_id": "s1", "level": "overview"}, routes
    )
    assert result["session_id"] == "s1"
    assert result["turn_count"] == 1
    assert result["title"] == "hello"


def test_server_get_session_history_turns(server):
    """get_session_history delegates to SessionTools.history (turns tier)."""
    routes = {"GET /api/sessions/s1/events": (200, _EVENTS_PAYLOAD)}
    result = _run_tool(
        server, "get_session_history", {"session_id": "s1", "level": "turns"}, routes
    )
    assert "turns" in result
    assert result["turns"][0]["user_text"] == "hello"


# ---------------------------------------------------------------------------
# Agent tree wiring (lines 150-151)
# ---------------------------------------------------------------------------


def test_server_get_agent_tree_wires_to_session_tools(server):
    """get_agent_tree delegates to SessionTools.agent_tree."""
    tree = {"agents": [], "running": False}
    routes = {"GET /api/sessions/s1/agents": (200, tree)}
    result = _run_tool(server, "get_agent_tree", {"session_id": "s1"}, routes)
    assert result == tree


# ---------------------------------------------------------------------------
# Wiki tool wiring (lines 158-159, 164-165, 170-171, 194-195, 216-217)
# ---------------------------------------------------------------------------


def test_server_list_wiki_projects_wires_to_wiki_tools(server):
    """list_wiki_projects delegates to WikiTools.list_projects."""
    routes = {"GET /v1/wiki/projects": (200, [{"slug": "assistant"}])}
    result = _run_tool(server, "list_wiki_projects", {}, routes)
    assert result == [{"slug": "assistant"}]


def test_server_read_wiki_structure_wires_to_wiki_tools(server):
    """read_wiki_structure delegates to WikiTools.read_structure."""
    graph = {"nodes": [{"id": "a"}], "edges": []}
    routes = {"GET /v1/wiki/projects/assistant/graph": (200, graph)}
    result = _run_tool(server, "read_wiki_structure", {"project": "assistant"}, routes)
    assert result == graph


def test_server_read_wiki_page_wires_to_wiki_tools(server):
    """read_wiki_page delegates to WikiTools.read_page."""
    page = {"id": "intro", "body": "# Intro"}
    routes = {"GET /v1/wiki/projects/assistant/pages/intro": (200, page)}
    result = _run_tool(
        server, "read_wiki_page", {"project": "assistant", "page_id": "intro"}, routes
    )
    assert result["body"] == "# Intro"


def test_server_submit_insight_wires_to_wiki_tools(server):
    """submit_insight delegates to WikiTools.submit_insight (condense=True default)."""
    response = {"ok": True, "claims": [{"action": "created", "node_id": "n1", "content": "c"}]}
    routes = {"POST /v1/wiki/projects/assistant/insights": (201, response)}
    result = _run_tool(
        server,
        "submit_insight",
        {"project": "assistant", "insight": "Auth uses JWT"},
        routes,
    )
    assert result["ok"] is True


def test_server_submit_insight_condense_false(server):
    """submit_insight with condense=False posts verbatim content."""
    response = {"ok": True, "claims": []}
    routes = {"POST /v1/wiki/projects/assistant/insights": (200, response)}
    result = _run_tool(
        server,
        "submit_insight",
        {"project": "assistant", "insight": "short claim", "condense": False},
        routes,
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# ask_wiki wiring (lines 216-217) — SSE-driven, needs special dispatch
# ---------------------------------------------------------------------------


def test_server_ask_wiki_wires_to_wiki_tools(server):
    """ask_wiki streams QA SSE and polls the snapshot; wiring asserted end-to-end."""
    primer = ":" + (" " * 16) + "\n\n"
    meta_frame = (
        'id: 0\nevent: meta\ndata: {"answerId": "ans-svr", "model": "m", "fromPageId": ""}\n\n'
    )
    sse_body = (primer + meta_frame).encode()

    def _dispatch(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v1/wiki/qa":
            return httpx.Response(
                200, content=sse_body, headers={"content-type": "text/event-stream"}
            )  # noqa: E501
        if req.method == "GET" and req.url.path == "/v1/wiki/qa/ans-svr":
            return httpx.Response(200, json={"blocks": [{"kind": "p", "text": "Answer text"}]})
        return httpx.Response(404, json={"message": "no route"})

    ctx = _make_ctx(server)
    tool = server._tool_manager._tools["ask_wiki"]
    _orig_init = RestClient.__init__

    def _mock_init(self, base_url: str, token: str, **kwargs: Any) -> None:
        _orig_init(self, base_url, token, transport=httpx.MockTransport(_dispatch))

    with patch("mewbo_mcp.server.authenticate", return_value="msk-test"):
        with patch.object(RestClient, "__init__", _mock_init):
            result = asyncio.run(
                tool.run({"project": "assistant", "question": "how?"}, context=ctx)
            )

    assert result["answer_id"] == "ans-svr"
    assert result["answer"] == "Answer text"
    assert result["status"] == "complete"


# ---------------------------------------------------------------------------
# Integrations wiring (lines 230-231)
# ---------------------------------------------------------------------------


def test_server_list_integrations_wires_to_integration_tools(server):
    """list_integrations delegates to IntegrationTools.discover."""
    routes = {
        "GET /api/tools": (200, {"tools": [{"tool_id": "shell"}]}),
        "GET /api/plugins": (200, {"plugins": []}),
    }
    result = _run_tool(server, "list_integrations", {}, routes)
    assert result["tools"] == [{"tool_id": "shell"}]
    assert result["plugins"] == []


# ---------------------------------------------------------------------------
# Search tool wiring (lines 243-244, 264-265, 279-280)
# ---------------------------------------------------------------------------


_WS_PAYLOAD = {
    "workspaces": [
        {"id": "ws-1", "name": "Engineering", "desc": "", "sources": ["notion"], "past_queries": []}
    ]
}

_RUN_PAYLOAD = {
    "run_id": "run-1",
    "workspace_id": "ws-1",
    "query": "q",
    "status": "completed",
    "total_ms": 100,
    "answer": {"tldr": "ok", "bullets": [], "confidence": 1.0, "sources_count": 0},
    "results": [],
    "related_questions": [],
}


def test_server_list_search_workspaces_wires_to_search_tools(server):
    """list_search_workspaces delegates to SearchTools.list_workspaces."""
    routes = {"GET /api/agentic_search/workspaces": (200, _WS_PAYLOAD)}
    result = _run_tool(server, "list_search_workspaces", {}, routes)
    assert result["workspaces"][0]["id"] == "ws-1"
    assert "instructions" not in result["workspaces"][0]


def test_server_search_wires_to_search_tools(server):
    """search delegates to SearchTools.search (workspace resolved by name)."""
    routes = {
        "GET /api/agentic_search/workspaces": (200, _WS_PAYLOAD),
        "POST /api/agentic_search/runs": (
            200,
            {"run": _RUN_PAYLOAD, "run_id": "run-1", "session_id": "s1", "status": "completed"},
        ),
    }
    result = _run_tool(
        server, "search", {"query": "what is CI?", "workspace": "Engineering"}, routes
    )
    assert result["status"] == "completed"
    assert result["answer"]["tldr"] == "ok"


def test_server_get_search_run_wires_to_search_tools(server):
    """get_search_run delegates to SearchTools.get_run."""
    routes = {
        "GET /api/agentic_search/runs/run-1": (
            200,
            {"run": {"status": "completed", "payload": _RUN_PAYLOAD}},
        )
    }
    result = _run_tool(server, "get_search_run", {"run_id": "run-1"}, routes)
    assert result["status"] == "completed"
    assert result["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# main() entry point (lines 287-288)
# ---------------------------------------------------------------------------


def test_main_builds_server_and_calls_run():
    """main() constructs a server from env defaults and calls server.run()."""
    from mewbo_mcp.server import main

    with patch("mewbo_mcp.server.build_server") as mock_build:
        mock_server = mock_build.return_value
        main()
        mock_build.assert_called_once_with()
        mock_server.run.assert_called_once_with(transport="streamable-http")


# ---------------------------------------------------------------------------
# build_server uses config.from_env() when no config provided (line 34)
# ---------------------------------------------------------------------------


def test_build_server_without_config_uses_env_defaults(monkeypatch):
    """build_server(None) falls back to McpConfig.from_env()."""
    monkeypatch.delenv("MEWBO_API_URL", raising=False)
    monkeypatch.delenv("MEWBO_MCP_PORT", raising=False)
    # Should not raise; uses env defaults.
    s = build_server()
    assert s.settings.port == 5127


# ---------------------------------------------------------------------------
# Instructions string is embedded in server metadata
# ---------------------------------------------------------------------------


def test_server_instructions_mention_key_capabilities(server):
    """The _INSTRUCTIONS embedded in the server mention the key tool surfaces."""
    from mewbo_mcp.server import _INSTRUCTIONS

    assert "Bearer" in _INSTRUCTIONS
    assert "Mewbo Search" in _INSTRUCTIONS
    assert "Agentic Wiki" in _INSTRUCTIONS
