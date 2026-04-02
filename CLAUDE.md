# Agents Guide - Personal Assistant (Meeseeks)

> **⚠️ MANDATORY — HYDRATE CONTEXT WITH DEEPWIKI FIRST ⚠️**
>
> Before reading files, writing code, or even asking clarifying questions — **use DeepWiki to hydrate yourself with project context**. This is non-negotiable. DeepWiki (`mcp__deepwiki__Deepwiki-OSS-ask_question` on `bearlike/Assistant`) gives you an instant architecture map, deep relationship understanding between components, and answers about how subsystems interact — all without reading a single file. **Do this at the start of every conversation and every non-trivial task.**
>
> Quick-start: `ask_question` with your task context → then `read_wiki_structure` to find relevant sections → then `read_wiki_contents` for details. Only after this should you touch local files.

## What this codebase is
Meeseeks is a multi-agent LLM personal assistant with an async sub-agent hypervisor. The core engine uses a single async `ToolUseLoop` that the LLM drives via native `bind_tools` / `tool_use`. Sub-agents are spawned via a `spawn_agent` tool, tracked by an `AgentHypervisor`, and cleaned up via structured concurrency. It ships multiple interfaces (CLI, web console, REST API, Home Assistant) that share the same core engine.

## Core entry points
- `packages/meeseeks_core/src/meeseeks_core/tool_use_loop.py`: async tool-use conversation loop (`ToolUseLoop`) — the core execution engine
- `packages/meeseeks_core/src/meeseeks_core/agent_context.py`: `AgentContext` (immutable per-agent state)
- `packages/meeseeks_core/src/meeseeks_core/hypervisor.py`: `AgentHypervisor` (control plane), `AgentHandle` (per-agent runtime state)
- `packages/meeseeks_core/src/meeseeks_core/spawn_agent.py`: `SpawnAgentTool` + `SPAWN_AGENT_SCHEMA` — sub-agent creation with tool scoping
- `packages/meeseeks_core/src/meeseeks_core/skills.py`: `SkillSpec`, `SkillRegistry`, `discover_skills()`, `activate_skill()`, `ACTIVATE_SKILL_SCHEMA` — Agent Skills standard support
- `packages/meeseeks_core/src/meeseeks_core/orchestrator.py`: session lifecycle, sync→async bridge via `asyncio.run()`
- `packages/meeseeks_core/src/meeseeks_core/task_master.py`: `generate_action_plan` + `orchestrate_session` entry points
- `packages/meeseeks_core/src/meeseeks_core/classes.py`: `ActionStep` (tool_id/operation/tool_input), `TaskQueue`, `AbstractTool` contracts, `ToolResult` (structured tool execution result)
- `packages/meeseeks_core/src/meeseeks_core/planning.py`: `Planner`, `PromptBuilder`
- `packages/meeseeks_core/src/meeseeks_core/session_runtime.py`: session lifecycle, listing, user steering (`enqueue_message`, `interrupt_step`)
- `packages/meeseeks_core/src/meeseeks_core/session_store.py`: transcript storage, tags, archive state, and `session_dir()` for attachment paths
- `packages/meeseeks_core/src/meeseeks_core/context.py`: `ContextBuilder`, `ContextSnapshot` (includes `attachment_texts` for uploaded file content)
- `packages/meeseeks_core/src/meeseeks_core/tool_registry.py`: `ToolRegistry`, `ToolSpec` (typed fields: `concurrency_safe`, `read_only`, `max_result_chars`, `timeout`), `filter_specs()` (reusable allowlist/denylist filtering), `load_registry()`
- `packages/meeseeks_core/src/meeseeks_core/config.py`: `AppConfig` including `AgentConfig` (max_depth, max_concurrent, allowed_models, etc.), `HooksConfig` (external hook configuration), and `resolve_meeseeks_home()` / `_resolve_config_path()` for location-independent config discovery
- `packages/meeseeks_core/src/meeseeks_core/compact.py`: `CompactionMode`, `CompactionResult`, `compact_conversation()` — two-mode (full/partial) context compaction with structured summaries and post-compact file restoration
- `packages/meeseeks_core/src/meeseeks_core/hooks.py`: `HookManager` — error-isolated hook execution with lifecycle hooks (`on_session_start`, `on_session_end`, `on_compact`), external command hooks via `HooksConfig`, and `fnmatch`-based tool matcher filtering
- `packages/meeseeks_tools/src/meeseeks_tools/integration/mcp_pool.py`: `MCPConnectionPool` — persistent MCP connection manager with memoized connections, error-based reconnection, and config change detection
- `packages/meeseeks_tools/src/meeseeks_tools/`: tool implementations and integration glue
- `apps/meeseeks_console/`: Web console (React + Vite, connects via REST API)
- `apps/meeseeks_api/src/meeseeks_api/backend.py`: Flask API
- `apps/meeseeks_cli/src/meeseeks_cli/cli_master.py`: terminal CLI with Rich Live agent display
- `meeseeks_ha_conversation/`: Home Assistant integration

