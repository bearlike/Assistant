---
name: meeseeks-cli-smoketest
description: End-to-end smoke testing of the Meeseeks CLI via tmux. Use this skill when asked to test the CLI, verify CLI behavior after changes, smoke-test the agent loop, check for regressions, or validate MCP/plugin/session features work correctly through the terminal interface. Also use when debugging CLI crashes, MCP connection issues, or session lifecycle problems that need live reproduction.
---

# Meeseeks CLI Smoke Test via Tmux

Automate end-to-end CLI testing by running `meeseeks` inside a tmux pane, sending commands/queries, and analyzing verbose output for errors, warnings, and regressions.

## Why tmux

The CLI is a full-screen Rich/Textual TUI. You cannot run it directly via Bash tool because it requires a PTY and renders interactive widgets. Tmux gives you a real terminal to drive the CLI while capturing output programmatically via `tmux capture-pane`.

## Setup

Find a running tmux session and create a new window:

```bash
tmux list-sessions
tmux new-window -t <session>:<next> -n meeseeks-test
```

Launch with maximum verbosity and auto-approve (skips permission prompts):

```bash
tmux send-keys -t <session>:meeseeks-test "uv run meeseeks -vv --auto-approve" Enter
```

Wait for startup (MCP connections, plugin loading, skill discovery). Startup typically takes 5-10 seconds depending on MCP server count. Capture and verify the banner appears:

```bash
sleep 10 && tmux capture-pane -t <session>:meeseeks-test -p -S -100
```

Look for the ready banner showing model, session ID, tool counts, and `meeseeks>` prompt. If the prompt hasn't appeared, wait longer — MCP servers may take time to connect.

## Capture timing

This is the critical non-obvious part. Different operations need different wait times before capturing output:

| Operation | Wait (seconds) | Why |
|---|---|---|
| Startup | 8-10 | MCP pool connects, plugins load, skills discover |
| Slash command (`/help`, `/status`) | 2-3 | Local only, no LLM call |
| Interactive command (`/mcp`, `/models`) | 2-3 | Opens TUI picker — must send `Escape` to dismiss before next command |
| Simple query (no tools) | 10-15 | Action plan + LLM call + response |
| Tool-using query | 15-25 | Plan + LLM + tool execution + synthesis |
| MCP tool query | 25-40 | Plan + LLM + MCP network call + synthesis |
| `/compact` | 10-15 | Rebuilds summary via LLM call |

Always use `sleep N && tmux capture-pane` as a single command — do not separate them. Adjust the scroll buffer depth (`-S -N`) based on expected output verbosity. `-S -60` is usually sufficient; use `-S -100` for startup output.

## Test progression

Test in layers, from cheapest to most expensive. If an early layer fails, later layers will too.

### Layer 1: Slash commands (no LLM, no network)

These validate the CLI framework, config loading, and plugin discovery:

```
/help          — all commands listed, no crashes
/status        — session JSON with valid ID and idle state
/session       — session ID matches banner
/tokens        — budget table renders, context window > 0
/skills        — skill count matches banner, names listed
/plugins       — installed plugins table renders (note any WARNING lines)
```

**What to look for in verbose output**: `WARNING` or `ERROR` log lines during plugin/skill loading. Common issues:
- `Failed to parse manifest` — stale plugin cache, missing files
- `No YAML frontmatter` — agent definition files missing required format
- `Missing or invalid 'name'` — skill SKILL.md files lacking name field

These warnings are non-fatal but indicate plugin integration gaps.

### Layer 2: Interactive commands

Commands that open TUI pickers need special handling:

```bash
# /mcp opens a selector — verify it renders, then dismiss
tmux send-keys -t ... "/mcp" Enter
sleep 3
# Capture to verify the picker rendered with server list
tmux capture-pane -t ... -p -S -40
# Dismiss the picker
tmux send-keys -t ... Escape
sleep 1
```

### Layer 3: Simple query (tool-use loop, no MCP)

Send a query that exercises the core loop with a local tool:

```
List the files in the current directory
```

