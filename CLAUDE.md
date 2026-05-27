# Agents Guide - Mewbo

## MANDATORY: Hydrate context with Devin/DeepWiki before touching files

At the start of every conversation and every non-trivial task, call `ask_question` on `bearlike/Assistant` to understand the subsystem you'll work on. Try Devin Wiki (`mcp__devin__Devin-Wiki-Personal-ask_question`) first; fall back to DeepWiki (`mcp__deepwiki__Deepwiki-OSS-ask_question`). Use `read_wiki_structure` → `read_wiki_contents` for deeper exploration. Only read local files after hydration.

This applies to subagents too — include the directive in their prompts. There are multiple CLAUDE.md files in this monorepo. You must deliberately read the appropriate CLAUDE.md file required for the task.

> Caveat: the auto-generated wikis lag the repo and miss recent refactors (e.g. they don't know `mewbo_graph`/#25 and still cite a removed `mewbo_chat`). Use them for "how should this work" intuition, but verify anything structural (which packages/modules exist, where code lives) against local source — never trust the wiki's repo map.

## Where the per-subsystem CLAUDE.md files live

Nested folders document the non-trivial engineering decisions specific to their scope. Read the deepest one that applies before editing files there.

| Scope | File |
|---|---|
| Engine: tool-use loop, hypervisor, hooks, plugins, built-in tools | `packages/mewbo_core/CLAUDE.md` |
| Integrations: MCP pool, file edit, LSP, Aider, vendored | `packages/mewbo_tools/CLAUDE.md` |
| Capability library: graph/memory/embedding substrate + SCG engine + wiki/scg plugins (optional, extras-gated) | `packages/mewbo_graph/CLAUDE.md` |
| HTTP API server (routes, channels, hooks, Web IDE) | `apps/mewbo_api/CLAUDE.md` |
| MewboWiki — API side (indexing-pipeline glue, SSE, capability gating; substrate in `mewbo_graph.wiki`) | `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` |
| Agentic Search — API side (run lifecycle, event-log-as-stream, `SearchRunner` seam, source→tool scoping) | `apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md` |
| Agentic Search — SCG API glue (run/map-job lifecycle, `ScgConfig` gate; engine in `mewbo_graph.scg`) | `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md` |
| Web console (React, shadcn, TanStack Query, wouter) | `apps/mewbo_console/CLAUDE.md` |
| MewboWiki — Console side (atomic progress class, KG renderer, log pinning) | `apps/mewbo_console/src/components/wiki/CLAUDE.md` |
| MCP server: tools exposing Mewbo to external agents | `apps/mewbo_mcp/CLAUDE.md` |
| Agentic Search — Console side (SSE-driven reveal, shared `sse.ts`, optimistic run history, FE-side time labels) | `apps/mewbo_console/src/components/agentic_search/CLAUDE.md` |
| CLI (Rich/Textual display, agent panel) | `apps/mewbo_cli/CLAUDE.md` |
| Home Assistant conversation agent | `mewbo_ha_conversation/CLAUDE.md` |
| Test patterns + fixtures | `tests/CLAUDE.md` |

## What Mewbo is

An AI assistant modeled as a conversation state machine with a hierarchical agent hypervisor. Core engine: a single async `ToolUseLoop` bound to an LLM via native `bind_tools`. Child sessions are admitted via `spawn_agent`, tracked by `AgentHypervisor`, and resolved through structured concurrency into one of four terminal states (`completed`, `failed`, `cancelled`, `rejected`). Interfaces: CLI, web console, REST API, Home Assistant, Nextcloud Talk — all share the core engine.

For architecture details, query the wiki. Do not restate them here.

## Monorepo layering (separation of concerns) — read before adding a module

Dependencies flow **strictly down** this DAG; a lower layer must **never** import a higher one — not even a lazy in-function `try/except ImportError` (that guard is exactly the smell that masks an inversion):

`mewbo_core` (lean SDK) → `mewbo_tools` (integrations) · `mewbo_graph` (optional capability libs) → apps (`mewbo_api`, `mewbo_cli`, `mewbo_mcp`, …)

- **`mewbo_core`** — lean orchestration SDK: generic, dependency-light primitives only (loop, hypervisor, session/store bases, config, plugin registry incl. `register_builtin_root`). No product/graph code; heavy/optional third-party deps go behind a `mewbo-core[...]` extra, never the base install. (The embedder/graph/memory seams deliberately live in `mewbo_graph`, not core — keeping core leaner.)
- **`mewbo_tools`** — subprocess/remote integrations (MCP, LSP, file edit). Deps core only.
- **Capability libraries** (`mewbo_graph`, …) — reusable domain engines (graph, memory, embedding, search). **Optional + dependency-ignorable**: heavy deps behind the library's own extras + in-code import-guards. Deps core, down-only; never an app.
- **apps** — thin product surfaces: HTTP routes, wire contracts, transport, persistence, channel/MCP glue. They **compose** libraries; they never **host** a reusable engine.

**Placement rule.** Reusable substrate/domain engine → a library (core if generic + lean, else a capability lib). Orchestration primitive → core. Subprocess/integration → tools. HTTP route / wire contract / transport → an app. Two corollaries that prevent the recurring failure: **(1) a reusable engine must never live inside an app**, and **(2) two products must never import each other — extract the shared part into a library.**

**"Optional" means both layers:** PEP 621 extras (+ root extra-of-extra forwarding + Docker build-arg) **and** a graceful `try/except ImportError` at the import site → feature absent, never a crash. Plugins/AgentDefs ship with the library whose substrate they wrap, never in core where they'd import up — `mewbo_graph.plugins.{wiki,scg}` are the canonical examples (`widget_builder` is the in-core, zero-app-import template). A library above core registers its plugin root via `mewbo_core.plugins.register_builtin_root` (a *push* on import; core never imports up to discover it).

> The `mewbo_graph` extraction (**Gitea #25**) flipped all three former inversions — `builtin_plugins/{wiki,scg}` reaching up into `mewbo_api`, and Search reaching into Wiki — by moving the shared substrate + plugin suites **down** into the library and replacing the reach-ups with down-only seams (store singleton, `CloneTokenCache`, `MapPhaseSink` DI, `register_builtin_root`). Every edge now imports down; keep it that way.

## Key entry points (non-obvious only)

| Concern | File |
|---|---|
| Async tool-use loop | `packages/mewbo_core/src/mewbo_core/tool_use_loop.py` |
| Agent hypervisor & handles | `packages/mewbo_core/src/mewbo_core/hypervisor.py` |
| Sub-agent spawn + management tools | `packages/mewbo_core/src/mewbo_core/spawn_agent.py` |
| Skills (Agent Skills standard) | `packages/mewbo_core/src/mewbo_core/skills.py` |
| Plugin discovery + install | `packages/mewbo_core/src/mewbo_core/plugins.py` |
| Agent definition registry | `packages/mewbo_core/src/mewbo_core/agent_registry.py` |
| Session lifecycle, sync→async bridge | `packages/mewbo_core/src/mewbo_core/orchestrator.py` |
| Tool registry + `filter_specs()` | `packages/mewbo_core/src/mewbo_core/tool_registry.py` |
| Config (`AgentConfig`, `HooksConfig`, `PluginsConfig`) | `packages/mewbo_core/src/mewbo_core/config.py` |
| Compaction (FULL/PARTIAL modes) | `packages/mewbo_core/src/mewbo_core/compact.py` |
| Hook manager | `packages/mewbo_core/src/mewbo_core/hooks.py` |
| MCP server entry point + tool wiring | `apps/mewbo_mcp/src/mewbo_mcp/server.py` |
| Channel adapter abstraction | `apps/mewbo_api/src/mewbo_api/channels/base.py` |
| Nextcloud Talk adapter | `apps/mewbo_api/src/mewbo_api/channels/nextcloud_talk.py` |
| Email adapter + IMAP poller | `apps/mewbo_api/src/mewbo_api/channels/email_adapter.py` |
| Channel routes + shared pipeline | `apps/mewbo_api/src/mewbo_api/channels/routes.py` |
| MCP connection pool | `packages/mewbo_tools/src/mewbo_tools/integration/mcp_pool.py` |
| File edit tools + shared utils | `packages/mewbo_tools/src/mewbo_tools/integration/edit_common.py` |
| Web IDE manager + routes | `apps/mewbo_api/src/mewbo_api/ide.py`, `ide_routes.py` |
| LSP tool, manager, server defs | `packages/mewbo_tools/src/mewbo_tools/integration/lsp/` |

Use `rg` / `glob` for anything else.

## Debugging session errors (trace methodology)

When given a session URL (`/s/<session_id>`), work through these layers in order:

1. **MongoDB transcript** — authoritative event log. Query `db.events.find({session_id}).sort({ts:1})` via `MEWBO_MONGODB_URI` (port 27018). Check `tool_result.error`, `context.mcp_tools`, `completion.done_reason`.
2. **Langfuse traces** — LLM conversation chain. `fetch_traces(age=N)` → `fetch_observation(id)` on `GENERATION` to see system prompt, bound tool schemas, model reasoning. Trace-to-session: `trace_id == session_id`.
3. **Config** — `configs/app.json` (mounted read-only at `/app/configs/`), `docker.env` for secrets, MCP at `configs/mcp.json` (global) or `<project>/.mcp.json` (project).
4. **Docker env** — `docker-compose.yml` + override for mounts. API runs at `/app` with `MEWBO_HOME=/app/data`. Project dirs need identical host/container paths.

### Common root-cause signatures

| Symptom | Cause |
|---|---|
| `result: null, success: false` on shell/file tools | CWD missing in container (volume mount), or `root` not injected |
| `"Tool not available"` | LLM hallucinated a filtered-out built-in tool, or `tool_id` mismatch with registry |
| `"MCP server 'X' not found in config"` | `MCPToolRunner` loaded config without project CWD; project `.mcp.json` not merged |
| `done_reason: "max_steps_reached"` | Legacy: only in sessions before natural-completion loop refactor. Agents now run until natural completion. |
| Langfuse `sessionId: null` | `invoke_config["metadata"]` not propagated; check `langfuse_metadata` 3-line pattern in `tool_use_loop.py` |

## MCP tools — when to use each

**Devin Wiki / DeepWiki (`ask_question`, `read_wiki_structure`, `read_wiki_contents`)** — primary context source for this project (`bearlike/Assistant`) and external repos (up to 10 at once). Try Devin Wiki first, fall back to DeepWiki.

**Devin session tools (`devin_session_create`, `devin_session_interact`, `devin_session_events`, `devin_session_search`, `devin_session_gather`, `devin_knowledge_manage`, `devin_schedule_manage`)** — delegate long-running tasks, manage knowledge notes, schedule automated work. Session IDs need `devin-` prefix when reused.

**Langfuse (`mcp__langfuse__*`)** — observability. Standard path: `get_error_count(age)` → `fetch_sessions(age)` → `fetch_traces(age, session_id, name)` → `fetch_trace(id, include_observations=true)` → `fetch_observation(id)`. Trace names: `mewbo-tool-use`, `mewbo-task-master`, `mewbo-context`. `age` is in minutes (max 10080). For large payloads use `output_mode="full_json_file"`.

**SearXNG (`searxng_web_search`) + `web_url_read`** — current events, docs, error messages, anything outside codebase/wikis.

**Context7 (`resolve_library_id` → `query_docs`)** — library/framework API docs (LangChain, Pydantic, LiteLLM, Textual, etc).

Fire MCP calls in parallel during investigations. Devin/DeepWiki tells you "how should it work"; Langfuse tells you "how did it actually work."

## Engineering principles

**KISS & DRY are the core philosophy.** Bias toward less code, not more. Before writing anything custom, search for an existing library or existing utility in the codebase. Precedents already set: LiteLLM for LLM calls, Rich/Textual for terminal UI, Pydantic for validation, Flask-RESTX for the API, Langfuse for tracing, Jinja2 for prompts, Tiktoken for tokens, Loguru for logging, langchain-mcp-adapters for MCP, PyMongo for Mongo. **Pattern: proven library for infrastructure, custom code only for business logic.**

Other rules:
- Write code that validates itself at the point of definition (Pydantic `field_validator`, `ConfigDict(extra="forbid")`).
- Define logic once; call everywhere (e.g. `filter_specs()` is reused by spawn_agent, skills, and the API).
- Smallest diff that solves the problem.
- No speculative abstractions.
- Keep tool contracts stable: `AbstractTool`, `ActionStep`, `TaskQueue`, and field names `tool_id` / `operation` / `tool_input`.
- Tests prefer real code paths; stub only I/O boundaries. Cover the full orchestration loop with fake tools and fake LLM outputs.
- Type hints stay precise; avoid `Any`.
- Gitmoji + Conventional Commits (`✨ feat: ...`). See `.github/git-commit-instructions.md`.
- Do not push unless explicitly asked.
- Treat LLMs as non-deterministic black-box APIs; avoid anthropomorphic language.

## Project instructions loading

`discover_all_instructions()` loads four priority levels (low→high): user `~/.claude/CLAUDE.md`, project `CLAUDE.md` / `.claude/CLAUDE.md` walking up to git root, rules `.claude/rules/*.md`, local `CLAUDE.local.md`.

Subtree discovery (`discover_subtree_instructions()`, max depth 5) walks DOWN from CWD to find nested `CLAUDE.md` / `AGENTS.md` / `.claude/CLAUDE.md` and indexes them (not injected — model reads on demand). Prunes `node_modules`, `__pycache__`, `.venv`, hidden dirs. Same mechanism for `.claude/skills/*/SKILL.md`.

Add `<!-- mewbo:noload -->` on line 1 to skip a file (marker: `_NOLOAD_MARKER` in `common.py`). Git context is injected via `get_git_context()`.

## Orchestration invariants

These are *rules*, not explanations. For background, query the wiki.

- **Single async loop**: `ToolUseLoop.run()` is the only execution engine. No separate planner/executor/synthesizer.
- **Edit tool is configurable**: `AgentConfig.edit_tool` selects `"search_replace_block"` or `"structured_patch"`. Both share `edit_common.py` and emit `{"kind": "diff", ...}`. When `edit_tool` is empty (default), `ToolUseLoop._configured_edit_tool_id()` auto-selects based on model identity via `llm.model_prefers_structured_patch()`.
- **Tool scoping**: `filter_specs()` applies allowlist/denylist. API passes `context.mcp_tools` as `allowed_tools` through `SessionRuntime` → `Orchestrator` → `ToolUseLoop`.
- **Sub-agent spawn signature**: `spawn_agent(task, model, allowed_tools, denied_tools, acceptance_criteria)`. `max_steps` is deprecated and not enforced — agents run until natural completion. Inherits parent's `approval_callback` so write/edit/shell tools work in API/headless contexts.
- **Non-blocking root spawn** (depth=0): returns `{agent_id, status: "submitted"}` immediately. `_run_child_lifecycle` stores `AgentResult` on the `AgentHandle` and calls `send_to_parent()`. Non-root agents use blocking spawn and return the `AgentResult` as JSON in a `ToolMessage`.
- **Root-only tools** (depth=0): `check_agents(wait?, timeout?)` (blocks if `wait=true` until a child completes) and `steer_agent(agent_id, action, message?)` (NL steering to a running agent's queue, or cancel; supports agent_id prefix matching).
- **`AgentResult` fields**: `content`, `status`, `steps_used`, `summary`, `warnings`, `artifacts`.
- **6-state lifecycle**: `submitted → running → {completed, failed, cancelled, rejected}`.
- **Hypervisor** = reflexes (code-level watchdog, zero tokens, checks every 30s) + brain (root LLM sees `render_agent_tree()` in its system prompt each turn, with `progress_note` and result `summary`). Budget enforcement is graduated: NL warnings via `SystemMessage`, never force-kill. Admission control via `max_concurrent` Semaphore.
- **Progress updates**: non-root agents auto-write `AgentHandle.progress_note` each step (`"step N: tool_id -> snippet"`); zero token cost. Root reads via `check_agents` or the agent tree.
- **Bidirectional messaging**: parent→child via `send_message(agent_id, text)` or `steer_agent` tool. Child→parent via `send_to_parent(child_id, text)`. Messages drain as `HumanMessage` between steps.
- **Depth-role prompting** (`_build_depth_guidance()`):
  - Root (depth=0): hypervisor — direct execution by default, async delegation when spawning, steer/cancel running agents, synthesize results.
  - Sub-orchestrator: delegated agent with bounded scope; may further delegate.
  - Leaf: executor — complete task directly, self-terminate, admit failure explicitly.
- **Compaction resilience**: agent tree lives in the system prompt (rebuilt each step), `check_agents` reads live `AgentHandle` state, child results stored on `AgentHandle.result` — all survive compaction.
- **Max depth 5**. At max depth, `spawn_agent` is removed from the tool schema entirely.
- **User steering**: root has `message_queue` (thread-safe `queue.Queue`) and `interrupt_step` (`threading.Event`), drained between steps as `HumanMessage`. Exposed via `/message` and `/interrupt`. Created in `RunRegistry.start()`.
- **Attachments**: `ContextBuilder` reads uploaded text files, injects into `ContextSnapshot.attachment_texts`.
- **Planning is root-only**. Sub-agents always execute (act mode), bypassing `Orchestrator`.
- **Skills**: discovered from `~/.claude/skills/` and `.claude/skills/` following the Agent Skills standard. Catalog injected into the system prompt for auto-invocation via `activate_skill`. User `/skill-name` detected in `Orchestrator` and rendered into `skill_instructions`. Skills scope tools via `allowed-tools` (reuses `filter_specs()`) and preprocess shell via `` !`cmd` `` syntax.
- **Concurrency**: `_partition_tool_calls()` batches `concurrency_safe` tools and isolates exclusive ones. Per-tool `asyncio.wait_for(spec.timeout)`, default 120s. Timeouts don't cancel siblings.
- **MCP pool**: `MCPConnectionPool` (persistent, auto-reconnect after 3 consecutive errors, 60s per-request timeout, config change detection). Legacy one-shot client is fallback.
- **Compaction modes**: `FULL` or `PARTIAL` (default for auto-compact). Structured summary prompt with `<analysis>` and `<summary>` sections. Post-compact file restoration within token budgets.
- **LLM call resilience** (`llm_resilience.py`): `tool_use_loop` drives a per-run `RetryStrategy` — an atomic object holding the token-bucket retry budget + per-model circuit breaker + policy knobs, built via `from_config()` and fed injected invoke/emit/compact (so the state machine is testable without a model). Error classification (`RetryStrategy.classify`) is **3-way, not binary**: `retry_same` (timeout/5xx/conn/unknown → full-jitter backoff), `switch_model` (quota / "no deployments" / auth / context-window — hopeless on *this* model, recoverable on another), `fatal` (malformed 400 / permission / deterministic / cancellation — never retried; cancellation bubbles up). Fallback is **opt-in** (`llm.fallback.enabled`; legacy `llm.fallback_models` still honored via `effective_fallback_models()`); with fallback off, a `switch_model` error fails fast → clean `error`/`halted_no_progress` completion → one-click recovery via `resolve_recovery_query`. **Idempotency invariant**: the response is appended to `messages` only *after* success, so a retry never duplicates a tool call or replays a partial generation — never append a failed/partial assistant turn. Compaction's recent-tail slice can orphan a `tool_use`/`tool_result` pair → `repair_tool_pairing` rebalances it (the model API rejects an unbalanced pair otherwise). Doom-loop halt fires at `agent.retry.doom_loop_threshold` identical tool+input calls. Defaults are calibrated from production agent loops (timeout 120s, 3 attempts, base 1s/cap 60s full-jitter, 240s turn deadline, breaker 3-fails/30s), deliberately looser than vendor-SDK defaults (0.5s/8s/2) — agent turns need longer waits for capacity provisioning.
- **Hooks**: all invocations are try/excepted — failing hook logs warning, never blocks. Lifecycle: `on_session_start`, `on_session_end`, `on_compact`. Two hook types: `"command"` (shell subprocess) and `"http"` (fire-and-forget POST to URL in daemon thread). External hooks in `HooksConfig` filter by `fnmatch` tool matcher. `_session_env()` passes `MEWBO_SESSION_ID` and `MEWBO_ERROR` to command hooks. API server loads hooks from config via `HookManager.load_from_config()` and passes `hook_manager` to all `start_async()` calls.
- **Channel adapters**: `ChannelAdapter` Protocol (3 methods + `system_context` property) in `channels/base.py`. `ChannelRegistry` for lookup, `DeduplicationGuard` for replay protection. Shared `_process_inbound()` pipeline in `routes.py` handles dedup → mention gate → session resolve → commands → LLM for all channels. Webhook endpoint at `POST /api/webhooks/<platform>` (HMAC auth, not API key). Non-webhook channels (e.g. email) use `_process_inbound()` directly from their own poller. Channel sessions are standard API sessions — created via `session_store.create_session()`, mapped via session tags (`tag_session`/`resolve_tag`), visible in console/Langfuse. Completion callback reads `source_platform` from transcript context event and sends the final answer back via the adapter. Adapters may expose `requires_mention(message)` to dynamically skip mention gating (e.g. email skips mentions for 1-to-1 but requires `@Mewbo` in multi-party threads). Adapters: Nextcloud Talk (HMAC-SHA256, ActivityStreams 2.0, OCS Bot API) and Email (IMAP polling + SMTP reply with markdown→HTML rendering via mistune).
- **Structured errors**: `AgentError` captures `agent_id`, `depth`, `task`, `message`, `last_tool`, `steps`. Sub-agent cleanup cascades to children before unregistering.
- **Cleanup**: 3-phase (cancel → wait with timeout → force-mark as cancelled). `await_lifecycle_managers(timeout)` before event-loop teardown.
- **Forced synthesis**: on root step limit with pending non-blocking children, 2s grace period, then inject completed results as `SystemMessage` for synthesis; still-running agents warned in the prompt.
- **Fork from message**: `SessionRuntime.resolve_session(fork_from=..., fork_at_ts=...)` creates a new session with only events up to the given timestamp, enabling edit-and-regenerate from any point in the conversation.
- **Plugins**: `PluginsConfig` in `config.py` defines `registry_paths` and `marketplaces`. `plugins.py` handles discovery, install, uninstall, and marketplace reading. Plugins contribute agent definitions (via `agent_registry.py`), skills, hooks (format-translated to `HooksConfig`), and MCP tools. `load_all_plugin_components()` is called during session init in the orchestrator. CLI: `/plugins`. API: `GET/POST /api/plugins`, `GET/POST /api/plugins/marketplace`, `DELETE /api/plugins/<name>`. Console: `PluginsView`.
- **Web IDE**: opt-in per-session code-server containers managed by `IdeManager` / `IdeStore` in the API. Config: `agent.web_ide` (`WebIdeConfig`). Requires MongoDB. API: `POST/DELETE /api/sessions/{id}/ide`, `POST /api/sessions/{id}/ide/extend`. Console shows an "Open in Web IDE" button when enabled.
- **Proxy model prefix**: `LLMConfig.proxy_model_prefix` (default `"openai"`) is prepended to model names when routing through a proxy. Read by `build_chat_model()` in `llm.py`. The wiki's embedder follows the same rule — bare model names get an `openai/` prefix so LiteLLM routes through the proxy instead of a provider SDK.
- **MewboWiki**: DeepWiki-style auto-generated wikis. Six-phase pipeline `clone → scan → graph → plan → pages → finalize` driven by `emit_phase(ctx, name)` which writes the SSE event AND persists `phase` + `phase_started_at` on the `IndexingJob` snapshot — single source of truth for both the SSE-driven indexing page and the snapshot-polling landing card. Capability-gated: session must advertise `client_capabilities: ["wiki"]` for the `wiki-indexer` / `wiki-page-writer` / `wiki-qa` AgentDefs to appear in `spawn_agent` lookups. Embeddings route through `litellm.embedding` (NOT `langchain-openai` — version conflict with `openai==2.24.0` that litellm pins). FE has a `progress.ts:IndexingProgress` atomic class used by both screens — never compute progress fractions locally. See `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` and `apps/mewbo_console/src/components/wiki/CLAUDE.md` for the full non-obvious-decision list.
- **SCG (Agentic Search)**: agent-driven routing over the Source Capability Graph (indexes *reachability* — schemas/pathways, never data). The deterministic graph ops (route / parse / ER) are tools the `scg-search` AgentDef drives — never a parallel control loop (the one engine stays `ToolUseLoop`); tiers (Fast/Auto/Deep) are one decomposition+probe budget knob; the connector's real return is the only verifier. Flag-gated on `scg.enabled`. See `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md`.
- **LSP**: `lsp_tool` (backed by pygls/lsprotocol, gracefully absent when not installed). Operations: `diagnostics`, `definition`, `references`, `hover`. Servers auto-discovered via `shutil.which`; spawned lazily per-session; passive diagnostics injected as a `_append_lsp_feedback` hook in `ToolUseLoop` after every file edit. Config: `agent.lsp.enabled` (default `true`) + `agent.lsp.servers` (per-server overrides or custom server definitions). Built-ins: pyright (Python), typescript-language-server (TS/JS), gopls (Go), rust-analyzer (Rust). Per-session shutdown via `shutdown_lsp_managers()`.

## Running, testing, linting

- Tests: `pytest` under `tests/`.
- Install: `uv sync` (core) or `uv sync --all-extras --all-groups` (dev).
- Run: `uv run mewbo` / `uv run mewbo-api` from repo root, or `npm run dev` in `apps/mewbo_console`.
- Global install: `uv tool install .`. Config chain: `CWD/configs/` → `$MEWBO_HOME/` → `~/.mewbo/`. Override with `--config`. Run `/init` to scaffold.
- Docker: `docker/` dir, compose supported.
- Lint: `ruff` (auto-fix: `.venv/bin/ruff check --fix .`). Types: `mypy`. Helpers: `make lint`, `make lint-fix`, `make typecheck`, `make precommit-install`.