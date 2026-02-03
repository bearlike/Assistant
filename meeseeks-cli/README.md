# Meeseeks CLI

A terminal frontend for Meeseeks. It runs the same orchestration loop as the API and chat UI, but in a fast, interactive shell.

## Features
- Interactive conversations in the terminal.
- Shows action plans and tool results per request.
- Session transcripts and compaction from the core engine.
- Tag and fork sessions for experiments.

## Run
```bash
poetry run python cli_master.py
```

## MCP setup (required for /mcp tools)
- Create MCP server config: `configs/mcp.example.json`.
- Set `MESEEKS_MCP_CONFIG` in `.env`.
- MCP tools are auto-discovered and cached on load.
- Optional: add `auto_approve_tools` per server to allowlist tools (the CLI writes this when you pick “Yes, always”).

Advanced: set `MESEEKS_TOOL_MANIFEST` to override the tool list (disables auto-discovery).

## Common commands
- `/help` list commands
- `/plan on|off` toggle plan display
- `/summarize` compact the session transcript
- `/tag NAME` tag a session
- `/fork [TAG]` fork the current session
- `/new` start a new session
- `/mcp` list MCP tools and servers
- `/mcp init` scaffold an MCP config file
- `/mcp select` filter the MCP tools displayed
- `/models` switch models using a wizard
- `/automatic` enable auto-approve for this session (prompts for confirmation)
- `/quit` exit the CLI

CLI flags:
- `-v/--verbose` increase log verbosity (`-v` = debug, `-vv` = trace).
- `--auto-approve` start the session with auto-approve enabled (skips the `/automatic` prompt).