## How to get context fast

**DeepWiki is your primary context source. Use it before touching local files.**

1. **DeepWiki first (always)**: Use `ask_question` on `bearlike/Assistant` to understand the area you're about to work on. Ask about architecture, data flow, component relationships, and hidden dependencies. This is faster and more comprehensive than reading files piecemeal.
2. **DeepWiki wiki structure**: Use `read_wiki_structure` on `bearlike/Assistant` to discover what sections exist, then `read_wiki_contents` to read specific sections relevant to your task.
3. **Cross-repo context**: When your task involves external libraries or integrations, use `ask_question` with multiple repos (up to 10) to understand compatibility and relationships between projects.
4. Read `README.md` and component READMEs for configuration/runtime details.
5. Use `rg` to locate specific behavior and follow the exact file path.
6. For CI issues, use GitHub Actions logs (GH CLI or MCP GitHub tools).

**Example hydration workflow** (do this at conversation start):
```
# 1. Ask DeepWiki about the area you're working on
ask_question(repo="bearlike/Assistant", question="How does the tool-use loop interact with the agent hypervisor?")

# 2. Browse wiki structure for related sections
read_wiki_structure(repoName="bearlike/Assistant")

# 3. Read specific sections
read_wiki_contents(repoName="bearlike/Assistant", page="...")

# 4. NOW read local files with full context
```

## MCP tools (use first — for both internal and external context)
**DeepWiki is not just for external repos — it is the fastest way to understand THIS project too.** Use `ask_question` on `bearlike/Assistant` before diving into local files. It understands component relationships, data flows, and architectural decisions that you would otherwise need to read dozens of files to piece together. When you need external context (other repos, CI failures, specs, APIs), prefer MCP tools instead of guessing.

### DeepWiki (`mcp__deepwiki__Deepwiki-OSS-*`) — YOUR PRIMARY CONTEXT HYDRATION TOOL
Fast AI-powered Q&A about any public GitHub repository without cloning or loading large files. **This is the single most valuable tool for understanding this codebase quickly.** Use it at the start of every task to build a mental model before touching code.

- **`ask_question`**: Ask any question about a repo and get a grounded, cited answer. Supports passing a single repo or a list of up to 10 repos for cross-repo questions. **Use this as your first action** when starting any non-trivial task — ask about the subsystem you're about to modify, its dependencies, and how it connects to other components.
- **`read_wiki_structure`**: Get the table of contents for a repo wiki. Use this to discover what sections exist and find relevant deep-dives. Pass `repoName` in `owner/repo` format (e.g., `bearlike/Assistant`, `anthropics/claude-code`).
- **`read_wiki_contents`**: Get the full wiki page content for a repo. Use after `read_wiki_structure` to read specific sections for detailed context.
- **When to use**:
  - **Start of every conversation**: Hydrate yourself with architecture context before reading files.
  - **Before modifying any subsystem**: Ask how it works, what depends on it, and what invariants it maintains.
  - **Cross-repo understanding**: Compare implementations across repos, check compatibility between libraries, understand how external projects work.
  - **Debugging**: Ask about expected behavior of a component before investigating what went wrong.
