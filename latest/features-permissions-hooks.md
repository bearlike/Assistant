# Permissions & Hooks

Meeseeks runs every tool call through a **permission policy** before it executes, and lets you wire **hooks** into session lifecycle events and individual tool calls. Together they give you a security boundary and a clean place to hang automation — notifications, audit logging, webhook fan-out, external guardrails.

**Quick example — auto-approve all tool calls in the current session:**

```
/automatic --yes
```

Or configure it globally:

```json
"permissions": {
  "approval_mode": "allow"
}
```

---

## Permission policies

### The three outcomes

Every tool call carries a `tool_id` (which tool it is, e.g. `aider_shell_tool`, `file_edit_tool`, `mcp__my_server__my_tool`) and an `operation` (what kind of action it is, e.g. `get`, `set`). Before the call runs, Meeseeks consults your policy and resolves to one of three outcomes:

| Decision | Meaning |
|----------|---------|
| `allow` | Execute without prompting |
| `deny` | Block unconditionally |
| `ask` | Prompt the user — on the CLI this is an interactive prompt; in the console it is an approval card |

Rules are evaluated in order; the first match wins. If no rule matches, Meeseeks falls back to per-operation defaults (`get` → `allow`, `set` → `ask`) and then to a catch-all `default_decision`.

### Rule syntax

Rules live in a JSON or TOML policy file pointed to by `permissions.policy_path`. Each rule matches on `tool_id` and `operation` using `fnmatch` glob patterns:

```json
{
  "rules": [
    { "tool_id": "aider_shell_tool", "operation": "*", "decision": "ask" },
    { "tool_id": "read_file",        "operation": "*", "decision": "allow" },
    { "tool_id": "*",                "operation": "get", "decision": "allow" },
    { "tool_id": "*",                "operation": "*",   "decision": "ask" }
  ],
  "default_by_operation": {
    "get": "allow",
    "set": "ask"
  },
  "default_decision": "ask"
}
```

`fnmatch` wildcards are simple: `*` matches any string, `?` matches a single character. Patterns work equally well on MCP tool IDs — `"mcp__*"` matches every MCP-sourced tool, `"mcp__my_server__*"` scopes to one server.

### Approval modes

`permissions.approval_mode` is a session-wide shortcut that overrides the rule file. It is useful when you want a blanket policy without editing rules:

| Value | Aliases | Effect |
|-------|---------|--------|
| `allow` | `auto`, `approve`, `yes` | All tools run without prompting |
| `deny` | `never`, `no` | All tools are blocked |
| `ask` (default) | — | Falls through to per-rule decisions |

### /automatic (CLI)

`/automatic` flips the current session into allow-mode without touching your config file:

```
/automatic          # prompts for confirmation
/automatic --yes    # skip confirmation
/automatic off      # revert to policy-driven mode
```

### Config example

```json
"permissions": {
  "policy_path": "./configs/policy.json",
  "approval_mode": "ask"
}
```

