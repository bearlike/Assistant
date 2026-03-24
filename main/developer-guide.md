# Developer Guide

This page summarizes the code layout, core interfaces, and the minimal steps needed to build a new client.

## Monorepo layout
- `packages/meeseeks_core/`: orchestration loop, session runtime, schemas, session storage, compaction, tool registry.
- `packages/meeseeks_tools/`: tool implementations and integration glue.
- `packages/meeseeks_tools/src/meeseeks_tools/vendor/aider`: vendored Aider utilities used by local file + shell tools.
- `apps/meeseeks_api/`: Flask API that exposes the assistant over HTTP.
- `apps/meeseeks_console/`: Web console for task orchestration (React + Vite).
- `apps/meeseeks_cli/`: terminal CLI for interactive sessions.
- `meeseeks_ha_conversation/`: Home Assistant integration that routes voice requests to the API.

## Project instructions (`CLAUDE.md` / `AGENTS.md`)

The orchestrator loads project instructions from the working directory and injects them into the system prompt. `discover_project_instructions()` in `meeseeks_core.common` checks for `CLAUDE.md` first, then falls back to `AGENTS.md`.

- Place a `CLAUDE.md` at the repo root or in any sub-package to provide context-specific guidance to the orchestration loop.
- `AGENTS.md` is a fallback for tools that look for that filename. In this repo the `AGENTS.md` files are shims that redirect to `CLAUDE.md`.
- To **skip** a file from being loaded (e.g., a shim that would duplicate content), add `<!-- meeseeks:noload -->` as the very first line. The loader checks for this marker and skips the file.

## Model and provider support
- **Model gateway:** Uses LiteLLM for OpenAI-compatible access across multiple providers.
- **Reasoning compatibility:** Applies reasoning-effort controls where supported by the model.
- **Model routing:** Supports provider-qualified model names and a configurable API base URL. Per-role model selection (plan, tool, default) is configured in `configs/app.json`.

## Core abstractions and interfaces
- `AbstractTool` (`meeseeks_core.classes`): base class for local tools; implement `get_state` and `set_state` and return a `MockSpeaker`.
- `ToolRunner` protocol (`meeseeks_core.tool_registry`): interface for tool runners with `run(ActionStep)`.
- `ToolSpec` / `ToolRegistry` (`meeseeks_core.tool_registry`): register tools with `tool_id`, metadata, and a factory.
- `ActionStep`, `Plan`, `TaskQueue` (`meeseeks_core.classes`): planning and tool-execution payloads.
- `PermissionPolicy` (`meeseeks_core.permissions`): allow/deny/ask rules for tool execution.
- `HookManager` (`meeseeks_core.hooks`): pre/post hooks and compaction transforms.
- `SessionStore` / `SessionRuntime` (`meeseeks_core.session_store`, `meeseeks_core.session_runtime`): transcripts and the shared runtime facade.
- `ChatModel` protocol (`meeseeks_core.llm`): interface for LLM backends via `build_chat_model`.

## New client walkthrough (concrete steps)
1. Load config and initialize core services:
   - `load_registry()` for tool registration.
   - `load_permission_policy()` and `approval_callback_from_config()` for approvals.
   - `SessionStore()` and `SessionRuntime()` for transcripts and runs.
2. Resolve or create a session id using `SessionRuntime.resolve_session()`.
3. Handle core slash commands (`/compact`, `/status`, `/terminate`) with `parse_core_command()`.
4. Execute the request:
   - `run_sync()` for synchronous use cases.
   - `start_async()` + `load_events(after=...)` for polling flows.
5. Emit and consume session events:
   - `action_plan` when a plan is generated.
   - `permission` decisions when approvals are requested or denied.
   - `tool_result` for each tool execution (includes `tool_id`, `operation`, `tool_input`, and `result`).
   - `step_reflection` when the reflector requests a revision.
   - `assistant` and `completion` for final output and status.
6. Logging:
   - Use `get_logger()` for module logging.
   - Use `session_log_context(session_id)` to capture per-session logs.

### Minimal sync example
```python
from meeseeks_core.common import get_logger
from meeseeks_core.permissions import approval_callback_from_config, load_permission_policy
from meeseeks_core.session_runtime import SessionRuntime, parse_core_command
from meeseeks_core.session_store import SessionStore
from meeseeks_core.tool_registry import load_registry

logger = get_logger("client")

session_store = SessionStore()
tool_registry = load_registry()
runtime = SessionRuntime(session_store=session_store)

session_id = runtime.resolve_session(session_tag="client")
user_text = "Hello from the client"
command = parse_core_command(user_text)
if command:
    logger.info("Handled command: {}", command)
else:
    result = runtime.run_sync(
        session_id=session_id,
        user_query=user_text,
        tool_registry=tool_registry,
        permission_policy=load_permission_policy(),
        approval_callback=approval_callback_from_config(),
    )
    logger.info("Task result: {}", result.task_result)
```

### Implementing a local tool
1. Subclass `AbstractTool` and implement `get_state` / `set_state`.
2. Register the tool with a `ToolSpec` factory in the registry.

```python
from meeseeks_core.classes import AbstractTool, ActionStep
from meeseeks_core.common import get_mock_speaker
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec

class ExampleTool(AbstractTool):
    def __init__(self) -> None:
        super().__init__(name="Example", description="Example tool")

    def get_state(self, action_step: ActionStep | None = None):
        return get_mock_speaker()(content="Example read")

    def set_state(self, action_step: ActionStep | None = None):
        return get_mock_speaker()(content="Example write")

registry = ToolRegistry()
registry.register(
    ToolSpec(
        tool_id="example_tool",
        name="Example",
        description="Example local tool",
        factory=ExampleTool,
    )
)
```
