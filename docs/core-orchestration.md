# Core Orchestration and Features

This page summarizes the orchestration loop, core components, and operational features for bearlike/Assistant.

## Execution flow
- Input arrives from a client (CLI, API, chat, or Home Assistant integration).
- The orchestrator builds a context snapshot (summary, recent events, and selected history).
- In act mode, a single async `ToolUseLoop` executes — the LLM decides which tools to call via native `bind_tools`.
- In plan mode, the `Planner` generates a plan without execution.
- The LLM can spawn sub-agents via `spawn_agent` for parallel subtasks, managed by the `AgentHypervisor`.
- Results are written to the session transcript.

## Core components
- Orchestrator (`meeseeks_core.orchestrator.Orchestrator`): session lifecycle, context building, and mode resolution.
- ToolUseLoop (`meeseeks_core.tool_use_loop.ToolUseLoop`): async tool-use conversation loop — the single execution engine for all agents.
- AgentContext (`meeseeks_core.agent_context.AgentContext`): immutable per-agent state propagated through the hierarchy.
- AgentHypervisor (`meeseeks_core.hypervisor.AgentHypervisor`): hypervisor control plane — admission control, lifecycle tracking, cancellation, cleanup.
- SpawnAgentTool (`meeseeks_core.spawn_agent.SpawnAgentTool`): sub-agent creation with tool scoping (allowlist/denylist filtered before binding).
- Planner (`meeseeks_core.planning.Planner`): plan generation via LLM.
- SessionRuntime (`meeseeks_core.session_runtime.SessionRuntime`): shared facade for CLI and API.
- SessionStore (`meeseeks_core.session_store.SessionStore`): transcript + summary storage.
- ToolRegistry (`meeseeks_core.tool_registry.ToolRegistry`): local tools and external MCP tools.

## Feature highlights
- Auto-compact runs when token budget or event thresholds are reached; `/compact` forces a summary pass. Token thresholds are configured with `token_budget.auto_compact_threshold`.
- Langfuse tracing is session-scoped when enabled, keeping multi-turn work in one trace context.
- External MCP servers are supported via `configs/mcp.json` and auto-discovered at startup.
- LiteLLM-backed chat models support multiple providers and model aliases; different models can be used for planning and tool execution.
- Permission policies gate tool execution; approvals can be automatic, denied, or prompted.

## Extensibility points
- Add tools by implementing `AbstractTool` or by registering MCP servers with schemas.
- **Configurable file edit tool:** Set `agent.edit_tool` in config to `"search_replace_block"` (Aider-style) or `"structured_patch"` (per-file exact match). The tool schema, LLM prompt instructions, and backend implementation all switch together — they're bundled in the same `ToolSpec` registration.
- Add hooks through `HookManager` for pre/post events and compaction transforms.
- Add new interfaces by reusing `SessionRuntime` and the event transcript model.
