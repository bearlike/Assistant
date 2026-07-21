> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura SSE — data/sse/

Scope: `data/sse/` — `SessionStreamClient`, which owns ALL OkHttp-SSE mechanics and exposes
`stream(sessionId): Flow<SessionEvent>` via `callbackFlow`. Repos compose it; the UI sees only the flow.

## Frame + reconnect contract

- **Bare `data: <json>` frames** — no `id:`/`event:` lines (unlike the wiki/search SSE family; one
  parser does not fit both). Each `onEvent` → `SessionEvent.decode`. Heartbeat is a comment line
  (`: heartbeat`, never reaches `onEvent`); the terminal frame decodes to `SessionEvent.StreamEnd`
  (a domain value, not a transport signal). Auth is the `?api_key=` query param (EventSource can't set
  headers), from `settingsStore.baseUrl`/`apiKey`.
- Backoff `INITIAL_BACKOFF_MS = 500` → `MAX_BACKOFF_MS = 15_000` (×2, reset on a clean connect).
  Reconnect loop `while (isActive && !terminated)`; `CancellationException` rethrown, `IOException`
  swallowed → backoff+reconnect.
- **Every (re)connect replays the FULL persisted backlog** — the stream endpoint has NO partial-resume
  cursor. This class deliberately does NOT filter or dedupe by `ts` (a strictly-greater cursor is lossy
  when two events share a `ts`, and would defeat the reducer's content-key dedupe). Idempotent reduction
  is what makes reconnects safe — dedupe belongs ONLY to `TranscriptReducer`
  ([`data/model/CLAUDE.md`](../model/CLAUDE.md)). The `/events?after=` backfill cursor is a REST call on
  [`AuraApi`](../api/CLAUDE.md), not here.

## The `trySend` + `terminated`-regardless-of-landing hazard (why dispatch is a pipeline step)

Inside the connect callback: `trySend(event)` then `if (event is StreamEnd) terminated = true` — **`terminated`
is set regardless of whether `trySend` actually landed.** Under buffer pressure the terminal frame can
be DROPPED downstream while the loop still exits cleanly, so a collector terminated on the `StreamEnd`
VALUE would never end. This is exactly why `RunRepository` puts device-tool dispatch INTO the pipeline
(dies with the upstream however it dies) rather than as a subscriber keyed on that frame — see
[`data/repo/CLAUDE.md`](../repo/CLAUDE.md).
