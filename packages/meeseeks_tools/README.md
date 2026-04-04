# meeseeks-tools

Tool implementations and integrations for Meeseeks. This package ships the built‑in local tools (file read/list/edit, shell) plus integrations for Home Assistant and MCP.

## What it provides
- **Configurable file editing:** Two edit mechanisms selectable via `agent.edit_tool` config — Aider-style SEARCH/REPLACE blocks (`search_replace_block`) and per-file structured patch (`structured_patch`). Shared utilities in `edit_common.py` keep both implementations DRY.
- Aider-based local tools for file reads, directory listing, and shell commands.
- MCP tool integration with persistent connection pooling (`MCPConnectionPool`), automatic reconnection, and per-request timeouts.
- Home Assistant tool adapter (used by the HA conversation integration).

## Use in the monorepo
From the repo root:
```bash
uv sync --extra tools
```

Then run an interface from `apps/` (CLI, API, chat UI), which will load tools via `ToolRegistry`.

## Notes
- Tool inputs are passed via `tool_input` (string or JSON object).
- Tool results surface through `tool_result` events with `tool_id` and `operation`.
