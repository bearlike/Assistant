> ↑ [root /CLAUDE.md](../../CLAUDE.md)

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

### Non-blocking init — a slow/dead server never stalls (Gitea #130)

A slow or unreachable MCP server (cold container, dead host, hung
`tools/list`) must never block startup or an unrelated tool call. Reducing
the connect timeout is **not** the fix — any server can legitimately take
arbitrary time. The pool enforces this with four cooperating seams:

- **Non-blocking startup (Phase 1, `tool_registry._ensure_auto_manifest`).**
  Discovery is gated on a stored `config_hash`: when the cached manifest was
  built from the *same* MCP config, startup reuses it and does **no** live
  connect. The pool then connects **lazily on first use**. A config edit
  changes the hash and re-triggers discovery; `/mcp` refresh deletes the
  manifest + `reset_mcp_pool()` to force a re-probe (the "I fixed my server,
  reconnect now" path — also clears backoff/quarantine state).
- **Deferred wait-on-first-use (Phase 2).**
  `refresh_if_config_changed(..., connect=False)` updates config + prunes
  removed/changed servers but **never eagerly dials** — the connect is
  deferred to `get_or_connect(server)` for the one server actually needed.
  This is the natural place the on-demand `tool_search` round-trip (#131)
  waits for a still-connecting server.
- **Generalized quarantine + backoff (Phase 3, `_record_failure`).**
  Every connect failure is unwrapped (`unwrap_exception_group` peels the
  opaque anyio `TaskGroup` wrapper — reused by #132) and classified
  (`classify_connect_failure` → auth/config/dns/refused/timeout/other).
  **Auth/config never auto-retry** — they quarantine until the config hash
  changes (`skip_reason`). Transient causes get **exponential, capped
  backoff** (`next_retry_at`); a server inside its backoff window fast-fails
  in `get_or_connect` instead of being re-dialed. `status_snapshot()` surfaces
  connected/backoff/quarantined/failed/pending in `/mcp`.
- **No per-tool-call churn (Phase 4, `MCPToolRunner._invoke_via_pool`).**
  A server that `pool.is_connected()` skips the config reload + refresh
  entirely, so a healthy tool call never re-dials every other (possibly dead)
  server mid-query.

Deferred to a follow-up (Phase 5, stretch): MCP `notifications/tools/
list_changed` re-listing and a per-server `required: true` hard-fail flag.

## File edit tools

`integration/edit_common.py` is the shared helper for both edit
implementations (`search_replace_block` and `structured_patch`). Both
emit `{"kind": "diff", ...}` results so downstream renderers don't
have to discriminate.

The active implementation is selected by `AgentConfig.edit_tool`. When
empty (default), `ToolUseLoop._configured_edit_tool_id()` auto-picks
based on the ACTIVE model identity via `llm.model_prefers_structured_patch()`.
**The model→variant decision is controllable DATA, not code (#113):** that
function now reads `mewbo_core/prompts/model_variants.yaml` (via
`ModelVariantRegistry`), where the gpt-5/o3/o4/codex/gpt-4 → structured_patch
defaults were migrated. Onboard a model or flip its preferred variant by editing
that file (longest-prefix match), not Python; `llm.structured_patch_models` config
still overrides on top. Selection reads the active model, so a #54-escalated model
gets ITS variant. Pair a variant with a per-model prompt nudge via a `kind: model`
override on the `file.tools.*` entry under the SAME prefix.

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

## Inline `@<ref>` expansion + file catalog (#119/#124)

`integration/reference_expansion.py:ReferenceExpander` is the submit-time
preprocessor that expands `@file`/`@dir/`/`@diff`/`@https://…` tokens in a user
message into bounded inline context blocks (truncate-not-reject caps, dedupe, no
recursion, unresolved → literal). It lives HERE, not in an app, on purpose: BOTH
the API and the in-process CLI invoke it at their own submit seams, and an app
can't import another app — so the reusable engine sits one layer down. Each app
calls the thin `expand_references(text, cwd, attachments=)` helper; the API also
builds the session's attachment map first. It composes existing renderers
(`mewbo_core.attachments.parse_to_markdown` for docs+URLs, `git diff HEAD`,
`FileCatalog`) — never a parser per type.

`integration/file_catalog.py:FileCatalog` is the single "what files belong to
this project?" authority, git-index first (`git ls-files --cached --others
--exclude-standard` → tracked + new-but-not-`.gitignore`d; worktree-safe via
`-C`), bounded-walk fallback for non-git dirs. It backs THREE agreeing callers:
the expander's scoping gate (`contains`), the API's `/api/files` autocomplete
endpoint, and the CLI completer (`list_files`). `.gitignore`d secrets/artifacts
are out of scope by construction. Both classes never raise — a missing repo /
absent `git` / unreadable tree degrades to empty, never a broken request.

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
