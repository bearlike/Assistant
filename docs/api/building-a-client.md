# Building a Client

Every Mewbo client drives the same engine. This page walks the session lifecycle over the REST API, from creating a session to following a run and steering it. Build against these endpoints and your client behaves like the console, the CLI, and the mobile app, because they all speak this same surface.

You have two ways to build on Mewbo. Drive it over HTTP with the REST API, which is what this page covers, or embed the core engine in-process as a Python library. The [in-process option](#embedding-the-engine-in-process) is at the end.

The examples assume your API key is exported and your stack serves the default base URL:

```bash
export MEWBO_API_KEY="<your-api-key>"
export MEWBO_API_URL="http://localhost:5125"
```

## The session lifecycle

A session is the core resource. It holds one conversation and its full event history. The lifecycle is: create a session, send it a query, follow the run, then send follow-ups or steer as needed.

### Create a session

[POST /api/sessions](endpoint:POST /api/sessions) creates an empty session and returns its id:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

```json
{ "session_id": "9e2d47c1a0b34f12" }
```

The body is optional. The useful fields:

| Field | Purpose |
|---|---|
| `session_tag` | A stable lookup key. Resolve or reuse a session by tag instead of tracking the id yourself. |
| `project` | Bind the session to a configured project, which sets its working directory, tools, and skills. |
| `context` | Initial context to persist, such as `model`. |
| `fork_from` | Create the session as a branch of an existing session's history. Pair with `fork_at_ts` to branch at a point. |

An explicit `cwd` is also accepted, but it requires the `api.allow_external_cwd` flag. It is meant for external workspace managers that anchor a session in their own worktree.

### Send a query

[POST /api/sessions/{session_id}/query](endpoint:POST /api/sessions/{session_id}/query) starts a run. Only `query` is required:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/query" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize the open pull requests."}'
```

The run is asynchronous. The endpoint returns `202 Accepted` and the work continues server-side:

```json
{ "session_id": "9e2d47c1a0b34f12", "accepted": true }
```

The response codes tell you what happened:

| Status | Meaning |
|---|---|
| `202` | A run started. Follow it via the events or stream endpoints. |
| `200` | An inline slash command (`/status`) was handled without a run. |
| `409` | A run is already active. A session executes one turn at a time. |
| `400` | The `query` field was missing. |

The body carries optional fields alongside `query`. Set `context.model` to pick a model for the turn. Set `context.fallback_models` to a list to escalate down a ladder when the primary model keeps failing. Set `mode` to `plan` or `act`. Inline `@file`, `@dir`, `@diff`, and `@url` references in the query are expanded into bounded context before the run, so you do not need a separate read step for local context.

### Follow the run

You have two ways to follow a run: poll for events, or attach to the live stream.

**Poll** with [GET /api/sessions/{session_id}/events](endpoint:GET /api/sessions/{session_id}/events). Pass `after` with the timestamp of the last event you processed to fetch only newer ones. Pass `truncate=1` to cap large free-text payloads:

```bash
curl "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/events?after=1720000000.0" \
  -H "X-API-KEY: $MEWBO_API_KEY"
```

The response carries the events plus the authoritative run state, so you do not reconstruct status from the timeline:

```json
{
  "session_id": "9e2d47c1a0b34f12",
  "status": "completed",
  "running": false,
  "done_reason": "completed",
  "recoverable": false,
  "events": [ "..." ]
}
```

**Stream** with [GET /api/sessions/{session_id}/stream](endpoint:GET /api/sessions/{session_id}/stream). This is Server-Sent Events, which browser `EventSource` clients cannot send custom headers on, so the streaming routes accept the key as an `api_key` query parameter:

```bash
curl -N "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/stream?api_key=$MEWBO_API_KEY"
```

The stream replays the stored backlog once, then pushes each new event the instant it is appended. It is push-based, not a poll loop. A terminal `stream_end` event marks the run finished. Drain the stream through that event, because the final `completion` event arrives just before it.

### Event kinds

Each event has a `type` and a `payload`. The kinds you handle in a client:

| Event | When it fires |
|---|---|
| `action_plan` | A plan was generated. Steps are `{title, description}`. |
| `tool_result` | A tool ran. Carries `tool_id`, `operation`, `tool_input`, and `result`. |
| `permission` | An approval was requested or a decision was recorded. |
| `sub_agent` | A sub-agent changed state. |
| `step_reflection` | The reflector asked for a revision. |
| `assistant` | Final assistant output for the turn. |
| `completion` | The run reached a terminal state. Carries the final `status`. |

Tool events use `tool_id`, `operation`, and `tool_input`. Those field names are a stable part of the contract.

### Send a follow-up

A session is a continuing conversation. To ask a follow-up, send another query to the same session id. The new turn inherits the session's history and context:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/query" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "Now open a draft PR for the first one."}'
```

Wait for the previous run to reach a terminal state first. A query sent while a run is still active returns `409`. To add input to a run that is still going, steer it instead.

### Steer or interrupt a run

While a run is active, you can push input into it or pause it.

