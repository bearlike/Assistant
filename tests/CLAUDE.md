# Tests - Project Guidance

Scope: this file applies to the root `tests/` suite and shared test patterns.

## What we test
- **Async tool-use loop** (`test_tool_use_loop.py`): text response, tool calls, max_steps, permission denial, plan context, cancellation. Uses `asyncio.run()` + `AgentContext.root()` + `AsyncMock` for `model.ainvoke`.
- **Agent hypervisor** (`test_agent_context.py`): AgentContext depth/child/root, AgentHypervisor CRUD, admission control, cancellation, cleanup.
- **Sub-agent spawning** (`test_spawn_agent.py`): SpawnAgentTool execution, tool scoping (allowed/denied), model validation, depth gate.
- **CLI agent display** (`test_cli_agent_display.py`): AgentDisplayManager state, hook callbacks, Rich rendering, tree structure, thread safety.
- **Orchestration** (`test_task_master.py`): `orchestrate_session` completion, max-iter, error paths.
- **Tool registry** behavior and tool disabling when init fails.
- **MCP discovery**: schema normalization, per-server failures, and CLI visibility even when tools are missing.
- **CLI integration** (`apps/meeseeks_cli/tests/test_cli.py`): full CLI flows using lightweight stubs.

## Hidden dependencies / assumptions
- Many tests rely on `monkeypatch` for env vars (LLM config, MCP config, log levels).
- LLM calls are mocked at the orchestration boundary (`orchestrate_session`) or via `AsyncMock` on `model.ainvoke`.
- `ToolUseLoop` tests require `AgentContext.root()` with an `AgentHypervisor` — do not use the old `model_name=` kwarg.
- Avoid pulling in real MCP servers or external HTTP.

## Pitfalls / gotchas
- Over-mocking hides real behavior. Mock only the LLM call boundary and tool execution boundary.
- Schema mismatches must be exercised (string tool_input, dict tool_input, invalid schema, required fields).
- Missing-tool tests should assert `last_error` and follow the same path as production.
- Keep tests deterministic: fixed timestamps, fixed session IDs, explicit env.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language.
- `ToolUseLoop.run()` is async — wrap calls with `asyncio.run()` in sync test methods.
- Agent lifecycle hooks (`on_agent_start`/`on_agent_stop`) fire from async context — use thread-safe mocks.

## Preferred patterns
- Use fake tools that implement the same `ToolSpec` interface.
- Use `_make_agent_context()` helper (in `test_tool_use_loop.py`) to create root contexts for tests.
- Use `AsyncMock` for `model.ainvoke` — set `return_value` or `side_effect` for multi-step conversations.
- Use `_text_response()` and `_tool_call_response()` helpers to create mock `AIMessage` objects.
- Favor integration-style tests that cover a full turn: user → tool calls → final text response.
- Parameterize micro-variants instead of duplicating tests (schema coercion, tool discovery).
- Use lightweight stubs for CLI I/O (dummy input/output) to avoid terminal dependencies.

## Cross-project insights (for test design)
- Assert outbound request payloads and event ordering rather than only return values (tool_id/operation/tool_input).
- Build tests around "event streams" (tool call, tool result, response) to catch orchestration regressions.
- Prefer harness-style helpers to simulate model responses without HTTP.
- Exercise error paths with structured exceptions to verify logging and error propagation.
- Track context updates (summary, recent events) via stored session state.
