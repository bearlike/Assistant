> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura SSE — data/sse/

Scope: `data/sse/` — `SessionStreamClient`, which owns ALL OkHttp-SSE mechanics and exposes
`stream(sessionId): Flow<SessionEvent>` via `callbackFlow`. Repos compose it; the UI sees only the flow.

## Frame + reconnect contract

- **Bare `data: <json>` frames** — no `id:`/`event:` lines, unlike the wiki/search SSE family; one
  parser does not fit both. Each `onEvent` → `SessionEvent.decode`. Heartbeat is a comment line
  (`: heartbeat`, never reaches `onEvent`); the terminal frame decodes to `SessionEvent.StreamEnd`, a
  domain value rather than a transport signal. Auth is the `?api_key=` query param, because
  EventSource cannot set headers.
- Backoff `INITIAL_BACKOFF_MS = 500` → `MAX_BACKOFF_MS = 15_000` (×2, reset on a clean connect).
  Reconnect loop `while (isActive && !terminated)`; `CancellationException` rethrown, `IOException`
  swallowed → backoff+reconnect.
- **The FIRST connect replays the full backlog; a RECONNECT carries `?after=<newest ts seen>`.** The
  stream endpoint does support that cursor (`backend.py`'s stream generator trims its once-only replay
  to that timestamp or later), and using it is not optional politeness: the server closes an IDLE
  session's stream within milliseconds, so anything that re-subscribes — notably the device-control
  hold ([`notify/CLAUDE.md`](../../notify/CLAUDE.md)) — would otherwise re-transfer the whole
  transcript every few seconds.
- **The server's `after` is INCLUSIVE, which is the whole reason it is compatible with the next rule.**
  Everything sharing the cursor's `ts` is re-sent, so no event can be lost by resuming. What stays
  banned is a CLIENT-side strictly-greater filter: that one silently drops an event sharing a `ts` with
  the last one seen, and would defeat the reducer's content-key dedupe. This class still filters and
  dedupes NOTHING — the cursor narrows the server's WORK, never the client's view of it. Idempotent
  reduction is what makes reconnects safe, so **dedupe belongs ONLY to `TranscriptReducer`**
  ([`data/model/CLAUDE.md`](../model/CLAUDE.md)). A separate `/events?after=` backfill cursor also
  exists as a REST call on [`AuraApi`](../api/CLAUDE.md); that one is not this.

## The `trySend` + `terminated`-regardless-of-landing hazard

Inside the connect callback: `trySend(event)` then `if (event is StreamEnd) terminated = true` —
**`terminated` is set regardless of whether `trySend` actually landed.** Under buffer pressure the
terminal frame can be DROPPED downstream while the loop still exits cleanly, so a collector that
terminates on the `StreamEnd` VALUE would never end. This is why `RunRepository` puts device-tool
dispatch INTO the pipeline rather than as a subscriber keyed on that frame
([`data/repo/CLAUDE.md`](../repo/CLAUDE.md)).
