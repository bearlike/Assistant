# Mewbo Tools — Integrations Guidance

Scope: `packages/mewbo_tools/src/mewbo_tools/` — the package that
implements heavyweight tools and external integrations: file edit, MCP
connection pool, LSP, Aider bridge, vendored third-party glue. Root
`CLAUDE.md` lists the entry-point files; this file documents the
non-obvious decisions.

**Layering (see root CLAUDE.md → "Monorepo layering"):** tools deps
`mewbo_core` only and imports strictly down — never an app or a capability
library. Reusable domain engines (graph/memory/embedding) are NOT tools;
they belong in a capability library (`mewbo_graph`).

## What lives here vs `mewbo_core`

`mewbo_core` owns the orchestration engine and the "core skills" that
ship with every Mewbo install. `mewbo_tools` owns tools that:

- Bring large external dependencies (tree-sitter, LSP servers,
  `langchain-mcp-adapters`, the Aider library).
- Implement complex integration patterns (MCP transport lifecycle,
  LSP server pool, file-edit conflict resolution).
- Are opt-in or environment-sensitive (LSP servers depend on
  `shutil.which`; MCP servers need configuration).

If your tool is a thin in-process function on Python state, it goes in
`mewbo_core/builtin_plugins/`. If it manages a long-lived subprocess
or remote connection, it goes here.

## MCP connection pool

`integration/mcp_pool.py:MCPConnectionPool` is the only MCP transport
layer. Use it; never spawn an MCP client one-off. The pool:

- Maintains a persistent connection per configured server.
- Auto-reconnects after 3 consecutive errors.
- Per-request 60s timeout.
- Detects config changes via fingerprint hash and reconnects on the
  next request.

The legacy one-shot client is kept as a fallback for environments
where the pool can't start (e.g. constrained sandboxes), but the pool
is the default. If a tool needs MCP, request it through the pool.

## File edit tools

`integration/edit_common.py` is the shared helper for both edit
implementations (`search_replace_block` and `structured_patch`). Both
emit `{"kind": "diff", ...}` results so downstream renderers don't
have to discriminate.

The active implementation is selected by `AgentConfig.edit_tool`. When
empty (default), `ToolUseLoop._configured_edit_tool_id()` auto-picks
based on model identity via `llm.model_prefers_structured_patch()`.
Keep this auto-select function in sync with provider behavior — some
models hallucinate the structured-patch shape when given the
search-replace tool and vice versa.

If you need to add a new edit tool variant, put the shared
diff-emission helpers in `edit_common.py` so all variants behave
identically downstream.

## LSP integration

`integration/lsp/` wraps pygls + lsprotocol. The tool is gracefully
absent when the libraries aren't installed — `LspTool` checks at
import time and skips registration. Per-session manager;
`shutdown_lsp_managers()` runs at session teardown.

Built-in server detection uses `shutil.which`. Operators can override
or add custom servers via `agent.lsp.servers` in app.json. Built-ins:

- pyright (Python)
- typescript-language-server (TS/JS)
- gopls (Go)
- rust-analyzer (Rust)

Passive diagnostics: after every file edit, `ToolUseLoop` runs an
`_append_lsp_feedback` hook that asks the LSP for diagnostics on the
edited file and appends them as a tool result message. This is what
catches "you forgot to import this" / "undefined name" without
explicitly invoking the LSP tool.

## Aider bridge

`aider_bridge/` exposes the Aider library as a Mewbo tool. Aider has
its own opinions about prompt engineering, edit format, and file
context — we don't fight them. The bridge is a thin shim. If a Mewbo
tool wants Aider's edit semantics, it goes through here.

## Vendored code

`vendor/` holds third-party code we copied in (license-compatible) and
can't easily depend on as a package — typically because we needed to
patch behavior or pin a version that no longer publishes wheels.

Rules for `vendor/`:
- Don't modify files in `vendor/` directly. Patch via a wrapper in the
  same package.
- Each vendored module must have a `VENDOR.md` at its root noting:
  upstream URL, version pinned, license, patches applied.

## Testing

Integration tests live in `tests/` (root) with mock I/O. MCP tests
mock at the `langchain_mcp_adapters` boundary. LSP tests mock at the
pygls boundary — never spawn real language servers in CI. File edit
tests use real string transforms against in-memory file contents.

## Pre-edit checklist

- [ ] Adding a new integration? Does it warrant its own subdirectory
      with a `VENDOR.md` (if vendored) or a brief `INTEGRATION.md`
      (if a network/subprocess integration)?
- [ ] Adding a new MCP transport feature? Did I add it to the pool
      rather than a parallel client?
- [ ] Adding a new file edit variant? Did I route diff emission
      through `edit_common.py`?
- [ ] Adding a new LSP server? Did I add detection via `shutil.which`
      AND a built-in server entry?
