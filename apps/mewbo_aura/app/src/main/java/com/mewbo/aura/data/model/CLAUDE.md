> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Domain Model — data/model/

Scope: `data/model/` — the domain types + the `TranscriptReducer`. **This is the layout-enforcement
layer**: DESIGN.md §1 "the data structure enforces the layout" and §8's enforcement map both name
`TranscriptReducer` + `TranscriptReducerTest`. The UI must never re-derive or "fix" ordering
positionally.

## `TranscriptReducer` invariants (the contract suite)

- Pure + idempotent on event identity: `reduce(events) == reduce(events + replayed-tail)` — reconnect
  safety lives HERE, which is why `SessionStreamClient` neither dedupes nor cursor-filters
  ([`data/sse/CLAUDE.md`](../sse/CLAUDE.md)).
- Root-agent text only (`depth == 0`); sub-agent narration stays out of the chat lane.
- **Activity-precedes-narration:** within a turn, activity items precede the assistant text (the turn's
  LAST item, `ErrorCard` excepted). An activity item first created while the assistant item is already
  open is INSERTED at that item's index (`appendOrInsertBeforeOpenAssistant`), not appended.
- **One tool group per turn**; only a fresh `user`/`user_steer` closes it. Narration no longer closes
  the group — tools and narration interleave within one group.
- **Promoted tools bypass the group → `ChatItem.ToolCard`.** An allowlisted (`PromotedTools`) id whose
  call SUCCEEDED becomes its own top-level item. Three contract-tested invariants keep it safe: it
  returns BEFORE any group bookkeeping (never inflates "Used N tools", never disturbs `openToolGroupKey`);
  it still routes through `appendOrInsertBeforeOpenAssistant` (inherits ordering); it upserts on key
  (idempotent). `success` is part of the gate — a failed allowlisted call falls through to the normal
  fold (a confident card for a failed tool is the UI lying).
- **`widget_ready` → `ChatItem.Widget`** (`foldWidget`), keyed by `widget_id`, its own item
  like the promoted card. BOTH `app.py` and `data.json` required or the payload degrades to `Unknown`
  and is dropped (never a half-rendered widget).
- **`user_question` → `ChatItem.Question`** (`foldUserQuestion`), keyed `question:<callId>`, its own
  item via the activity-precedes-narration insert like the widget card. **Settle is authoritative
  from the `user_question_answered` EVENT** (`foldUserQuestionAnswered` flips `resolution` —
  `answered` → `Answered(answers, answeredVia)`, every other outcome → `Dismissed`), so the card
  settles even when ANOTHER surface answered; the local POST result only steers submit-state/toast.
  Orphan answered events (no matching card) are ignored. An unknown future `outcome` string reads as
  `Dismissed`, never a crash.
- **`ErrorCard` is gated on `completion.error` alone** — a successful run's residual `last_error`
  spawns NO card. Appends at the very end, never through the insert-before path.

## THE POISON ANCHOR — adopt the server `ts`, keep the client `key`

A `UserBubble.ts` is the `from_ts` that retry (`/recover`, matched by **exact string equality**) and
fork (`fork_session_at`, **lexicographic** compare) anchor on, consumed RAW by the backend. But
`ChatViewModel.send` stamps its optimistic echo with `Instant.now()` — a DEVICE clock in an ISO shape
the backend never emits (`…02.5Z`) versus Python `isoformat()`'s `…02.547517+00:00`. An unreconciled
bubble anchors NOTHING: retry 400s and a branch silently cuts at a timestamp that exists nowhere,
dropping the very message you branched from. So `foldUserText`'s dedupe branch adopts the server's `ts`
**unconditionally** — and **the `key` must NOT follow** (key = LazyColumn identity, stable; ts = server
anchor, authoritative). Gating adoption on `pending` does not save you (an ordinary fresh send is not
pending, carries the same wrong ts, and was skipping reconciliation). `user_steer` can anchor a FORK
but never a RETRY (`/recover` matches `type == "user"` only; fork reads `ts` alone), so `steer` is also
adopted down the dedupe branch.

## The rest of the model

- **`SessionEvent`** — sealed union, polymorphic serializer with an `Unknown` fallback keyed on `type`
  (an unknown future type must never crash parsing). **There is NO `error` event type** on session
  streams; `user_steer` (not `user`) is recorded for a message enqueued into an active run; `todos`
  payloads REPLACE the whole checklist per event; `agent_message_delta` is the only incremental-text
  source, `agent_message` the replace-buffer, `assistant` the turn-finalizer. `context` decodes
  `Unknown` but `lastContextModel` reads `payload.model` back out.
- **`PromotedTools`** — the ONE allowlist, a data-side transcript-STRUCTURE decision (which items
  exist), never presentation; [`ui/chat/toolcards`](../../ui/chat/toolcards/CLAUDE.md) owns rendering +
  a generic fallback, so the two drift freely.
- **`ComposerScope`** — `toolsFacetSummary` groups active tools by `ToolSummary.scope` (order
  project→system→plugin→builtin→other) but falls back to the plain "N tools" total when no active tool
  carries a recognized scope — never a lone, meaningless "N other." Counts sum to `activeToolCount` by
  construction (unit-tested); pure logic on the model.
- **`Timestamps.parseInstantOrNull`** — the ONE ISO parser (`OffsetDateTime.parse` first, `Instant.parse`
  fallback). Android's bundled `java.time` throws on a numeric offset (`+00:00`) that desktop JVMs
  accept — this shipped as three silently-broken copies before unification. Every ts call site routes
  through it (never a second local `Instant.parse`, even in tests).
- `ModelCatalog` (normalize the provider-prefixed `default` before comparing bare list entries),
  `ToolSummary`, `StagedAttachment`, `Session`/`SessionSummary` (`running: Boolean` is the liveness
  signal — `status` never literally reads `"running"`; `terminated`/`terminated_at` are durable),
  `ProjectSummary` (`contextKey` = bare name or `managed:<id>`), `ChatItem`.
