# Triggers

A session doesn't have to stay open to wait for something. An agent can arm a **trigger**, a durable "wake me up" record, and end its turn. Nothing polls, nothing burns tokens waiting. When the trigger's condition is met, later, possibly much later, Mewbo re-engages the session with the instruction the agent left for itself.

This is the reverse of [Automation](automation.md). Automation is the world pushing work *into* Mewbo (a CI pipeline assigns an issue). A trigger is a session reaching *out* to be woken later by a clock, a CI run, a pull request event, or a webhook.

## What an agent uses it for

Inside a running session, the model has a `schedule_trigger` tool. It arms a trigger, ends the turn, and waits:

> "I started the deploy, but it takes twenty minutes to finish. Instead of sitting here polling, I'll arm a trigger to check the CI run in twenty minutes and end my turn now."

When the trigger fires, the orchestrator re-invokes the same session with the trigger's `wake_prompt`, the instruction the agent wrote for its future self. The session picks up with its full history intact, as if the agent had just kept working.

`schedule_trigger` also lists and cancels the triggers the current session has armed. Arming is deliberately scoped to the session's own agent turn: it is not exposed as a general-purpose REST mutation, only as something an agent does to itself. See [`session_tool.py`](repo:packages/mewbo_core/src/mewbo_core/triggers/session_tool.py).

## The five kinds

Every trigger declares one `kind`. There is no free-form scripting: the agent picks a kind and fills in that kind's fields, defined in [`spec.py`](repo:packages/mewbo_core/src/mewbo_core/triggers/spec.py).

| Kind | Fires when | Notes |
|---|---|---|
| `time.at` | A specific wall-clock instant passes | One-shot; fires exactly once. |
| `time.cron` | A cron schedule is due | Repeats. Anchored on the last fire (not "now"), so time offline never causes a catch-up burst; the missed slot fires once and re-anchors. |
| `ci.workflow` | A CI run on a repo completes | Matches a specific `run_id`, or the next completed run of a named `workflow`. Optionally filtered to a `ref` or a set of conclusions (`success`, `failure`, ...). |
| `forge.pr` | A pull request reaches a lifecycle event | Events: `merged`, `review`, `comment`, `ci_status`. See the polling limitation below, not every event reaches this kind the same way. |
| `webhook` | An inbound HTTP request presents the right secret | Fires on anything: a third-party service, a script, a manual `curl`. See the capability URL below. |

