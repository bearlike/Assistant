# meeseeks-core

Core orchestration engine for Meeseeks. This package owns the plan → tool selection → step execution loop, session storage, and event model shared by every interface.

## What it provides
- Orchestrator + planning stages (`Planner`, `ToolSelector`, `StepExecutor`, `PlanUpdater`).
- Tool execution runner with permissions and hooks.
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
