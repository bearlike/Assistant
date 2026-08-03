# Get Started

The REST API is Mewbo's programmable surface: the same engine behind every Mewbo client, exposed over HTTP.

The web console, the terminal CLI, the MCP server, and the mobile app all drive sessions through this API. When you call it directly, you are talking to the same orchestration loop they use. Sessions are the core resource. You create one, send it queries, and read the results back as a stream of events. Everything else in this section builds on that loop.

## Base URL

A self-hosted stack serves the API on port `5125` by default:

```
http://localhost:5125
```

Point the examples on this page at wherever your stack runs. If you start the server locally for development with `uv run mewbo-api`, the built-in Flask dev server listens on port `5124` instead. The container deployment publishes `5125`. See [Docker Compose](../deployment-docker.md) for the deployment details.

## Authentication

Every protected route requires an API key. Send it in the `X-API-KEY` request header:

```
X-API-KEY: <your-api-key>
```

The key is validated against your configured master token (`api.master_token`), or against any revocable key you mint through the key store with [POST /api/keys](endpoint:POST /api/keys). Prefer a minted key over the master token, since revoking it is instant and does not disturb anything else. Server-Sent Events routes are the one exception to the header rule: browser `EventSource` cannot set custom headers, so those routes also accept the key as an `api_key` query parameter.

## Your first request

This walkthrough runs one query end to end. It creates a session, sends a query, and reads the events back. Export your key first so the examples are copy-pasteable:

```bash
export MEWBO_API_KEY="<your-api-key>"
```

### 1. Create a session

A session holds one conversation and its full event history. Create an empty one with [POST /api/sessions](endpoint:POST /api/sessions):

```bash
curl -X POST http://localhost:5125/api/sessions \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The response carries the new session id:

```json
{ "session_id": "9e2d47c1a0b34f12" }
```

The request body is optional. You can bind a project, apply a lookup `session_tag`, or persist initial context such as the model to use. See [Building a Client](building-a-client.md) for the full set of fields.

### 2. Send a query

Start a run with [POST /api/sessions/{session_id}/query](endpoint:POST /api/sessions/{session_id}/query). The body needs a `query` field:

```bash
curl -X POST http://localhost:5125/api/sessions/9e2d47c1a0b34f12/query \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "List the Python files in this project."}'
```

The run is asynchronous. The endpoint returns `202 Accepted` right away and the work continues server-side:

```json
{ "session_id": "9e2d47c1a0b34f12", "accepted": true }
```

A session runs one turn at a time. A second query while one is still active returns `409`. The slash commands `/status` and `/terminate` are handled inline without starting a run.

### 3. Read the results

You have two ways to follow a run. Poll for events, or attach to the live stream.

Poll with [GET /api/sessions/{session_id}/events](endpoint:GET /api/sessions/{session_id}/events). Pass `after` with the timestamp of the last event you saw to fetch only newer ones:

```bash
curl "http://localhost:5125/api/sessions/9e2d47c1a0b34f12/events" \
  -H "X-API-KEY: $MEWBO_API_KEY"
```

The response carries the event list plus the authoritative run state:

```json
{
  "session_id": "9e2d47c1a0b34f12",
  "status": "completed",
  "running": false,
  "done_reason": "completed",
  "events": [
    { "type": "assistant", "payload": { "content": "..." } },
    { "type": "completion", "payload": { "status": "completed" } }
  ]
}
```

Or attach to the live stream with [GET /api/sessions/{session_id}/stream](endpoint:GET /api/sessions/{session_id}/stream). This is Server-Sent Events, so pass the key as a query parameter:

```bash
curl -N "http://localhost:5125/api/sessions/9e2d47c1a0b34f12/stream?api_key=$MEWBO_API_KEY"
```

The stream replays the stored backlog once, then pushes each new event the moment it is appended. Pass `after` with a timestamp to resume a dropped connection without re-downloading the transcript, and watch for the `session_state` frame carrying the same authoritative run state the polling response returns. A terminal `stream_end` event marks the run finished. See [Building a Client](building-a-client.md) for the full contract.

## What is in this section

| Page | What it covers |
|---|---|
| [Building a Client](building-a-client.md) | The full session lifecycle: sessions, queries, streaming, follow-ups, steering, and permanent termination. Start here to write your own client. |
| [Structured Outputs](structured-outputs.md) | Constrain a run to a JSON Schema and get a validated object back. Includes the fast synthesis mode and token streaming. |
| [Automation](automation.md) | Drive Mewbo from CI. Assign a bot to an issue or pull request and it picks the work up. |
| [Triggers](triggers.md) | Let a session arm a durable wake instead of polling: time, cron, CI, pull request, or webhook. |
| [Device Tool Bridge](device-tools.md) | Let a connected client register on-device tools that the agent can call. |

## The full reference

This section is the guided tour. The complete endpoint catalog, with every parameter, response shape, and a ready-to-run request sample for each route, is the [REST API Reference](../rest-api.md). It is generated from the live server, so it always matches the running code. When you need the exact schema for a route, that is the authoritative source.

## Next steps

- [Building a Client](building-a-client.md) walks the session lifecycle in full.
- [Structured Outputs](structured-outputs.md) covers schema-constrained and low-latency runs.
- [REST API Reference](../rest-api.md) is the complete, generated endpoint catalog.
