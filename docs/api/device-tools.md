# Device Tool Bridge

Some tools can only run on the client. A phone can send an SMS, read a sensor, or toggle a setting that the server has no access to. The device tool bridge lets a connected client declare those tools to a session. The agent then calls them like any other tool. The client executes the call on-device and posts the result back. To the agent, a device tool looks exactly like a built-in one.

This is how the mobile client exposes device capabilities to the agent. The client declares what it can do, the agent decides when to call it, and the client fulfills the call.

## The flow

```
1. DECLARE   client -> POST /query  (context.device_tools: [ ... ])
2. CALL      agent calls the tool
3. DELIVER   server -> device_tool_call event on the session SSE stream
4. EXECUTE   client runs the tool on-device
5. RESULT    client -> POST .../device_tools/{call_id}/result
6. RESOLVE   the agent's tool call returns; the run continues
```

The declaration rides on the session context. The pending call is delivered over the session's existing Server-Sent Events stream. The result comes back on one dedicated route. There is no separate registration endpoint and no polling endpoint. The bridge reuses the session stream you are already reading.

## 1. Declare the tools

A client declares its device tools in the `context.device_tools` field of a request body. Pass it on [POST /api/sessions/{session_id}/query](endpoint:POST /api/sessions/{session_id}/query). The value is an array of tool specifications:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/query" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Text mom that I will be late.",
    "context": {
      "device_tools": [
        {
          "tool_id": "device_send_sms",
          "description": "Send an SMS to a phone number.",
          "parameters": {
            "type": "object",
            "properties": {
              "to":   { "type": "string" },
              "body": { "type": "string" }
            },
            "required": ["to", "body"]
          }
        }
      ]
    }
  }'
```

Each entry has three fields:

| Field | Required | Description |
|---|---|---|
| `tool_id` | yes | The tool name the agent calls. Must match the pattern `device_` followed by 1 to 48 lowercase letters, digits, or underscores. |
| `description` | yes | A non-empty description. This is what the agent reads to decide when to call the tool. |
| `parameters` | yes | A JSON Schema object describing the tool's arguments. It becomes the tool's function schema unchanged. If a `type` is present it must be `object`. |

The mandatory `device_` prefix on `tool_id` is a namespace guard. It prevents a client-declared tool from shadowing a built-in or plugin tool. A declaration with a malformed spec, or with two entries sharing a `tool_id`, is rejected with `400` before anything is persisted, so a bad declaration cannot poison the session.

You can also declare device tools on [POST /api/sessions](endpoint:POST /api/sessions) at create time. They are persisted with the session there. The tools are validated and bound to a run when you send a query. Re-sending the declaration on each query keeps it current, and re-engaging an idle session tolerates a previously persisted declaration rather than failing.

## 2. Receive the pending call

When the agent calls a declared tool, the server appends a `device_tool_call` event to the session. You receive it on the session's SSE stream, [GET /api/sessions/{session_id}/stream](endpoint:GET /api/sessions/{session_id}/stream):

```bash
curl -N "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/stream?api_key=$MEWBO_API_KEY"
```

The event arrives as a frame like this:

```
data: {"type": "device_tool_call", "payload": {
  "call_id": "3f9a1c2b7d5e4a6f",
  "call_token": "Q1sT9x...redacted",
  "tool_id": "device_send_sms",
  "args": { "to": "+15550001234", "body": "Running late, be there soon." },
  "expires_at": 1720000030.0
}}
```

The payload fields:

| Field | Description |
|---|---|
| `call_id` | Identifies this specific call. You put it in the result URL. |
| `call_token` | A single-use token you present when posting the result. It authorizes exactly this call. |
| `tool_id` | The declared tool the agent is calling. |
| `args` | The arguments the agent passed, matching your `parameters` schema. |
| `expires_at` | An epoch-seconds deadline. The server stops waiting for a result after this time. |

Note the field names. The arguments object is `args`, not `arguments`, and the tool name is `tool_id`, not `name`.

The call is delivered only if a client is attached to the session's SSE stream. If nobody is listening, the agent's tool call fails immediately with a `device_unavailable` error, and no event is appended. Attach to the stream before you send a query that may call a device tool.

## 3. Execute and post the result

Run the tool on-device, then deliver the outcome to [POST /api/sessions/{session_id}/device_tools/{call_id}/result](endpoint:POST /api/sessions/{session_id}/device_tools/{call_id}/result). Use the `call_id` from the event in the path, and present the `call_token` in the body.

On success, set `status` to `ok` and put the tool's return value in `result`:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/device_tools/3f9a1c2b7d5e4a6f/result" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "call_token": "Q1sT9x...redacted",
    "status": "ok",
    "result": { "delivered": true, "message_id": "sms_88213" }
  }'
```

