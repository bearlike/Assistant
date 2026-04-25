# Tests - Project Guidance

Scope: this file applies to the root `tests/` suite and shared test patterns.

## What we test
- **Async tool-use loop** (`test_tool_use_loop.py`): text response, tool calls, natural completion (no max_steps — loop runs until model returns text), permission denial, plan context, cancellation, lifecycle-aware depth guidance (root/sub/leaf), budget warning injection, no-step-count-in-messages. Uses `asyncio.run()` + `AgentContext.root()` + `AsyncMock` for `model.ainvoke`.
- **Agent hypervisor** (`test_agent_context.py`): AgentContext depth/child/root, AgentHypervisor CRUD, admission control, cancellation, cleanup, budget tracking, stall detection, message passing, global eye rendering, AgentResult serialization, expanded 6-state lifecycle. Note: `send_message`/`cancel_agent`/`send_to_parent` return `str | None` (None = success, str = diagnostic failure reason).
- **Sub-agent spawning** (`test_spawn_agent.py`): SpawnAgentTool execution, tool scoping (allowed/denied), model validation, depth gate, approval_callback inheritance, AgentResult structured JSON return, acceptance_criteria schema, deprecated max_steps schema field. Note: tool filtering now uses `filter_specs()` from `tool_registry` — mock `mewbo_core.tool_registry.get_config_value` (not `spawn_agent.get_config_value`) when testing config-denied tools. Sub-agents now run until natural completion (no max_steps enforcement).
- **CLI agent display** (`test_cli_agent_display.py`): AgentDisplayManager state, hook callbacks, Rich rendering, tree structure, thread safety, submitted/rejected state rendering, error message display.
- **Hierarchical instruction discovery** (`test_project_instructions.py`): four-level hierarchy discovery (user/project/rules/local), priority ordering, git context formatting, noload marker, empty/missing files.
- **Session runtime** (`test_session_runtime.py`): `SessionRuntime` session resolution, fork-from-tag, `fork_at_ts` (fork-from-message), event loading, archiving, and session listing.
- **Orchestration** (`test_task_master.py`): `orchestrate_session` completion, max-iter, error paths.
- **Tool registry** behavior and tool disabling when init fails.
- **MCP discovery**: schema normalization, per-server failures, and CLI visibility even when tools are missing.
- **CLI integration** (`apps/mewbo_cli/tests/test_cli.py`): full CLI flows using lightweight stubs.
- **HTTP hooks** (`test_hooks_http.py`): HTTP hook factories (pre/post tool, session start/end), matcher filtering, mixed command+http hooks, session env var enrichment (`MEWBO_SESSION_ID`, `MEWBO_ERROR`). Mocks `_post_json` to avoid real HTTP.
- **Channel adapters** (`test_channels.py`): `DeduplicationGuard` TTL, `ChannelRegistry` CRUD, `NextcloudTalkAdapter` HMAC verification (valid/invalid/missing headers, backend allowlist with missing header bypass), ActivityStreams payload parsing (Create/Update/Delete, thread_id, Rich Object placeholder stripping, file attachment extraction), `ChannelAdapter` protocol compliance.
- **Email channel** (`test_email_channel.py`): `EmailAdapter.parse_email()` MIME parsing, thread ID extraction (Message-ID/In-Reply-To/References), allowed_senders filtering, multi-party mention gating (`requires_mention()`), `send_response()` SMTP with threading headers and HTML body, markdown→HTML rendering (bold, code blocks, tables, template wrapping), `EmailPoller` lifecycle (start/stop, daemon thread, poll interval floor), protocol compliance.
- **Plugins** (`test_plugins.py`): plugin discovery, manifest parsing, marketplace reading, install/uninstall, path traversal guards, MCP format unwrap, DRY substitution.
- **Plugin hooks** (`test_plugin_hooks.py`): hook format translation from plugin manifests to `HooksConfig`, matcher filtering.
- **Plugin integration** (`test_plugin_integration.py`): end-to-end plugin loading into session init, component wiring (skills, hooks, agent definitions, MCP tools).
- **Agent registry** (`test_agent_registry.py`): agent definition registry, markdown frontmatter parsing, `agent_type` lookup in `spawn_agent`.

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
