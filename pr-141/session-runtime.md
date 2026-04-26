# Session Runtime

The session runtime is a small shared facade that powers both the CLI and the REST API. It centralizes session lifecycle, async run tracking, and cancellation. Event polling is exposed via the API; the CLI reads events directly in-process when needed.

## What it does
- Resolves sessions by id, tag, or fork.
- Supports fork-from-message via `fork_at_ts`. It creates a new session with only events up to the given timestamp, enabling edit-and-regenerate workflows.
- Runs orchestration synchronously or in a background thread.
- Tracks active runs per session and supports cancellation.
- Filters session events for polling (`after` timestamp).
- Summarizes a session (title, status, done reason, context, archived flag).
- Filters empty sessions from listings; archived sessions are hidden unless requested.

## Core commands
These commands are supported across interfaces:
- `/compact`: compact the session transcript and write a summary.
- `/terminate`: request cancellation for the active run.
- `/status`: return the current session summary.

The runtime only recognizes these core commands. Interface-specific commands remain in each UI layer.

## Event polling model (API)
Events are stored as JSONL records by `SessionStore`. The runtime exposes `load_events(session_id, after)` which filters by the ISO-8601 timestamp (`ts`) on each event.

Typical polling flow:
1. Create a session.
2. Start an async run.
3. Poll `/events` with `after` to receive only new records.

Event payload notes:
- `action_plan` payloads include `steps: [{title, description}]`.
- Tool activity uses `tool_id`, `operation`, and `tool_input` in `tool_result` and `permission` events.

## Minimal usage (Python)
```python
from truss_core.session_runtime import SessionRuntime
from truss_core.session_store import SessionStore

runtime = SessionRuntime(session_store=SessionStore())
session_id = runtime.resolve_session(session_tag="primary")

# synchronous run
result = runtime.run_sync(user_query="Hello", session_id=session_id)

# async run + polling
runtime.start_async(session_id=session_id, user_query="Do the task")
events = runtime.load_events(session_id, after=None)

# fork from a specific message timestamp (edit & regenerate)
forked_id = runtime.resolve_session(
    fork_from=session_id,
    fork_at_ts="2026-04-15T10:30:00+00:00",
)
```

## Archiving behavior
- `SessionStore.archive_session(session_id)` marks a session archived.
- `SessionStore.unarchive_session(session_id)` removes the archive flag.
- `SessionRuntime.list_sessions()` hides archived sessions by default. Use
  `list_sessions(include_archived=True)` to include them.

## Channel adapter sessions

Chat platform adapters (Nextcloud Talk, etc.) create standard sessions via the runtime. Thread-to-session mapping uses existing session tags:

- **Tag format**: `"<platform>:<thread_id>"` (e.g. `"nextcloud-talk:100"`)
- **Lookup**: `session_store.resolve_tag(tag)` returns the session ID or `None`
- **Create**: `create_session()` + `tag_session(session_id, tag)`
- Tags persist in MongoDB/JSON and survive API restarts

Channel sessions are indistinguishable from console/CLI sessions in listings, event streams, and Langfuse traces. A `context` event with `source_platform` metadata is injected at creation so the LLM and completion callback know the session origin.

## Design goals
- Keep the core orchestration engine centralized.
- Make interface layers thin and easy to extend.
- Avoid duplicate session lifecycle logic.