The server confirms delivery:

```json
{ "resolved": true }
```

The agent's tool call resolves with your `result`, and the run continues.

On failure, set `status` to `error` and describe it in an `error` object. The object needs a non-empty `code` or `message`:

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/device_tools/3f9a1c2b7d5e4a6f/result" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "call_token": "Q1sT9x...redacted",
    "status": "error",
    "error": { "code": "permission_denied", "message": "The user denied SMS permission." }
  }'
```

The agent sees the call as a failed tool step and reasons about the failure.

The result body fields:

| Field | Required | Description |
|---|---|---|
| `call_token` | yes | The single-use token from the `device_tool_call` event. |
| `status` | yes | `ok` or `error`. |
| `result` | when `ok` | The tool's return value. Any JSON. |
| `error` | when `error` | An object with `code` and `message`. At least one must be non-empty. |

### Response codes

| Status | Meaning |
|---|---|
| `200` | The result was delivered. The waiting tool call resolves. Body is `{"resolved": true}`. |
| `400` | The body was malformed. For example a missing `status`, or `status: "error"` with an empty `error`. |
| `403` | The `call_token` did not match the pending call. |
| `404` | No pending call with that `call_id`. It is unknown, expired, or already delivered. |
| `409` | A result was already delivered for this call. |

## Timeouts and single delivery

A pending call has a wait budget of 30 seconds. If no result arrives in time, the agent's tool call fails with a `device_timeout` error and the run continues without your result. The `expires_at` field on the event carries the exact deadline, so a client can decide whether it is still worth answering.

A result may be delivered exactly once. The first valid POST wins and resolves the call. A second POST for the same call returns `409`. This single-delivery rule guards against a duplicated real-world side effect, such as sending the same SMS twice.

## Security model

The `call_token` proves that the caller can read the session's SSE stream, and it prevents replay of a delivered result. It is not proof that the result came from the specific device the call was dispatched to. Any client viewing the session's SSE stream receives the same token. Treat the token as a per-call capability scoped to session-stream access, not as a device identity. Verified per-device identity is a future phase.

Because the result route is a normal protected endpoint, the caller also needs a valid API key, the same as every other session route.

## Implementation

The wire contract and its guarantees are defined in source:

- The client-facing spec and the core dispatch seam are in [`client_tools.py`](repo:packages/mewbo_core/src/mewbo_core/client_tools.py).
- The pending-call registry, the timeout constant, and the dispatcher are in [`device_tools.py`](repo:apps/mewbo_api/src/mewbo_api/device_tools.py).
- The result route and the request and response models are in [`backend.py`](repo:apps/mewbo_api/src/mewbo_api/backend.py).

The exact request and response schemas for the result route are in the generated [REST API Reference](../rest-api.md).

## Next steps

- [Building a Client](building-a-client.md): the SSE stream that delivers device tool calls.
- [Device Tools on Android](../android/device-tools.md): the same bridge from the phone's side.
- [Full API reference](../rest-api.md): every route, parameter, and response shape.