- **Tip**: Start with `ask_question` for targeted context (e.g., "How does the ToolUseLoop handle sub-agent spawning?"), then use `read_wiki_structure` → `read_wiki_contents` for broader exploration. For this project, always use `bearlike/Assistant`.

### Devin Wiki (`mcp__devin__Devin-Wiki-Personal-*`)
Devin-hosted wiki with the same structure as DeepWiki but from Devin's index. Also provides session management, knowledge notes, and scheduling.
- **`read_wiki_structure`** / **`read_wiki_contents`** / **`ask_question`**: Same as DeepWiki but uses Devin's index. Use `bearlike/Assistant` for this project.
- **`devin_session_create`**: Spawn child Devin sessions for complex tasks. Pass `sessions: [{prompt: "...", title: "..."}]`. Returned `session_id` values need `devin-` prefix for subsequent calls.
- **`devin_session_interact`**: Interact with a running session — `action: "get"` (status), `"message"` (send message), `"terminate"`, `"archive"`, `"get_messages"`, `"get_attachments"`, `"set_tags"`. Always include the `devin-` prefix on `session_id`.
- **`devin_session_events`**: Inspect session event timeline — `action: "list"` (summaries), `"details"` (full content), `"search"` (full-text). Filter by `categories` (shell, file, browser, git, message, etc.) or `event_types`.
- **`devin_session_search`**: Find sessions by tags, date range, origin, playbook, or user. Returned IDs need `devin-` prefix.
- **`devin_session_gather`**: Wait for multiple child sessions to settle (finish/error/suspend). Pass `session_ids` with `devin-` prefix. Max timeout 600s.
- **`devin_knowledge_manage`**: Manage knowledge notes — `action: "list"`, `"get"`, `"create"`, `"update"`, `"delete"`, `"folders"`. Also `"list_suggestions"`, `"view_suggestion"`, `"dismiss_suggestions"` for pending knowledge suggestions.
- **`devin_schedule_manage`**: Schedule recurring or one-time sessions — `action: "list"`, `"get"`, `"create"`, `"update"`, `"delete"`. Supports cron expressions via `frequency`.
- **When to use**: Same scenarios as DeepWiki, plus delegating long-running tasks to Devin, managing knowledge bases, and scheduling automated work.

### Langfuse (`mcp__langfuse__Langfuse-*`)
Observability platform for LLM traces. Meeseeks instruments all LLM calls with Langfuse. Use these tools to investigate orchestration behavior, debug regressions, and audit LLM call patterns.

#### Investigation workflow (most common path)
1. **Start broad**: `get_error_count(age=1440)` to check if there are recent errors (last 24h).
2. **List recent sessions**: `fetch_sessions(age=1440)` to find Meeseeks session IDs.
3. **List traces for a session**: `fetch_traces(age=1440, session_id="...", name="meeseeks-tool-use")` to find tool-use loop traces, or `name="meeseeks-task-master"` for planning traces.
4. **Inspect a trace**: `fetch_trace(trace_id="...", include_observations=True)` to see all LLM calls within a trace, including prompts, completions, token counts, and latency.
5. **Drill into a specific LLM call**: `fetch_observation(observation_id="...")` to inspect a single generation's input/output.
6. **Check exceptions**: `get_exception_details(trace_id="...")` when a trace has errors.

