# meeseeks-core

Core orchestration engine for Meeseeks. This package owns the unified async tool-use loop, agent hypervisor, session storage, and event model shared by every interface.

## What it provides
- `ToolUseLoop` — single async execution engine driven by native `bind_tools`.
- `AgentHypervisor` — control plane for sub-agent admission, lifecycle, and cleanup.
- `Orchestrator` + `Planner` for session lifecycle and plan generation.
- Permission policies and hooks for tool execution gating.
- `HookManager` with error-isolated execution, session lifecycle hooks (`on_session_start`/`on_session_end`/`on_compact`), and external hook configuration via `HooksConfig`. Supports two hook types: `"command"` (shell subprocess with env vars `MEESEEKS_SESSION_ID`, `MEESEEKS_ERROR`) and `"http"` (fire-and-forget POST to external URLs in daemon threads).
- Session runtime, transcripts (JSONL), summaries, and two-mode compaction (full/partial) with structured summaries and post-compact file restoration.
- `ToolSpec` with typed metadata: `concurrency_safe`, `read_only`, `max_result_chars`, `timeout`, `capabilities` for fine-grained tool execution control. The file edit tool is conditionally registered based on `AgentConfig.edit_tool`.
- `ToolResult` for structured tool execution results.
- Hierarchical instruction discovery (user → project → rules → local) via `discover_all_instructions()`.
- Git context injection (`get_git_context()`) in the system prompt.
- Concurrency-aware tool partitioning: concurrent-safe tools run in parallel, exclusive tools run alone.
- Per-tool execution timeout via `asyncio.wait_for()`.
- `AgentError` for structured sub-agent error propagation.
- Event payloads used by the API and UIs (`action_plan`, `tool_result`, `permission`).

## Key contracts
- `ActionStep` uses `tool_id`, `operation`, `tool_input` (no action_* fields).
- `action_plan` events emit `steps: [{title, description}]`.
- Tool events emit `tool_id`, `operation`, and `tool_input`.

## Use in the monorepo
From the repo root:
```bash
uv sync
```

Then run an interface from `apps/` (CLI, API, chat UI) which imports this core.

## Docs
- Root overview: `README.md`
- Setup: `docs/getting-started.md`
- Runtime: `docs/session-runtime.md`
