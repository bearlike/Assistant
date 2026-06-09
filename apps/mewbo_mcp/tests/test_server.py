"""Smoke tests for FastMCP server wiring.

We don't start a live ASGI server; we only assert the server builds and that
the expected ~15 tools are registered with the streamable-HTTP path. The tool
*behavior* is covered by ``test_tools.py``.
"""

from __future__ import annotations

import asyncio

from mewbo_mcp.config import McpConfig
from mewbo_mcp.server import build_server

_EXPECTED_TOOLS = {
    "create_session",
    "send_followup",
    "interrupt_session",
    # Fix 2: cleanup_worktree removed (system-owned lifecycle, not caller-controlled)
    "list_sessions",
    "get_session_history",
    "get_agent_tree",
    "list_wiki_projects",
    "read_wiki_structure",
    "read_wiki_page",
    "submit_insight",
    "ask_wiki",
    "get_wiki_answer",
    "structured_query",
    "get_structured_run",
    "list_integrations",
    "list_projects",
    "list_search_workspaces",
    "search",
    "get_search_run",
}


def test_build_server_registers_all_tools():
    server = build_server(McpConfig(api_url="http://api.test", host="127.0.0.1", port=5125))
    listed = asyncio.run(server.list_tools())
    names = {t.name for t in listed}
    assert names == _EXPECTED_TOOLS


def test_streamable_http_path_is_mcp():
    server = build_server(McpConfig(api_url="http://api.test", host="0.0.0.0", port=9000))
    assert server.settings.streamable_http_path == "/mcp"
    assert server.settings.host == "0.0.0.0"
    assert server.settings.port == 9000


def test_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv("MEWBO_API_URL", raising=False)
    monkeypatch.delenv("MEWBO_MCP_HOST", raising=False)
    monkeypatch.delenv("MEWBO_MCP_PORT", raising=False)
    cfg = McpConfig.from_env()
    assert cfg.api_url == "http://localhost:5124"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 5127


def test_config_from_env_overrides(monkeypatch):
    monkeypatch.setenv("MEWBO_API_URL", "https://mewbo.example.com/")
    monkeypatch.setenv("MEWBO_MCP_PORT", "7000")
    cfg = McpConfig.from_env()
    assert cfg.api_url == "https://mewbo.example.com"  # trailing slash stripped
    assert cfg.port == 7000
