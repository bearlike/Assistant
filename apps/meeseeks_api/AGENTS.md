# Meeseeks API - Project Guidance

Scope: this file applies to the `apps/meeseeks_api/` package. It captures runtime behavior, hidden dependencies, and testing notes so changes stay safe and predictable.

## Runtime flow (what actually happens)
- Entry point: `apps/meeseeks_api/src/meeseeks_api/backend.py` (HTTP API framework).
- Session endpoints:
  - `POST /api/sessions` create session
  - `GET /api/sessions` list sessions
  - `POST /api/sessions/{session_id}/query` enqueue run or core command
  - `GET /api/sessions/{session_id}/events?after=...` poll events
  - `POST /api/query` legacy synchronous endpoint
- Auth: requires `X-API-KEY` header. Token defaults to `api.master_token` from `configs/app.json` (default: `msk-strong-password`).
- Orchestration: uses `meeseeks_core.session_runtime.SessionRuntime` to run sync/async sessions.
- Core commands: `/compact`, `/status`, `/terminate` (shared runtime).
- Sessions: supports `session_id`, `session_tag`, and `fork_from` (tag or id). Tags are resolved via `SessionStore`.

## Hidden dependencies / assumptions
- Uses core logging (`meeseeks_core.common.get_logger`); log level controlled by `runtime.log_level`.
- Relies on core LLM config (`llm.api_base`, `llm.api_key`, `llm.default_model`, `llm.action_plan_model`).
- No rate limiting or auth hardening beyond the header token.

## Pitfalls / gotchas
- `api.master_token` default is insecure; production should override it in `configs/app.json`.
- No heartbeat or health endpoint; external deployments must handle liveness checks.
- The API returns the whole `TaskQueue` including action steps; ensure tool results are safe to expose.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Testing guidance
- `apps/meeseeks_api/tests` mock `SessionRuntime.run_sync` and focus on response schema.
- Avoid mocking too much of core: keep at least one integration test that exercises `SessionStore` behavior.

## Cross-project insights (fast decision help)
- Explicit tool allowlists and permission gates reduce unsafe actions; keep API calls explicit and auditable.
- Clear turn boundaries help keep outputs stable; avoid mixing raw tool output with the final response.
- Keep the API surface small and obvious; avoid hidden behaviors.
