# Meeseeks HA Conversation - Project Guidance

Scope: this file applies to `meeseeks_ha_conversation/` (home automation integration).

## Runtime flow
- Entry point for API calls: `meeseeks_ha_conversation/api.py`.
- Uses an async HTTP client + timeout helper to call `POST /api/query` on the Meeseeks API.
- Returns a parsed `MeeseeksQueryResponse` to the home automation host.

## Hidden dependencies / assumptions
- Uses a hardcoded API key in `MeeseeksApiClient` (`msk-strong-password`). Override if you change the API auth behavior.
- Assumes `base_url` includes protocol and is reachable from the host.
- `async_get_models` is currently stubbed (static response).

## Pitfalls / gotchas
- API failures raise `ApiJsonError` or client exceptions; callers must handle these.
- Conversation history is not yet used; do not assume multi-turn context is preserved.
- Must remain fully async; avoid blocking calls or synchronous HTTP.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Testing guidance
- Use async client test utilities or monkeypatch `_session.request` for deterministic behavior.
- Validate error mapping (non-2xx -> exception) and JSON parsing.

## Cross-project insights
- Explicit approval flows are key; keep this integration purely a client to avoid mixing policy with transport.
- Keep the transport layer slim; avoid embedding orchestration logic here.
