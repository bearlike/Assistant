# Tests - Project Guidance

Scope: this file applies to the root `tests/` suite and shared test patterns.

## What we test
- Orchestration loop (`meeseeks_core.task_master.orchestrate_session`): completion, replanning, max-iter behavior.
- Tool registry behavior and tool disabling when init fails.
- CLI integration flows using lightweight stubs.
 - MCP discovery: schema normalization, per-server failures, and CLI visibility even when tools are missing.

## Hidden dependencies / assumptions
- Many tests rely on `monkeypatch` for env vars (LLM config, MCP config, log levels).
- LLM calls are mocked at the orchestration boundary (not at HTTP layer).
- Avoid pulling in real MCP servers or external HTTP.

## Pitfalls / gotchas
- Over-mocking hides real behavior. Mock only the LLM call boundary and tool execution boundary.
- Schema mismatches must be exercised (string tool_input, dict tool_input, invalid schema, required fields).
- Replan tests should include failure context (“Last tool failure: …”) in the next prompt.
- When a failure triggers replan, disable response synthesis or stub it to avoid network calls.
- Missing-tool tests should assert `last_error` and follow the same path as production.
- Keep tests deterministic: fixed timestamps, fixed session IDs, explicit env.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language.

## Preferred patterns
- Use fake tools that implement the same `ToolSpec` interface.
- Validate tool-call reflection: tool output must be passed to response synthesis.
- Favor integration-style tests that cover a full turn: user → plan → tool → replan/response.
- Parameterize micro-variants instead of duplicating tests (schema coercion, tool discovery).
- Use a single “scenario” test to cover multiple branches rather than many micro-tests.
- Use lightweight stubs for CLI I/O (dummy input/output) to avoid terminal dependencies.

## Cross-project insights (for test design)
- Reference implementations emphasize integration tests with mocked model/tool servers.
- Assert outbound request payloads and event ordering rather than only return values (tool_id/operation/tool_input).
- Build tests around “event streams” (plan, tool call, tool result, response) to catch orchestration regressions.
- Prefer harness-style helpers to simulate model responses without HTTP.
- Exercise error paths with structured exceptions to verify logging and replan behavior.
- Track context updates (summary, recent events) via stored session state.
