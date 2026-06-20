# MCP Tools

Model Context Protocol (MCP) tools extend Mewbo with external tool servers. Any MCP-compatible server can be plugged in via a config file. This includes file systems, databases, APIs, code execution environments, and search engines. Tools contributed by MCP servers appear in the tool registry alongside Mewbo' built-in tools and are available to every session.

> [!TIP] Drop-in compatible with Claude Code and VS Code
> Mewbo reads the same `.mcp.json` / `mcp.json` schema, accepting both the `servers` (Mewbo-native) and `mcpServers` (Claude Code / VS Code) top-level keys. Environment variable expansion follows the same `${VAR}` convention. If you already have an MCP config for another tool, copy it in and it will work unchanged. See the official [Model Context Protocol](https://modelcontextprotocol.io) specification.

## Configuring MCP servers

Define the servers you want the assistant to reach through a JSON config. The primary file is [`configs/mcp.json`](repo:configs/mcp.json) at the repo root (or `$MEWBO_HOME/mcp.json` for a global install).

**Example `configs/mcp.json`:**

```json
{
  "servers": {
    "codex_tools": {
      "transport": "streamable_http",
      "url": "http://127.0.0.1:6783/mcp/Codex-Tools",
      "headers": {
        "Authorization": "Bearer ${MY_MCP_TOKEN}"
      }
    },
    "filesystem": {
      "transport": "stdio",
      "command": ["mcp-filesystem", "--root", "/home/user/projects"]
    }
  }
}
```

`${VAR_NAME}` and `$VAR_NAME` patterns are expanded from the process environment at load time. Both `"servers"` and `"mcpServers"` (Claude Code / VS Code format) are accepted as the top-level key. Mewbo normalises them to a common shape internally, so you can drop in config files written for other tools.

## Supported transports

| Transport | Config keys | Use case |
|-----------|-----------|---------|
| `streamable_http` | `url`, `headers` | Remote HTTP servers (recommended for persistent services) |
| `http` | `url`, `headers` | Alias for `streamable_http`, accepted for compatibility |
| `stdio` | `command` | Local subprocess (binary on `PATH`) |

## Tool discovery

At session start, Mewbo connects to each configured MCP server, fetches its tool schema, and registers those tools in the registry. Connections are persistent. There is no per-request reconnect overhead, and a config change picks up on the next session.

## Deferred tool-schema loading (tool search)

Every MCP tool carries a JSON schema, and binding all of them to the model on **every** turn is expensive — a typical fleet of MCP servers can add tens of thousands of tokens of tool definitions to each request. Mewbo avoids this with **on-demand schema loading**, modelled on Claude Code's Tool Search Tool.

When deferral is active, MCP tool schemas (and any tool explicitly marked deferrable) are **stripped from the initial request** and surfaced to the model by name only, grouped by server, via a compact `<available-mcp-servers>` block. The model fetches the schemas it actually needs by calling the built-in **`tool_search`** tool (`select:tool_a,tool_b` for a direct fetch, or keywords for a fuzzy search). Mewbo then re-binds those tools so they become callable. Discovery is replayed from the conversation each turn, so it survives compaction.

Because the search round-trip happens **client-side** (the schemas come back as a normal tool result), this works through any LLM proxy — it does not depend on the provider forwarding tool-reference blocks.

Configure it under `agent.tool_search` in `app.json`:

| `mode` | Behaviour |
|--------|-----------|
| `auto` *(default)* | Defer only when the number of deferrable tools exceeds `auto_threshold` (default `25`). Lean or zero-MCP sessions keep verbatim binding and pay nothing; large fleets are spared the per-turn cost. |
| `on` | Always defer MCP / deferrable tools. |
| `off` | Never defer — every schema is bound on turn one. |

```json
{
  "agent": {
    "tool_search": { "mode": "auto", "auto_threshold": 25 }
  }
}
```

The `tool_search` tool is always available (it is exempt from `allowed_tools` scoping), so even a tightly scoped sub-agent can still reach its deferred tools. A model that cannot reliably issue a `tool_search` call simply sees no MCP tools for that turn — a graceful degradation rather than an error.

## Choosing which tools a session sees

You can control which MCP tools are bound to a session:

- **Console**: the config menu has a tool selector. Pick which MCP tools to enable for the current session.
- **API**: pass `allowed_tools` in the session create payload or the query body to scope which tools the LLM can call.
- **CLI**: run `/mcp select` to interactively pick servers and tools.

## Per-project MCP config

Drop a `.mcp.json` file at your project root. When you start a session inside that project, its servers are merged with the global [`configs/mcp.json`](repo:configs/mcp.json) automatically. You can also place `.mcp.json` files deeper in the tree for sub-package–specific tools.

**Example project `.mcp.json`:**

```json
{
  "servers": {
    "project_db": {
      "transport": "stdio",
      "command": ["mcp-sqlite", "--db", "./dev.db"]
    }
  }
}
```

Both the Mewbo schema (`"servers"`) and the Claude Code schema (`"mcpServers"`) are accepted. See [Project Configuration](project-configuration.md#project-level-mcp-configuration) for the full merge reference.

## Troubleshooting

### "MCP server 'X' not found in config"

The session started without the project directory set correctly, so the project-level `.mcp.json` was not picked up. Make sure the session has a valid `project` set and that the project path is mounted (Docker) or accessible on the host.

### Tool not available after a config change

Edits to `mcp.json` are picked up on the next session start. Start a new session (or restart the API if you need the change to propagate to every running session) and the updated servers will connect.

### Common error signatures

| Symptom | Cause |
|---------|-------|
| `ERROR: Connection refused` | Server URL unreachable or the process isn't running. |
| Tool schema validation error at startup | Server returned a schema Mewbo cannot parse; check server version compatibility. |
| `Tool 'X' not found on server 'Y' after reconnect` | The tool was removed from the server between sessions. |
| Session starts but MCP tools missing | An `allowed_tools` filter excluded them; check the console tool selector or the API payload. |

See also: [Troubleshooting](reference.md) for the general debugging methodology.

---

> [!NOTE] How it works internally
> See [Architecture Overview → MCP connection pool](core-orchestration.md#mcp).