#### Key tools
- **`get_error_count(age)`**: Quick health check — returns count of traces with exceptions in the last N minutes (max 10080 = 7 days).
- **`fetch_sessions(age)`**: List Langfuse sessions. Meeseeks sessions map to Langfuse sessions via the session ID in `orchestrator.py`.
- **`get_session_details(session_id, include_observations=True)`**: Deep-dive into a session with all its traces and observations.
- **`fetch_traces(age, ...)`**: Find traces by name, user_id, session_id, tags, or metadata. Key trace names in Meeseeks: `meeseeks-tool-use` (main tool-use loop), `meeseeks-task-master` (planning), `meeseeks-context` (context selection).
- **`fetch_trace(trace_id, include_observations=True)`**: Full trace with all child observations. Use `output_mode="full_json_file"` for large traces.
- **`fetch_observations(age, type="GENERATION")`**: Find all LLM generations in a time window. Filter by `name`, `user_id`, `trace_id`, or `parent_observation_id`.
- **`fetch_observation(observation_id)`**: Single observation detail — includes full input/output, model name, token usage, latency.
- **`get_exception_details(trace_id)`**: Extract exception info from a failed trace.
- **`find_exceptions(age)` / `find_exceptions_in_file(age)`**: Broader exception search across all traces.
- **`list_prompts` / `get_prompt(name)` / `get_prompt_unresolved(name)`**: Manage Langfuse prompt registry (separate from the local `.txt` prompt files).
- **`create_text_prompt` / `create_chat_prompt`**: Create new prompt versions in Langfuse.
- **`list_datasets` / `get_dataset` / `list_dataset_items` / `create_dataset` / `create_dataset_item`**: Manage evaluation datasets for testing orchestration quality.
- **`get_data_schema`**: Discover the Langfuse data schema for advanced queries.
- **`get_user_sessions(user_id)`**: Find all sessions for a specific user.
- **Output modes**: All fetch tools support `output_mode`: `"compact"` (default, summarized), `"full_json_string"` (raw JSON), `"full_json_file"` (saves to disk + returns summary). Use `"full_json_file"` for large payloads.
- **When to use**: Debugging orchestration issues (too many LLM calls, wrong tool selection, plan inflation), measuring latency/token usage, auditing prompt quality, comparing before/after behavior changes.

### Internet Search — SearXNG (`mcp__internet-search__Internet-Search-searxng_web_search`)
Self-hosted SearXNG instance for broad web search.
- Pass `query` (required). Optional: `language`, `time_range` (`"day"`, `"month"`, `"year"`), `safesearch` (0/1/2), `pageno`.
- **When to use**: Current events, API documentation, error messages, library versions, anything not in the codebase or wikis.

### Web URL Read (`mcp__internet-search__Internet-Search-web_url_read`)
Fetch and read a specific web page's content.
- Pass the URL to read. Use after `searxng_web_search` to read a specific result, or when you have a known URL.
- **When to use**: Reading specific documentation pages, blog posts, release notes, or any URL the user provides or search returns.

### Context7 Docs (via MCP utils server)
Official library/framework documentation and code examples.
- `resolve_library_id`: Find the Context7 library ID for a package (e.g., "langchain", "pydantic").
- `query_docs`: Query documentation for a resolved library. Pass `library_id` and `query`.
- **When to use**: Looking up API signatures, configuration options, or usage examples for dependencies like LangChain, Pydantic, LiteLLM, Textual, etc.

### General MCP investigation tips
- **DeepWiki before local reads**: When starting any task, use DeepWiki `ask_question` to understand the relevant subsystem BEFORE reading local files. This gives you architectural context that makes file reads far more productive.
- **Parallel queries**: When investigating, fire multiple MCP calls in parallel (e.g., DeepWiki for architecture + Langfuse for traces + SearXNG for docs).
- **Cross-reference**: Use DeepWiki/Devin wiki for "how should it work" and Langfuse for "how did it actually work" during debugging.
- **Session IDs bridge Meeseeks and Langfuse**: The `session_id` from `SessionStore` is the same ID used in Langfuse traces. Use it to jump between local transcript analysis and Langfuse observability.
- **Trace names in Meeseeks**: Tool-use loop traces use `user_id="meeseeks-tool-use"`, planning uses `user_id="meeseeks-task-master"`, context selection uses `user_id="meeseeks-context"`. Sub-agent traces share the same session_id but have distinct agent_id tags in event payloads.
- **Age parameter**: Langfuse tools use `age` in minutes (not timestamps). Common values: 60 (1h), 1440 (24h), 10080 (7 days max).

