# Agents Guide - Mewbo

## MANDATORY: Hydrate context with Devin/DeepWiki before touching files

At the start of every conversation and every non-trivial task, call `ask_question` on `bearlike/Assistant` to understand the subsystem you'll work on. Try Devin Wiki (`mcp__devin__Devin-Wiki-Personal-ask_question`) first; fall back to DeepWiki (`mcp__deepwiki__Deepwiki-OSS-ask_question`). Use `read_wiki_structure` → `read_wiki_contents` for deeper exploration. Only read local files after hydration.

This applies to subagents too — include the directive in their prompts. There are multiple CLAUDE.md files in this monorepo. You must deliberately read the appropriate CLAUDE.md file required for the task. 

## What Mewbo is

An AI assistant modeled as a conversation state machine with a hierarchical agent hypervisor. Core engine: a single async `ToolUseLoop` bound to an LLM via native `bind_tools`. Child sessions are admitted via `spawn_agent`, tracked by `AgentHypervisor`, and resolved through structured concurrency into one of four terminal states (`completed`, `failed`, `cancelled`, `rejected`). Interfaces: CLI, web console, REST API, Home Assistant, Nextcloud Talk — all share the core engine.

For architecture details, query the wiki. Do not restate them here.

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
- **Hooks**: all invocations are try/excepted — failing hook logs warning, never blocks. Lifecycle: `on_session_start`, `on_session_end`, `on_compact`. Two hook types: `"command"` (shell subprocess) and `"http"` (fire-and-forget POST to URL in daemon thread). External hooks in `HooksConfig` filter by `fnmatch` tool matcher. `_session_env()` passes `MEWBO_SESSION_ID` and `MEWBO_ERROR` to command hooks. API server loads hooks from config via `HookManager.load_from_config()` and passes `hook_manager` to all `start_async()` calls.
- **Channel adapters**: `ChannelAdapter` Protocol (3 methods + `system_context` property) in `channels/base.py`. `ChannelRegistry` for lookup, `DeduplicationGuard` for replay protection. Shared `_process_inbound()` pipeline in `routes.py` handles dedup → mention gate → session resolve → commands → LLM for all channels. Webhook endpoint at `POST /api/webhooks/<platform>` (HMAC auth, not API key). Non-webhook channels (e.g. email) use `_process_inbound()` directly from their own poller. Channel sessions are standard API sessions — created via `session_store.create_session()`, mapped via session tags (`tag_session`/`resolve_tag`), visible in console/Langfuse. Completion callback reads `source_platform` from transcript context event and sends the final answer back via the adapter. Adapters may expose `requires_mention(message)` to dynamically skip mention gating (e.g. email skips mentions for 1-to-1 but requires `@Mewbo` in multi-party threads). Adapters: Nextcloud Talk (HMAC-SHA256, ActivityStreams 2.0, OCS Bot API) and Email (IMAP polling + SMTP reply with markdown→HTML rendering via mistune).
- **Structured errors**: `AgentError` captures `agent_id`, `depth`, `task`, `message`, `last_tool`, `steps`. Sub-agent cleanup cascades to children before unregistering.
- **Cleanup**: 3-phase (cancel → wait with timeout → force-mark as cancelled). `await_lifecycle_managers(timeout)` before event-loop teardown.
- **Forced synthesis**: on root step limit with pending non-blocking children, 2s grace period, then inject completed results as `SystemMessage` for synthesis; still-running agents warned in the prompt.
- **Fork from message**: `SessionRuntime.resolve_session(fork_from=..., fork_at_ts=...)` creates a new session with only events up to the given timestamp, enabling edit-and-regenerate from any point in the conversation.
- **Plugins**: `PluginsConfig` in `config.py` defines `registry_paths` and `marketplaces`. `plugins.py` handles discovery, install, uninstall, and marketplace reading. Plugins contribute agent definitions (via `agent_registry.py`), skills, hooks (format-translated to `HooksConfig`), and MCP tools. `load_all_plugin_components()` is called during session init in the orchestrator. CLI: `/plugins`. API: `GET/POST /api/plugins`, `GET/POST /api/plugins/marketplace`, `DELETE /api/plugins/<name>`. Console: `PluginsView`.
- **Web IDE**: opt-in per-session code-server containers managed by `IdeManager` / `IdeStore` in the API. Config: `agent.web_ide` (`WebIdeConfig`). Requires MongoDB. API: `POST/DELETE /api/sessions/{id}/ide`, `POST /api/sessions/{id}/ide/extend`. Console shows an "Open in Web IDE" button when enabled.
- **Proxy model prefix**: `LLMConfig.proxy_model_prefix` (default `"openai"`) is prepended to model names when routing through a proxy. Read by `build_chat_model()` in `llm.py`.
- **LSP**: `lsp_tool` (backed by pygls/lsprotocol, gracefully absent when not installed). Operations: `diagnostics`, `definition`, `references`, `hover`. Servers auto-discovered via `shutil.which`; spawned lazily per-session; passive diagnostics injected as a `_append_lsp_feedback` hook in `ToolUseLoop` after every file edit. Config: `agent.lsp.enabled` (default `true`) + `agent.lsp.servers` (per-server overrides or custom server definitions). Built-ins: pyright (Python), typescript-language-server (TS/JS), gopls (Go), rust-analyzer (Rust). Per-session shutdown via `shutdown_lsp_managers()`.

## Running, testing, linting

- Tests: `pytest` under `tests/`.
- Install: `uv sync` (core) or `uv sync --all-extras --all-groups` (dev).
- Run: `uv run mewbo` / `uv run mewbo-api` from repo root, or `npm run dev` in `apps/mewbo_console`.
- Global install: `uv tool install .`. Config chain: `CWD/configs/` → `$MEWBO_HOME/` → `~/.mewbo/`. Override with `--config`. Run `/init` to scaffold.
- Docker: `docker/` dir, compose supported.
- Lint: `ruff` (auto-fix: `.venv/bin/ruff check --fix .`). Types: `mypy`. Helpers: `make lint`, `make lint-fix`, `make typecheck`, `make precommit-install`.