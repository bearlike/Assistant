> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura Data Layer — Wire Contract & Reducer Guidance

Scope: `data/` — API client, SSE, domain model, repos, settings. Spec: Gitea
#175 (full field-for-field contract lives there as a comment, verified
against `mewbo_api/backend.py` + `mewbo_core` source 2026-07-01). No
Compose/UI imports in this package, ever.

## Contract facts that differ from naive assumptions (verified)

- **Auth**: `X-API-Key` header; SSE uses `?api_key=` query param
  (EventSource can't set headers). `X-Mewbo-Surface: android` for trace
  provenance; `X-Mewbo-Capabilities` omitted (plain chat client).
- **`POST /api/sessions`** creates the session shell only — no run starts.
  **Two send routes, console-parity split (v3/#177):** fresh turn on an
  idle session → `POST /{id}/query` `{query, context, mode, attachments}`;
  `POST /{id}/message` `{text}` is ONLY for steering an active run (202;
  attachments impossible there). `/message` on an idle session re-engages
  and (since `80ba966`) inherits the persisted context — but offers no way
  to CHANGE it; that's what `/query` is for.
- **Scope context must be RESENT on every `/query`.** The backend
  re-resolves `model`/`project`/`mcp_tools` from EACH request's own
  context, nothing persists across turns for scoping — a partial or
  omitted context silently reverts turn 2+ to the config-default model and
  a temp-dir cwd (`SessionContext.kt` is the single assembly point).
  `mcp_tools` omitted ⇒ ALL tools bound; non-empty ⇒ allowlist.
- **`GET api/models`** → `{models, default, capabilities{supports_vision}}`;
  list entries are bare but `default` is provider-prefixed
  ("openai/claude-…") — normalize before comparing (`ModelCatalog`).
- **Attachments are a two-step flow**: multipart `POST /{id}/attachments`
  (repeated `files` field + optional `model` for eager vision rejection) →
  response records go VERBATIM into `/query`'s `attachments`. Server side:
  docs → markdown into the system prompt, images → real multimodal parts
  on vision models only. The transcript never renders them back.
- **`context` events decode as `Unknown`** (no dedicated variant); the raw
  JSON still matters — `SessionEvent.lastContextModel` reads `payload.model`
  back out so an opened session's header shows the model it actually runs.
- **OkHttp + coroutines trap**: an `enqueue`-based `await()` resumes on the
  CALLER's dispatcher, and `ResponseBody.string()` is a blocking socket
  read — from a ViewModel that's Main ⇒ fatal `NetworkOnMainThreadException`
  on any not-yet-buffered body (the 401 path). Pin call + body read to
  `Dispatchers.IO` (`ConnectionProbe`).
- **There is NO `error` event type on session streams.** Failure surfaces
  are: `completion` (`done_reason`/`error`/`last_error`), `tool_result`
  (`success:false` + `error`), `llm_call_end` (`success:false`). Never
  switch on `type == "error"` — it will never fire (that type belongs to
  the wiki/search event families only).
- **No event ids.** Events are `{type, ts, payload}` — dedupe by content
  key `"$ts|$type|<payload-json>"` (same as the web console). `/events?after=`
  is strictly-greater ISO-ts filtering.
- **SSE frames are bare `data: <json>`** — no `id:`/`event:` lines (unlike
  the wiki/search SSE family — one parser does NOT fit both). Heartbeat =
  comment line `: heartbeat` every 15s; idle auto-close after 300s;
  terminal frame `data: {"type":"stream_end"}` (no `ts`). **Reconnect
  replays the FULL backlog** — idempotent reduction + content-key dedupe is
  what makes reconnects safe, not a resume cursor.
- `user_steer` (not `user`) is the type recorded for a message enqueued
  into an active run — render it as a user bubble and dedupe optimistic
  echo against BOTH `user` and `user_steer`.
- `todos` payload `{items:[{label,status}], source, agent_id}` is
  authoritative and re-emitted in full — REPLACE the whole checklist per
  event, never merge.
- `agent_message_delta` (`text, agent_id, depth, step`) is the ONLY
  incremental-text source; `agent_message` is the authoritative
  replace-buffer for the step; `assistant` finalizes the turn (exactly one
  per turn, even on failure).
- **`recoverable`** (backend session field) means "this run finished ALL its
  work SUCCESSFULLY" — session-LIST "offer Continue?" semantics, not "can
  the user still type in this open chat." Don't wire it to composer-gating.
- **Sessions-list `running: Boolean`** is the actual liveness signal — the
  `status` string itself never contains `"running"` (observed values:
  `completed`/`failed`/`awaiting_approval`/`canceled`/`idle`).

## TranscriptReducer invariants (the contract test suite)

These invariants are the layout-side of the design system:
[`DESIGN.md`](../../../../../../../../DESIGN.md) §6 states the turn/disclaimer shape they guarantee,
and DESIGN.md §8's enforcement map names `TranscriptReducer` +
`TranscriptReducerTest` as where ordering is enforced ("the data structure enforces the layout",
§1) — the UI must never re-derive or "fix" ordering positionally.

- Pure + idempotent on event identity: `reduce(events)` ==
  `reduce(events + replayed-tail)` — reconnect/backfill safety lives HERE.
- Transcript renders **root-agent text only** (`depth == 0`) — sub-agent
  narration (`depth > 0`) stays out of the chat lane.
- Delta accumulation → one in-flight assistant item; `agent_message`
  replaces the buffer; `completion`/`stream_end` close streaming state.
- **Activity-precedes-narration (turn ordering, contract-tested).** Within a
  turn, activity items (`ToolCallGroup`/`AgentChip`/`TodoList`) always precede
  the turn's assistant-text item, and the text item is always the turn's LAST
  item (`ErrorCard` excepted — see below). Real sessions interleave
  `tool_result` → narration → `tool_result` → narration → `assistant` in
  arrival order (verified 9/122 aura turns); the old arrival-order append
  rendered a trailing tool group BELOW the response and its action-row footer.
  Mechanism: an activity item first *created* while the turn's assistant item is
  already open is INSERTED at that item's index (`appendOrInsertBeforeOpenAssistant`),
  not appended; in-place *updates* to an existing item still `upsert` in place.
- **One tool group per turn.** Consecutive `tool_result`s fold into the turn's
  single open `ToolCallGroup`; only a fresh `user`/`user_steer` message closes
  it (`closeToolGroup` clears `openToolGroupKey`). Narration opening/continuing
  the assistant-text item NO LONGER closes the group — a turn's tool calls and
  its narration interleave freely and all belong to one group.
- **`ErrorCard` is gated on a real run FAILURE** — `completion.error`, which the
  backend sets only on the orchestrator's terminal-failure path. A successful
  run's residual `last_error` (e.g. one MCP call failed mid-run then recovered)
  spawns NO card (user directive: never surface tool-error residue under a
  successful chat); when `error` IS present the message prefers it, falling back
  to `last_error` only then. This card always appends at the very end, never
  through the insert-before-assistant path — it is not a per-turn activity item.
- Ignored types (`context`, `run_accepted`, `llm_*`, `title_update`, …) are
  parsed (Unknown-safe) but dropped by the reducer.
  Unknown future types must never crash parsing — polymorphic serializer
  with an `Unknown` fallback keyed on `type`.

## Layering

`SessionStreamClient` (data/sse) owns ALL OkHttp-SSE mechanics: backoff
0.5s→15s, `/events?after=` backfill, frame parsing. Repos compose it; UI
sees only `Flow<SessionEvent>`. Retrofit for REST; kotlinx.serialization
with `ignoreUnknownKeys=true` everywhere.

## Settings

`SettingsStore` (DataStore-preferences). API key at rest: Android Keystore
AES/GCM wrap (`KeystoreCipher`), ciphertext in DataStore — no extra
dependency for one secret.