[POST /api/sessions/{session_id}/message](endpoint:POST /api/sessions/{session_id}/message) enqueues a steering message. The `text` field is required:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/message" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Focus on the auth module only."}'
```

The same endpoint does double duty. If the run is active, the text steers it and the call returns `202`. If the session is idle or finished, the text re-engages it: a fresh run starts with the text as its query and the call returns `200` with a new `run_id`. Only a terminated session rejects the message.

[POST /api/sessions/{session_id}/interrupt](endpoint:POST /api/sessions/{session_id}/interrupt) signals the current tool step to pause. Interrupting an idle session is a no-op that returns `200`.

### Terminate a session

Interrupting stops a run; the session lives on and can be steered or resumed afterward. Terminating is different: it ends the session itself, permanently.

[POST /api/sessions/{session_id}/terminate](endpoint:POST /api/sessions/{session_id}/terminate) is irreversible. There is no un-terminate call:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/terminate" \
  -H "X-API-KEY: $MEWBO_API_KEY"
```

```json
{
  "session_id": "9e2d47c1a0b34f12",
  "status": "terminated",
  "terminated_at": "2026-07-13T18:24:10.882001+00:00",
  "cancelled_triggers": 2
}
```

`cancelled_triggers` counts any [triggers](triggers.md) armed on the session, they're cancelled in the same call, so a terminated session can never wake itself back up later. The call is idempotent: terminating an already-terminated session returns the same original values.

After termination, every mutating route on that session, a new query, a steering message, recovery, forking, plan approval, returns `410 Gone`:

```json
{ "error": { "code": "session_terminated", "reason": "Session is permanently terminated", "retryable": false } }
```

Termination is not deletion. The transcript stays fully readable: `GET /api/sessions/{session_id}/events`, the stream, export, and share all keep working exactly as before. Only the ability to make the session do anything else is gone.

## Client capability negotiation

Clients advertise the UI primitives they can render with the `X-Mewbo-Capabilities` request header. The value is a comma-separated list of capability ids, for example `stlite`. The API writes the advertised list onto the session's context event, and the orchestrator reads it to filter which agent types, skills, and session tools the model can see.

A client that omits the header will not see capability-gated surfaces. The bundled widget builder is the reference case: without `X-Mewbo-Capabilities: stlite`, a session does not expose the widget-building agent type, skill, or tool.

Send the header on every request that creates or drives a session, not just on create. The orchestrator reads capabilities from the most recent context event, so re-sending the header on each drive keeps the grant current. On the server side, session recovery re-injects the capability context automatically, so a recovered session keeps its grant without any special handling from the client.

## Session utilities

A few more endpoints round out a client. Fork a session from any point to branch a conversation. Share a session read-only, or export its full payload:

```
POST /api/sessions/{session_id}/share     returns { token }
GET  /api/share/{token}                    fetch shared session data (no auth)
GET  /api/sessions/{session_id}/export     download the full payload
POST /api/sessions/{session_id}/archive    archive; DELETE unarchives
```

List and inspect what a project exposes with [GET /api/projects](endpoint:GET /api/projects), [GET /api/tools](endpoint:GET /api/tools), and [GET /api/skills](endpoint:GET /api/skills). Upload files to inject into a session's context with [POST /api/sessions/{session_id}/attachments](endpoint:POST /api/sessions/{session_id}/attachments).

## Embedding the engine in-process

If you would rather run the engine inside your own Python process instead of over HTTP, you can. The core is a library. This path suits an in-process integration where you do not want a network hop.

Initialize the core services, resolve a session, and run:

```python
from mewbo_core.common import get_logger
from mewbo_core.permissions import approval_callback_from_config, load_permission_policy
from mewbo_core.session_runtime import SessionRuntime, parse_core_command
from mewbo_core.session_store import SessionStore
from mewbo_core.tool_registry import load_registry

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

The building blocks:

- `SessionStore` and `SessionRuntime` ([`session_store.py`](repo:packages/mewbo_core/src/mewbo_core/session_store.py), [`session_runtime.py`](repo:packages/mewbo_core/src/mewbo_core/session_runtime.py)) hold transcripts and run the shared runtime.
- `load_registry()` ([`tool_registry.py`](repo:packages/mewbo_core/src/mewbo_core/tool_registry.py)) registers the built-in tools.
- `load_permission_policy()` and `approval_callback_from_config()` ([`permissions.py`](repo:packages/mewbo_core/src/mewbo_core/permissions.py)) wire up approvals.
- `parse_core_command()` handles the core slash commands `/compact`, `/status`, and `/terminate`.
- `run_sync()` runs a turn synchronously. `start_async()` plus `load_events(after=...)` gives you the polling flow instead.

For the full monorepo layout, the core abstractions, and how to add a local tool or a chat-platform channel adapter, see the [Architecture Overview](../core-orchestration.md) and [Session Runtime](../session-runtime.md).

## The full reference

This page is the guided lifecycle. Every route, with its full parameter list, response shapes, and a ready-to-run request sample, lives in the generated [REST API Reference](../rest-api.md).

## Next steps

- [Structured Outputs](structured-outputs.md): get schema-validated objects instead of prose.
- [Device Tool Bridge](device-tools.md): let your client expose tools the agent can call on the device.
- [Automation](automation.md): drive issue pickup and PR workflows through the API.