## Engineering principles (project-specific)

### KISS & DRY — keep the codebase lean
This is the core philosophy. Every decision — from picking a dependency to writing a single function — should bias toward less code, not more. KISS means writing code that does real work at the point of definition (validates itself, constrains its inputs, encodes the logic once) so callers stay simple. DRY means that logic lives in exactly one place and everything else just calls it. These aren't just infrastructure concerns — they apply equally when writing everyday application code.

**What this looks like in practice:**

- **Research before building**: Before writing a custom solution, search for well-reputed existing libraries or tools that solve the problem. Use DeepWiki (`ask_question`) to check how similar projects handle it, SearXNG to find established packages, and Context7 to check library APIs. A well-maintained dependency with a clear API beats a hand-rolled implementation every time.
- **Write code that carries its own weight**: Every function, model, or class should validate, constrain, and make sense at the point of definition — not push that burden to callers. Example: `AppConfig` uses Pydantic not just to define the config shape but to validate values at load time (`field_validator`, `ConfigDict(extra=”forbid”)`) so invalid config fails immediately instead of causing mysterious runtime errors downstream. That's KISS — the config is simple to *use* because it's smart where it's *defined*.
- **Define logic once, call it everywhere**: When a piece of logic applies in multiple contexts, encode it in one place. Example: `filter_specs()` encodes allowlist/denylist tool scoping once and is called by spawn_agent, skills, and the API — not reimplemented at each call site. That's DRY.
- **Prefer small, obvious changes**: The best diff is the smallest one that solves the problem. Remove redundancy instead of adding layers.
- **Do not over-engineer**: No speculative abstractions, no premature generalization, no “just in case” flexibility. Build what the task requires — nothing more.
- **Reuse before creating**: Check what already exists in the codebase (grep first) and in the ecosystem (search first). Only create new utilities, helpers, or abstractions when there is genuinely nothing suitable.
- **Lean dependencies**: When adding a dependency, prefer well-reputed, actively maintained packages with minimal transitive dependencies. Check download counts, maintenance status, and whether the project already uses something similar. Don't add a library for something the stdlib or an existing dependency already handles.

**Precedents — decisions already made in this codebase that embody this philosophy:**

| What we needed | What we use | What we did NOT build |
|---|---|---|
| Multi-provider LLM calls | **LiteLLM via LangChain** (`ChatLiteLLM`) — one adapter for OpenAI, Claude, Gemini, etc. | Custom provider adapters, API client wrappers, or model routing logic |
| Terminal UI (panels, spinners, layout) | **Rich** (`Console`, `Panel`, `Live`, `Syntax`) | Custom ANSI escape sequences, manual box-drawing, terminal width math |
| Full-screen CLI dialogs & REPL history | **Textual** + **Prompt-toolkit** (`PromptSession`, `FileHistory`) | Custom TTY handling, modal rendering, history file management |
| Data validation & serialization | **Pydantic** (`BaseModel`, `field_validator`, `ConfigDict`) | Hand-written validators, manual JSON parsing, custom schema generation |
| REST API | **Flask + Flask-RESTX** | Custom HTTP server, manual route dispatch, hand-written API docs |
| LLM observability & tracing | **Langfuse** (`CallbackHandler`) — plugs into LangChain callbacks | Custom telemetry pipeline, manual trace correlation |
| Prompt templating | **Jinja2** (`Environment`, `PackageLoader`) | Custom string interpolation or fragile f-string assembly |
| Token counting | **Tiktoken** — OpenAI's tokenizer | Heuristic character-ratio guessing |
| Structured logging | **Loguru** — one-liner config with color, context, formatting | Custom log handlers, formatters, rotation logic |
| MCP protocol integration | **langchain-mcp-adapters** (`MultiServerMCPClient`) | Custom MCP protocol client from scratch |
| MongoDB access | **PyMongo** — connection pooling, indexing, CRUD | Custom database driver or raw socket queries |