A trigger can cap how many times it fires (`max_fires`), and it expires on its own if nothing happens by a deadline (`expires_at`, defaulted per deployment when the agent doesn't set one).

## Watching and managing triggers

Outside the session that armed them, triggers are visible over the REST API and the console:

| Action | Endpoint |
|---|---|
| List triggers, filterable by session, kind, or status | [GET /api/triggers](endpoint:GET /api/triggers) |
| List one session's triggers | [GET /api/sessions/{session_id}/triggers](endpoint:GET /api/sessions/{session_id}/triggers) |
| Pause or resume a trigger | [PATCH /api/triggers/{trigger_id}](endpoint:PATCH /api/triggers/{trigger_id}) with `{"status": "paused"}` or `{"status": "armed"}` |
| Cancel a trigger | [DELETE /api/triggers/{trigger_id}](endpoint:DELETE /api/triggers/{trigger_id}) — idempotent; calling it again on an already-finished trigger just reports its status |

A **paused** trigger stops polling or matching, but keeps its state; resuming it picks up where it left off. A trigger that has fired its last allowed time, failed too many times in a row, expired, or was cancelled is **terminal**: it never runs again.

The console's trigger dashboard is this same surface for a human operator: it lists every trigger, filters by kind, status, and session, and offers pause, resume, and cancel from the same endpoints.

The MCP server exposes the read side to external agents too: `list_triggers` and `cancel_trigger`. There is deliberately no MCP tool to *create* a trigger. Arming stays an in-session capability, something an agent does to itself, never a general mutation another agent reaches in and does to a session it doesn't own. See [MCP Server](../clients-mcp.md).

## The webhook capability URL

A `webhook` trigger doesn't need an API key. Arming one mints a unique, unauthenticated URL:

```
POST /api/triggers/hook/{trigger_id}/{secret}
```

The id and secret together are the credential. Anything that can send an HTTP POST to that URL, an external service's webhook settings, a script, a manual test, fires the trigger. If the agent also set an `hmac_header`, the request additionally needs a valid HMAC-SHA256 signature over the raw body in that header, verified before anything else happens.

A wrong secret, an unknown id, a paused or already-finished trigger, and a trigger of the wrong kind all return the same `404`. There is no way to probe which reason applies, so a URL leak doesn't hand out information about what exists.

## The forge-polling limitation

`ci.workflow` and `forge.pr` triggers are serviced by polling the forge's REST API on a fixed cadence (`triggers.poll_interval_seconds`), not by a live webhook feed. Polling cleanly detects **terminal, idempotent** state: a CI run finished, a PR was merged, a PR's combined CI status settled. Those poll cleanly because re-checking the same terminal state twice is harmless.

It deliberately does **not** synthesize `review` or `comment` events. Without tracking exactly which reviews or comments a session has already seen, re-announcing "a comment exists" on every poll would either spam the session or lie about what's new. A `forge.pr` trigger listing only `review` or `comment` in its `events` will never fire from the poller.

Those two events already have a home: a PR review or a comment mentioning the bot account is delivered through [Automation](automation.md)'s `vcs-pickup` mention path, or through a `webhook` trigger wired to the forge's own webhook settings, either delivers the event the moment it happens instead of waiting on a poll.

## Configuration

The whole subsystem is off by default. A stock deployment pays nothing for it until an operator turns it on:

| Key | Default | Purpose |
|---|---|---|
| `triggers.enabled` | `false` | Master switch for the watcher that fires due triggers. When off, the management routes above still work (list, arm, pause, cancel), nothing actually fires until this is on. |
| `triggers.tick_interval_seconds` | `5.0` | How often the watcher wakes to check `time.at` / `time.cron` triggers and sweep expired ones. |
| `triggers.poll_interval_seconds` | `60.0` | Cadence for polling the forge REST API for `ci.workflow` / `forge.pr` triggers. |
| `triggers.max_consecutive_failures` | `5` | A trigger whose fire or poll errors this many times in a row moves to `failed` instead of retrying forever. |
| `triggers.max_armed_per_session` | `20` | Ceiling on currently-armed triggers one session may hold. |
| `triggers.max_fires_cap` | `100` | Hard ceiling on a trigger's own `max_fires` at arm time. |
| `triggers.default_expiry_days` | `7.0` | Expiry stamped on an armed trigger that doesn't set its own `expires_at`. |
| `triggers.cron_min_interval_seconds` | `60` | Minimum allowed gap between two consecutive cron fires (rejects an overly aggressive schedule at arm time). |
| `triggers.webhook_payload_max_bytes` | `200000` | Ceiling on an inbound webhook body; a larger payload is truncated, not rejected. |

See the [Configuration Reference](../configuration.md) for the full schema. The admission limits mirror [`TriggerPolicy`](repo:packages/mewbo_core/src/mewbo_core/triggers/policy.py) field-for-field; the firing pipeline that polls the forge and re-invokes sessions lives in [`service.py`](repo:apps/mewbo_api/src/mewbo_api/triggers/service.py).

## Related resources

- [MCP Server](../clients-mcp.md): the external `list_triggers` / `cancel_trigger` tools.
- [Automation](automation.md): the other direction, work pushed into Mewbo from CI.
- [Building a Client](building-a-client.md): permanently terminating a session cascade-cancels its armed triggers.
- [REST API Reference](../rest-api.md): every trigger route, parameter, and response shape.
