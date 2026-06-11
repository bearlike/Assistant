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
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore

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

## Session Provenance

Every session has an **origin**: the surface or subsystem that created it. Origin is classified automatically from session tags and context at creation time; it is never set manually. The console uses the origin to display a badge on each session card and to power the origin filter on the session list.

| Origin | Classified when |
|--------|-----------------|
| `wiki` | Session is tagged `wiki:job` (indexing run) or `wiki:qa` (Q&A query) |
| `search` | Session is tagged `agentic_search` |
| `structured` | Session is tagged `structured:run` (`POST /v1/structured`, including MCP `structured_query`) or `structured:fast` (`POST /v1/structured/fast`) |
| `draft` | Session is tagged `draft:stream` (`POST /v1/draft/stream`) |
| `channel` | Session carries a channel tag with a `:room:` or `:thread:` segment, such as `nextcloud-talk:room:<token>` |
| `user` | Everything else: direct console, CLI, or API sessions |

The `structured` and `draft` origins come from the realtime endpoints. Those endpoints used to be sessionless. They now mint real sessions, so every structured query and draft stream is browsable in the session list and carries a full transcript.

The **origin filter** on the session list lets you hide background sessions and show only the surfaces you care about. By default the console shows `user` and `channel` sessions and hides `wiki`, `search`, `structured`, and `draft` work. You can toggle any origin in or out independently.

**Badge display.** Each session card shows a small origin badge (`user`, `wiki`, `search`, `channel`, `structured`, or `draft`) so you can tell at a glance which surface created the session. Channel sessions show their platform name (for example Nextcloud or Email) instead of the generic label.

**Capability and workspace chips.** Session cards also show what a session was scoped to. Each capability the session advertised at creation (for example `scg` or `wiki`) renders as a small chip beside the project and branch, and a structured workspace id renders the same way. The chips reflect advertised capabilities only. A capability granted at runtime shows up in the session's Langfuse trace, not on the card.

### Trace provenance in Langfuse

The same provenance reaches observability. At run start, each session's tags, context, and client surface are folded into filter tags on its Langfuse trace. You can filter traces by origin, product, session type, client surface (`cli`, `console`, `api`, `mcp`, and so on), project, repo, branch, workspace, and model. Higher-cardinality facets, such as worktree ids, capabilities, and wiki or search run ids, land in trace metadata. CI agent pickup sessions surface as the `vcs` product. Operator setup for Langfuse is covered in [Production deployment](deployment-production.md#observability-with-langfuse).

## Design goals
- Keep the core orchestration engine centralized.
- Make interface layers thin and easy to extend.
- Avoid duplicate session lifecycle logic.