The pattern: **proven library for infrastructure, custom code only for business logic** (orchestration, agent state, tool coordination). When in doubt, check if a library already does it.

### Other principles
- **Context before code**: Use DeepWiki (`ask_question` on `bearlike/Assistant`) to understand the subsystem before modifying it. Uninformed changes waste everyone's time.
- KRY: keep requirements and acceptance criteria in view; do not drift.
- Keep tool contracts stable (`AbstractTool`, `ActionStep`, `TaskQueue`) and the tool field names (`tool_id`, `operation`, `tool_input`).
- Favor composition and reuse across interfaces; avoid duplicating core logic.
- Add or improve tests for non-trivial behavior; expand coverage when touching core logic or tools.
- Use Gitmoji + Conventional Commit format (e.g., `✨ feat: add session summary pass-through`).
- Do not push unless explicitly requested.
- Use `.github/git-commit-instructions.md` for commit + PR titles and bodies.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language and describe changes objectively (e.g., “updated prompts/instructions”).
- Keep type hints precise; avoid loosening to `Any` unless no accurate alternative exists.

## Project instructions loading
- `discover_all_instructions()` in `common.py` discovers instruction files from four priority levels:
  1. **User**: `~/.claude/CLAUDE.md` (lowest priority)
  2. **Project**: `CLAUDE.md` and `.claude/CLAUDE.md` walking from CWD up to the git root
  3. **Rules**: `.claude/rules/*.md` files in CWD
  4. **Local**: `CLAUDE.local.md` in CWD (highest priority)
- **Subtree discovery**: `discover_subtree_instructions()` walks DOWN from CWD into subdirectories (max depth 5) to find nested `CLAUDE.md`, `AGENTS.md`, and `.claude/CLAUDE.md` files. These are **indexed, not injected** — the model sees a list of paths and can read them on demand. Prunes hidden dirs, `node_modules`, `__pycache__`, `.venv`.
- **Recursive skill discovery**: `discover_skills()` also walks the subtree (max depth 5) to find `.claude/skills/*/SKILL.md` in subdirectories. Subtree skills don't override project-root or personal skills.
- The legacy `discover_project_instructions()` function uses `discover_all_instructions()` as its backend, appends the subtree index, and falls back to `AGENTS.md` if no sources are found.
- Place `<!-- meeseeks:noload -->` on the **first line** of a file to skip it. Used on shim `AGENTS.md` files that only redirect to `CLAUDE.md` to avoid duplicate context loading.
- The marker is defined as `_NOLOAD_MARKER` in `packages/meeseeks_core/src/meeseeks_core/common.py`.
- Git context (branch, status, recent commits) is injected into the system prompt via `get_git_context()` in `common.py`.

