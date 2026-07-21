> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo HA Conversation - Project Guidance

Scope: this file applies to `apps/mewbo_ha_conversation/` (home automation integration).

## Runtime flow
- Entry point for API calls: `apps/mewbo_ha_conversation/api.py`.
- Uses an async HTTP client + timeout helper to call `POST /api/query` on the Mewbo API.
- Returns a parsed `MewboQueryResponse` to the home automation host.
- **Continuity rides the `session_id` field of `POST /api/query`.** The legacy sync endpoint accepts `session_id` to continue an existing server session (verified: `backend.py` `MewboQuery.post` → `runtime.resolve_session`, `sync_query_model`). `MewboAgent.query` just forwards the id stored per conversation; there is no separate sessions-API handshake and no server-side session tag. Chosen because it is the smallest working path over the endpoint the client already calls.

## Hidden dependencies / assumptions
- `MewboApiClient` requires an `api_key` (sent as `X-API-KEY`), threaded from the config entry in `async_setup_entry` (the composition root). A legacy entry with no stored key falls back to the `msk-strong-password` default and logs a deprecation warning — do not re-add the hardcoded key to the client.
- Assumes `base_url` includes protocol and is reachable from the host.
- `async_get_models` is currently stubbed (static response).

## Pitfalls / gotchas
- API failures raise `ApiJsonError` or client exceptions; callers must handle these.
- Multi-turn context IS preserved: `self.history[conversation_id]` stores the last response's `session_id` and `MewboAgent.query` replays it on the next turn. Continuity breaks silently if a change stops forwarding that stored id.
- Must remain fully async; avoid blocking calls or synchronous HTTP.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Testing guidance
- Use async client test utilities or monkeypatch `_session.request` for deterministic behavior.
- Validate error mapping (non-2xx -> exception) and JSON parsing.

## Cross-project insights
- Explicit approval flows are key; keep this integration purely a client to avoid mixing policy with transport.
- Keep the transport layer slim; avoid embedding orchestration logic here.
