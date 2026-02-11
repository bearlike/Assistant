# meeseeks-tools

Tool implementations and integrations for Meeseeks. This package ships the built‑in local tools (file read/list/edit, shell) plus integrations for Home Assistant and MCP.

## What it provides
- Aider-based local tools for file reads, directory listing, edit blocks, and shell commands.
- MCP tool integration for remote tool servers.
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
