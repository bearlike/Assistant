# Core Orchestration and Features

This page summarizes the orchestration loop, core components, and operational features for bearlike/Assistant.

## Execution flow
- Input arrives from a client (CLI, API, chat, or Home Assistant integration).
- The orchestrator builds a context snapshot (summary, recent events, and selected history).
- A planner produces a plan; tool selection narrows allowed tools when needed.
- Steps run through the action runner with permission checks and tool schemas applied.
- Results are synthesized into a response and written to the session transcript.

## Core components
- Orchestrator (`meeseeks_core.orchestrator.Orchestrator`): plan/act loop and session-scoped execution.
- Planner / ToolSelector / StepExecutor / PlanUpdater / ResponseSynthesizer (`meeseeks_core.planning`): plan generation, tool choice, step decisions, plan updates, and response output.
- ActionPlanRunner (`meeseeks_core.action_runner.ActionPlanRunner`): executes action steps and applies permission policies.
- SessionRuntime (`meeseeks_core.session_runtime.SessionRuntime`): shared facade for CLI and API.
- SessionStore (`meeseeks_core.session_store.SessionStore`): transcript + summary storage.
- ToolRegistry (`meeseeks_core.tool_registry.ToolRegistry`): local tools and external MCP tools.

## Feature highlights
- Auto-compact runs when token budget or event thresholds are reached; `/compact` forces a summary pass. Token thresholds are configured with `token_budget.auto_compact_threshold`.
- Step reflection validates tool outcomes before results are finalized.
- Langfuse tracing is session-scoped when enabled, keeping multi-turn work in one trace context.
- External MCP servers are supported via `configs/mcp.json` and auto-discovered at startup.
- LiteLLM-backed chat models support multiple providers and model aliases; different models can be used for planning and tool execution.
- Permission policies gate tool execution; approvals can be automatic, denied, or prompted.

## Extensibility points
- Add tools by implementing `AbstractTool` or by registering MCP servers with schemas.
- Add hooks through `HookManager` for pre/post events and compaction transforms.
- Add new interfaces by reusing `SessionRuntime` and the event transcript model.
