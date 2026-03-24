# meeseeks-core

Core orchestration engine for Meeseeks. This package owns the unified async tool-use loop, agent hypervisor, session storage, and event model shared by every interface.

## What it provides
- `ToolUseLoop` — single async execution engine driven by native `bind_tools`.
- `AgentHypervisor` — control plane for sub-agent admission, lifecycle, and cleanup.
- `Orchestrator` + `Planner` for session lifecycle and plan generation.
- Permission policies and hooks for tool execution gating.
- Session runtime, transcripts (JSONL), summaries, and compaction.
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