## Orchestration architecture
- **Single async loop**: `ToolUseLoop.run()` is the only execution engine. The LLM decides which tools to call via native `bind_tools`. No separate planner→executor→synthesizer pipeline.
- **Tool scoping**: `filter_specs()` in `tool_registry.py` applies allowlist/denylist filtering. The API passes `context.mcp_tools` as `allowed_tools` through `SessionRuntime` → `Orchestrator` → `ToolUseLoop` to scope tool binding per query.
- **Sub-agent spawning**: The LLM can call `spawn_agent(task, model, allowed_tools, denied_tools, max_steps, acceptance_criteria)` to create child `ToolUseLoop` instances. Tool scoping uses the same `filter_specs()` function. Sub-agents inherit the parent's `approval_callback` so they can execute write/edit/shell tools in API/headless contexts.
- **Agent hypervisor (active governor)**: `AgentHypervisor` is a dual-layer autonomous system:
  - *Reflexes* (code): `update_step()` fires on every tool call across the entire tree, tracks budget, detects stalls, and can inject NL interventions via message queues — even while the root agent is blocked.
  - *Brain* (root LLM): sees the global agent tree via `render_agent_tree()` at each LLM turn, making strategic delegation and verification decisions.
  - **6-state lifecycle** (Ref: [A2A v1.0]): `submitted → running → {completed, failed, cancelled, rejected}`. Agents start as `submitted`, transition to `running` when the loop begins, then to a terminal state.
  - **AgentResult (Communication Units)** (Ref: [CoA §3.1]): Structured results with `content`, `status`, `steps_used`, `summary` (compressed CU), `warnings`, `artifacts`. Returned as JSON from sub-agents instead of raw text.
  - **Budget tracking** (Ref: [AgentCgroup §4.2]): Session-wide `session_step_budget` with graduated enforcement — NL warning injection via `SystemMessage`, never force-kill.
  - **Stall detection**: `last_step_at` per agent enables identifying unresponsive delegatees.
  - **Bidirectional messaging**: `send_message(agent_id, text)` injects steering into any running agent's queue.
  - **Global eye**: `render_agent_tree()` renders the full hierarchy into the root agent's system prompt.
  - Admission control (max_concurrent via Semaphore), 3-phase graceful cleanup (cancel → wait → force-mark).
- **Lifecycle-aware depth guidance**: `_build_depth_guidance()` injects role-specific prompting:
  - Root: "Orchestrator — verify results, synthesize, delegate bounded tasks with acceptance criteria"
  - Sub-orchestrator: "Delegated agent — bounded scope, may further delegate"
  - Leaf: "Executor — complete task directly, self-terminate when done, admit failure explicitly"
