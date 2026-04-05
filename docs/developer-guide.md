# Developer Guide

This page summarizes the code layout, core interfaces, and the minimal steps needed to build a new client.

## Monorepo layout
- `packages/meeseeks_core/`: orchestration loop, session runtime, schemas, session storage, compaction, tool registry.
- `packages/meeseeks_tools/`: tool implementations and integration glue.
- `packages/meeseeks_tools/src/meeseeks_tools/vendor/aider`: vendored Aider utilities used by local file and shell tools.
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
- `ToolSpec` / `ToolRegistry` (`meeseeks_core.tool_registry`): register tools with `tool_id`, metadata, and a factory. The file edit tool is conditionally registered based on `agent.edit_tool` config — either `aider_edit_block_tool` or `file_edit_tool`. The `read_file` tool is always registered as a native built-in (see [Built-in `read_file` tool](#built-in-read_file-tool) below).
- `ActionStep`, `Plan`, `TaskQueue` (`meeseeks_core.classes`): planning and tool-execution payloads.
- `PermissionPolicy` (`meeseeks_core.permissions`): allow/deny/ask rules for tool execution.
- `HookManager` (`meeseeks_core.hooks`): pre/post hooks, compaction transforms, and session lifecycle hooks. Supports `"command"` (shell) and `"http"` (fire-and-forget POST) hook types via `HooksConfig`.
- `ChannelAdapter` protocol (`meeseeks_api.channels.base`): abstraction for chat platform integrations (verify_request, parse_inbound, send_response, system_context). Shared `_process_inbound()` pipeline in `routes.py`. Adapters: Nextcloud Talk (webhook-driven) and Email (IMAP poll-driven with markdown→HTML replies).
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

## Built-in `read_file` tool

The `read_file` tool (`tool_id: "read_file"`) is a native built-in for reading local files with line-based windowing and a dedup cache that prevents redundant reads from bloating the LLM's context.

**Implementation:** `packages/meeseeks_tools/src/meeseeks_tools/integration/aider_file_tools.py` (`ReadFileTool` class)
**Registration:** `packages/meeseeks_core/src/meeseeks_core/tool_registry.py`
**Prompt:** `packages/meeseeks_core/src/meeseeks_core/prompts/tools/read-file.txt`

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | yes | — | File path to read (relative to `root`) |
| `root` | string | no | CWD | Project root for path resolution |
| `offset` | integer | no | `0` | 0-based start line |
| `limit` | integer | no | `2000` | Maximum lines to return |

### Output format

Returns a JSON payload with line-numbered content:

```json
{
  "kind": "file",
  "path": "src/main.py",
  "text": "1\timport os\n2\timport sys\n3\t\n4\tdef main():\n5\t    pass",
  "total_lines": 5
}
```

- **Line numbers** are 1-based, tab-separated (matches `cat -n` format).
- **`total_lines`** reflects the full file, not the windowed portion — so the model knows whether it has seen everything.
- When the file exceeds the limit, a truncation hint is appended: `... (truncated — use offset/limit to read more)`.

### Dedup cache

The `ToolUseLoop` maintains a per-run `_file_read_cache` that prevents the same file from being re-read when it hasn't changed.

**How it works:**

1. On the first `read_file` call for a path, the tool executes normally and the cache records: `{path, offset, limit, mtime}`.
2. On a subsequent `read_file` call with the same path, offset, and limit, the cache checks `os.path.getmtime()`. If the mtime matches, the tool returns a stub instead of the full content:

   > *"File unchanged since last read. The content from the earlier Read tool_result in this conversation is still current — refer to that instead of re-reading."*

3. When the `file_edit_tool` edits a file, the cache entry for that path is invalidated — the next read returns fresh content.

**Why this matters:** In observed sessions, GPT 5.4 re-read `backend.py` 8 times across 70 steps, adding 64KB of identical content to the message array. The dedup cache reduces this to one full read + seven 30-token stubs — a ~99% reduction in redundant context.

**Cache location:** `ToolUseLoop._file_read_cache` (`tool_use_loop.py`). The cache is per-run (created with the loop, discarded when the run ends). No cross-run persistence is needed — compaction handles session continuity.

### System prompt guidance

The system prompt (`prompts/system.txt`) includes:

> *Tool outputs from earlier steps persist in this conversation. Reference previous results instead of re-reading files or re-running commands you have already executed.*

This complements the dedup cache. The prompt guides the model to avoid redundant calls; the cache catches them programmatically when the model doesn't follow the guidance.