See [configuration.md](configuration.md#permissionsconfig) for field descriptions.

---

## Hooks

Hooks run custom code at specific moments in a session's life. They are declared in the `hooks` section of `configs/app.json`. A failing hook is logged as a warning and never blocks execution, so hooks are safe to use for side effects even if the external endpoint is flaky.

### When hooks fire

| Event | When it fires |
|-------|--------------|
| `on_session_start` | A new session begins |
| `on_session_end` | A session ends, whether it succeeded or errored |
| `pre_tool_use` | Just before a tool call executes |
| `post_tool_use` | Just after a tool call returns |

### Two hook types

#### Command hooks

A command hook runs a shell command. Meeseeks waits up to `timeout` seconds (default 30) for the process to finish before moving on. Use this when you want the hook to complete before the session continues — e.g. prepping a workspace at session start.

Meeseeks sets these environment variables on the subprocess:

| Variable | Available in | Value |
|----------|-------------|-------|
| `MEESEEKS_SESSION_ID` | `on_session_start`, `on_session_end` | Session identifier |
| `MEESEEKS_ERROR` | `on_session_end` (error cases only) | Error message string |
| `MEESEEKS_TOOL_ID` | `pre_tool_use`, `post_tool_use` | Tool identifier |
| `MEESEEKS_OPERATION` | `pre_tool_use`, `post_tool_use` | Operation name |
| `MEESEEKS_TOOL_RESULT` | `post_tool_use` | First 2 000 characters of the result |

Example — send a desktop notification when a session ends:

```json
"hooks": {
  "on_session_end": [
    {
      "type": "command",
      "command": "notify-send 'Meeseeks' \"Session $MEESEEKS_SESSION_ID done\"",
      "timeout": 5
    }
  ]
}
```

#### HTTP hooks

An HTTP hook posts a JSON body to a URL. HTTP hooks are non-blocking — Meeseeks does not wait for the response, and failures are logged rather than raised. Use this when you want to feed Meeseeks events into a webhook, audit log, or chat integration without slowing the agent down.

Payload for `on_session_end`:

```json
{
  "event": "session_end",
  "session_id": "abc123",
  "error": null
}
```

Payload for `post_tool_use`:

```json
{
  "event": "post_tool_use",
  "tool_id": "aider_shell_tool",
  "operation": "run",
  "result_preview": "stdout output (first 2000 chars)"
}
```

Example — notify an external webhook:

```json
"hooks": {
  "on_session_end": [
    {
      "type": "http",
      "url": "https://your-webhook.example.com/meeseeks",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" },
      "timeout": 10
    }
  ]
}
```

### Scoping hooks to specific tools

Add a `matcher` (an `fnmatch` pattern) to any hook entry to restrict which tool calls trigger it. This is the difference between logging every tool call and logging only shell commands:

```json
"hooks": {
  "post_tool_use": [
    {
      "type": "command",
      "command": "echo 'Shell command ran' >> /tmp/shell.log",
      "matcher": "aider_shell_tool"
    }
  ]
}
```

`"matcher": "mcp__*"` scopes the hook to every MCP tool. Omit `matcher` (or set to `null`) and the hook fires on every tool call.

### A fuller example

```json
"hooks": {
  "pre_tool_use": [],
  "post_tool_use": [
    {
      "type": "http",
      "url": "https://hooks.example.com/tool-events",
      "matcher": "aider_shell_tool",
      "timeout": 5
    }
  ],
  "on_session_start": [
    {
      "type": "command",
      "command": "logger -t meeseeks \"Session $MEESEEKS_SESSION_ID started\"",
      "timeout": 3
    }
  ],
  "on_session_end": [
    {
      "type": "command",
      "command": "/home/user/scripts/notify.sh",
      "timeout": 10
    }
  ]
}
```

---

## Reference

| Config key | Type | Default | Description |
|------------|------|---------|-------------|
| `permissions.policy_path` | string | `""` | Path to a JSON or TOML permission policy file. Empty uses built-in defaults. |
| `permissions.approval_mode` | string | `"ask"` | Session-wide shortcut: `allow`, `deny`, or `ask`. |
| `hooks.pre_tool_use` | list | `[]` | Hooks executed before each tool invocation. |
| `hooks.post_tool_use` | list | `[]` | Hooks executed after each tool invocation. |
| `hooks.on_session_start` | list | `[]` | Hooks executed when a new session begins. |
| `hooks.on_session_end` | list | `[]` | Hooks executed when a session ends. |

Each entry in a hooks list accepts:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | `"command"` | `"command"` or `"http"` |
| `command` | string | `""` | Shell command to run (`type=command`) |
| `url` | string | `""` | POST target (`type=http`) |
| `headers` | object | `{}` | Extra HTTP headers (`type=http`) |
| `matcher` | string \| null | `null` | fnmatch pattern on `tool_id`; `null` matches all |
| `timeout` | integer | `30` | Max seconds to wait for the hook |

See [configuration.md](configuration.md) for the full config schema.

> **How it works internally:** See [Architecture Overview → Permission policy](core-orchestration.md#permission-policy) and [Hook manager](core-orchestration.md#hook-manager).
