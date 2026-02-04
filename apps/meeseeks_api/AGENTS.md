# Meeseeks API - Project Guidance

Scope: this file applies to the `apps/meeseeks_api/` package. It captures runtime behavior, hidden dependencies, and testing notes so changes stay safe and predictable.

## Runtime flow (what actually happens)
- Entry point: `apps/meeseeks_api/src/meeseeks_api/backend.py` (HTTP API framework).
- Single endpoint: `POST /api/query`.
- Auth: requires `X-API-KEY` header. Token defaults to `msk-strong-password` if `MASTER_API_TOKEN` is not set.
- Orchestration: calls `meeseeks_core.task_master.orchestrate_session(...)` with `auto_approve` and a shared `SessionStore`.
- Sessions: supports `session_id`, `session_tag`, and `fork_from` (tag or id). Tags are resolved via `SessionStore`.

## Hidden dependencies / assumptions
- Loads `.env` via an env loader at import time.
- Uses core logging (`meeseeks_core.common.get_logger`); log level controlled by core env.
- Relies on core LLM config env: `OPENAI_API_BASE` / `OPENAI_API_KEY`, `DEFAULT_MODEL`, `ACTION_PLAN_MODEL`.
- No rate limiting or auth hardening beyond the header token.

## Pitfalls / gotchas
- `MASTER_API_TOKEN` default is insecure; production should override it.
- No heartbeat or health endpoint; external deployments must handle liveness checks.
- The API returns the whole `TaskQueue` including action steps; ensure tool results are safe to expose.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Testing guidance
- `apps/meeseeks_api/tests` mock `orchestrate_session` and focus on response schema.
- Avoid mocking too much of core: keep at least one integration test that exercises `SessionStore` behavior.

## Cross-project insights (fast decision help)
- Explicit tool allowlists and permission gates reduce unsafe actions; keep API calls explicit and auditable.
- Clear turn boundaries help keep outputs stable; avoid mixing raw tool output with the final response.
- Keep the API surface small and obvious; avoid hidden behaviors.