- **Depth control**: Max depth 5 (configurable). At max depth, `spawn_agent` is removed from the tool schema entirely.
- **User steering**: Root agent has a `message_queue` (`queue.Queue`, thread-safe) drained between steps as HumanMessage, and an `interrupt_step` (`threading.Event`). Both are created in `RunRegistry.start()` and shared with the `AgentContext` via the orchestration chain. Child agents also get their own `message_queue` for parent→child steering. The API exposes `/message` and `/interrupt` endpoints.
- **Attachment handling**: `ContextBuilder` reads uploaded text files from disk (via context events with attachment metadata) and injects their content into `ContextSnapshot.attachment_texts`, which is included in the system prompt.
- **Planning is root-only**: Sub-agents always execute (act mode). They bypass `Orchestrator` and its plan/mode logic entirely.
- **Skills**: `SkillRegistry` discovers `SKILL.md` files from `~/.claude/skills/` and `.claude/skills/` following the [Agent Skills](https://agentskills.io) open standard. The skill catalog is injected into the system prompt for LLM auto-invocation via `activate_skill`. User `/skill-name` invocations are detected in the `Orchestrator` and rendered into `skill_instructions` passed to `ToolUseLoop`. Skills can scope tools via `allowed-tools` (reuses `filter_specs()`) and preprocess shell commands via `` !`cmd` `` syntax.
- **Tool concurrency partitioning**: `ToolUseLoop._partition_tool_calls()` groups concurrent-safe tools into parallel batches and isolates exclusive tools (`concurrency_safe=False`) for sequential execution. The partitioner replaces the previous flat `asyncio.gather()` over all tools.
- **Per-tool timeout**: Each tool invocation is wrapped in `asyncio.wait_for()` with a configurable `spec.timeout` (default 120s). Timeouts produce error results without cancelling sibling tools.
- **MCP connection pool**: `MCPConnectionPool` in `mcp_pool.py` provides persistent, memoized MCP server connections with automatic reconnection after 3 consecutive errors, per-request timeouts (60s), and config change detection. `MCPToolRunner` uses the pool as its primary path with legacy one-shot client as fallback.
- **Structured compaction**: `compact_conversation()` in `compact.py` supports two modes — `FULL` (summarize everything) and `PARTIAL` (summarize old events, keep recent verbatim). Uses a structured summary prompt with `<analysis>` scratchpad and `<summary>` sections. Post-compact file restoration re-injects recently referenced files within token budgets. Auto-compact defaults to `PARTIAL` mode.
- **Hook error isolation**: All hook invocations are wrapped in try/except — a failing hook logs a warning but does not block tool execution or session lifecycle. New lifecycle hooks: `on_session_start`, `on_session_end`, `on_compact`. External hooks configurable via `HooksConfig` with `fnmatch`-based tool matcher filtering.
- **Structured agent errors**: `AgentError` in `spawn_agent.py` captures agent_id, depth, task, error message, last tool, and steps completed. Replaces bare error strings for better diagnostics. Sub-agent cleanup cascades to children before unregistering.
- **Graceful agent cleanup**: `AgentHypervisor.cleanup()` uses 3-phase escalation: request cancellation → wait with timeout → force-mark as cancelled.
- Make tool inputs schema-aware; prefer structured `tool_input` for MCP tools.
- Surface tool activity clearly (permissions, tool IDs, arguments) to reduce user confusion.

## Testing patterns (what worked)
- Mock as little as possible; prefer real code paths with stubbed I/O boundaries.
- Cover the full orchestration loop with fake tools and fake LLM outputs.
- Ensure tests fail when tool args are malformed (schema + coercion paths).
- Avoid hidden defaults in tests that mask production behavior.

## Testing & running (common paths)
- Tests live under `tests/` (use `pytest`).
- Local dev uses `uv` with `configs/app.json` (and `configs/mcp.json` when using MCP).
- Core-only install: `uv sync`.
- Full dev install: `uv sync --all-extras --all-groups`.
- Run interfaces from repo root with `uv run meeseeks`, `uv run meeseeks-api`, or `cd apps/meeseeks_console && npm run dev`.
- **Global install**: `uv tool install .` from the repo root installs `meeseeks` system-wide. Config files are discovered via a priority chain: `CWD/configs/` → `$MEESEEKS_HOME/` → `~/.meeseeks/`. Copy `configs/app.json` and `configs/mcp.json` to `~/.meeseeks/` for global use, or run `/init` to scaffold examples. Use `--config /path/to/app.json` to override.
- Dockerfiles live under `docker/` for base, console, and API; Compose is supported when needed.

## Linting & formatting
- Primary linting uses `ruff` (root + subpackages). Auto-fix with `.venv/bin/ruff check --fix .`.
- Type checking uses `mypy`. Run from repo root after installing with `uv`.
- `flake8`, `pylint`, and `autopep8` are still available as dev tools (optional/ad‑hoc use).
- Helper targets: `make lint`, `make lint-fix`, and `make typecheck`.
- Pre-commit hooks are defined in `.pre-commit-config.yaml` (install with `make precommit-install`).

## Expectations for agents
- **DeepWiki first, always**: Before reading files or writing code, use DeepWiki `ask_question` on `bearlike/Assistant` to understand the area you're about to work on. Ask about architecture, data flow, component relationships, and invariants. Then verify details in local code. Skipping this step wastes time and leads to uninformed changes.
- **Hydrate subagents too**: When spawning subagents or parallel agents, include instructions to use DeepWiki for context hydration in their prompts.
- Keep changes minimal, readable, and well‑scoped.
- Document assumptions in PRs/notes when behavior is inferred.
