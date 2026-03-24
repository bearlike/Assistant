# Agents Guide - Personal Assistant (Meeseeks)

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
1. Use the DeepWiki MCP tool on `bearlike/Assistant` for a fast architecture map.
2. Read `README.md` and component READMEs for configuration/runtime details.
3. Use `rg` to locate specific behavior and follow the exact file path.
4. For CI issues, use GitHub Actions logs (GH CLI or MCP GitHub tools).

## MCP tools (use first for external research)
When you need external context (other repos, CI failures, specs, APIs), prefer MCP tools instead of guessing.

### DeepWiki (`mcp__deepwiki__Deepwiki-OSS-*`)
Fast AI-powered Q&A about any public GitHub repository without cloning or loading large files.
- **`read_wiki_structure`**: Get the table of contents for a repo wiki. Use this first to discover what sections exist. Pass `repoName` in `owner/repo` format (e.g., `bearlike/Personal-Assistant`, `anthropics/claude-code`).
- **`read_wiki_contents`**: Get the full wiki page content for a repo. Use after `read_wiki_structure` to read specific sections.
- **`ask_question`**: Ask any question about a repo and get a grounded, cited answer. Supports passing a single repo or a list of up to 10 repos for cross-repo questions.
- **When to use**: Architecture overviews, understanding how another project works, comparing implementations, finding specific patterns in large repos you haven't cloned.
- **Tip**: Start with `read_wiki_structure` to see available topics, then use `ask_question` with targeted questions. For this project, use `bearlike/Personal-Assistant`.

### Devin Wiki (`mcp__devin__Devin-Wiki-Personal-*`)
Devin-hosted wiki with the same structure as DeepWiki but from Devin's index. Also provides session management, knowledge notes, and scheduling.
- **`read_wiki_structure`** / **`read_wiki_contents`** / **`ask_question`**: Same as DeepWiki but uses Devin's index. Use `bearlike/Personal-Assistant` for this project.
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
- **Parallel queries**: When investigating, fire multiple MCP calls in parallel (e.g., DeepWiki for architecture + Langfuse for traces + SearXNG for docs).
- **Cross-reference**: Use DeepWiki/Devin wiki for "how should it work" and Langfuse for "how did it actually work" during debugging.
- **Session IDs bridge Meeseeks and Langfuse**: The `session_id` from `SessionStore` is the same ID used in Langfuse traces. Use it to jump between local transcript analysis and Langfuse observability.
- **Trace names in Meeseeks**: Tool-use loop traces use `user_id="meeseeks-tool-use"`, planning uses `user_id="meeseeks-task-master"`, context selection uses `user_id="meeseeks-context"`. Sub-agent traces share the same session_id but have distinct agent_id tags in event payloads.
- **Age parameter**: Langfuse tools use `age` in minutes (not timestamps). Common values: 60 (1h), 1440 (24h), 10080 (7 days max).

## Engineering principles (project-specific)
- KISS and DRY: prefer small, obvious changes; remove redundancy instead of adding layers.
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
- The legacy `discover_project_instructions()` function uses `discover_all_instructions()` as its backend and falls back to `AGENTS.md` if no sources are found.
- Place `<!-- meeseeks:noload -->` on the **first line** of a file to skip it. Used on shim `AGENTS.md` files that only redirect to `CLAUDE.md` to avoid duplicate context loading.
- The marker is defined as `_NOLOAD_MARKER` in `packages/meeseeks_core/src/meeseeks_core/common.py`.
- Git context (branch, status, recent commits) is injected into the system prompt via `get_git_context()` in `common.py`.

## Orchestration architecture
- **Single async loop**: `ToolUseLoop.run()` is the only execution engine. The LLM decides which tools to call via native `bind_tools`. No separate planner→executor→synthesizer pipeline.
- **Tool scoping**: `filter_specs()` in `tool_registry.py` applies allowlist/denylist filtering. The API passes `context.mcp_tools` as `allowed_tools` through `SessionRuntime` → `Orchestrator` → `ToolUseLoop` to scope tool binding per query.
- **Sub-agent spawning**: The LLM can call `spawn_agent(task, model, allowed_tools, denied_tools)` to create child `ToolUseLoop` instances. Tool scoping uses the same `filter_specs()` function.
- **Agent hypervisor**: `AgentHypervisor` tracks all agents, enforces admission control (max_concurrent via Semaphore), and guarantees cleanup via 3-phase graceful escalation (cancel → wait → force-mark) in `finally` blocks.
- **Depth control**: Max depth 5 (configurable). At max depth, `spawn_agent` is removed from the tool schema entirely. Depth-aware prompts guide spawn behavior.
- **User steering**: Root agent has a `message_queue` (`queue.Queue`, thread-safe) drained between steps as HumanMessage, and an `interrupt_step` (`threading.Event`). Both are created in `RunRegistry.start()` and shared with the `AgentContext` via the orchestration chain. Sub-agents do not receive user messages. The API exposes `/message` and `/interrupt` endpoints for this.
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
- Start with DeepWiki for overview, then verify details in code.
- Keep changes minimal, readable, and well‑scoped.
- Document assumptions in PRs/notes when behavior is inferred.