This tests: action plan generation, tool binding, `aider_list_dir_tool` execution, response synthesis. Wait 20 seconds. Verify:
- Action plan box rendered
- Tool call shown (look for the tool emoji line)
- Response box rendered with coherent content
- No Python tracebacks in verbose output

### Layer 4: MCP tool query

Send a query that forces an MCP tool call:

```
Use deepwiki to look up the architecture of bearlike/Assistant
```

This tests: MCP tool routing, connection pool, external network call, large response handling. Wait 30-40 seconds. **Critical signals to watch for**:

- `Connected to MCP server` — new on-demand connections (the pool connects lazily for project-level servers)
- `Disconnected from MCP server` — connection churn during tool-use loop
- `Failed to reconnect MCP server` — config merge or library compatibility issues
- `_create_streamable_http_session() got an unexpected keyword argument` — config normalization bug (type/transport collision)
- `Configuration error: Missing 'transport' key` — plugin MCP config not normalized

These MCP errors often surface only on the SECOND tool call or during `/compact`, because `refresh_if_config_changed` triggers config re-merge. The first call may succeed using the initially-connected pool, while reconnection uses the merged config (which may include CWD `.mcp.json` and plugin configs with different schemas).

### Layer 5: New features

Test recently-added CLI features:

```
/fork test-fork     — should print "Forked session: <id>"
/edit What is 2+2?  — should re-run with edited prompt and return "4"
/compact            — should produce a summary (may trigger MCP reconnection)
/budget             — should show non-zero token usage after queries
```

After `/compact`, check verbose output carefully — compaction re-initializes the tool registry and triggers `refresh_if_config_changed`, which is the most common place for MCP config merge bugs to surface.

## Analyzing results

### Error extraction

After each test, scan the captured output for these patterns:

```
WARNING     — non-fatal issues (plugin loading, config parsing)
ERROR       — failures that may affect functionality
Traceback   — Python exceptions (CLI crash or near-crash)
Failed to   — connection/reconnection failures
```

### MCP config merge issues

When you see MCP reconnection errors, the root cause is usually in the config merge pipeline:

1. **Multiple config formats**: `configs/mcp.json` (Meeseeks native: `servers` + `transport`) vs `.mcp.json` (Claude Code: `mcpServers` + `type`) vs plugin `.mcp.json` (varies)
2. **Deep merge collision**: `_deep_merge` on individual server configs can produce entries with BOTH `type` and `transport` when CWD overrides global
3. **Plugin config normalization**: Plugin `.mcp.json` files may use `mcpServers` wrapper or bare server format without `transport`

To trace: read the MCP configs (`configs/mcp.json`, `.mcp.json`, and plugin `.mcp.json` files), then check `_normalize_mcp_config` in `meeseeks_tools/integration/mcp.py` and `load_all_plugin_components` in `meeseeks_core/plugins.py`.

### Session verification

After testing, verify the session was properly tracked:

```
/status     — should show the current session state
/budget     — token counts should reflect actual usage
```

For deeper verification, check Langfuse traces (trace_id == session_id) and MongoDB transcript if available.

## Cleanup

When done testing, exit cleanly:

```bash
tmux send-keys -t <session>:meeseeks-test "/exit" Enter
sleep 2
tmux kill-window -t <session>:meeseeks-test
```

## Non-obvious gotchas

- **Plugin warnings repeat every turn**: Plugin/skill loading runs at each tool-use loop iteration, so the same warnings appear multiple times. This is normal — focus on unique warning messages, not count.
- **MCP servers connect lazily**: Project-level MCP servers (from `.mcp.json`) may not connect until the first query that needs them. The startup banner only shows globally-configured servers.
- **`/compact` is the stress test**: It re-initializes the most subsystems (plugins, skills, MCP, tool registry) and is the most likely place to surface integration bugs.
- **Interactive commands block the prompt**: If you send a query while `/mcp` or `/models` picker is open, it goes to the picker, not the CLI. Always dismiss interactive UI first with `Escape`.
- **Env var expansion**: CWD `.mcp.json` may contain `${VAR}` references. If the var isn't set in the shell environment where the CLI runs, auth tokens remain as literal strings and MCP calls fail silently.
