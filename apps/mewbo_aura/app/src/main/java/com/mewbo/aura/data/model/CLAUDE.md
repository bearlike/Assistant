> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Domain Model — data/model/

Scope: `data/model/` — the domain types and the `TranscriptReducer`. **This is the layout-enforcement
layer: the data structure enforces the layout, and the UI must never re-derive or "fix" ordering
positionally.** `TranscriptReducerTest` is the contract suite.

## `TranscriptReducer` invariants

- **Pure + idempotent on event identity:** `reduce(events) == reduce(events + replayed-tail)`.
  Reconnect safety lives HERE, which is why `SessionStreamClient` neither dedupes nor cursor-filters
  ([`data/sse/CLAUDE.md`](../sse/CLAUDE.md)).
- Root-agent text only (`depth == 0`); sub-agent narration stays out of the chat lane.
- **Activity-precedes-narration:** within a turn, activity items precede the assistant text (the
  turn's LAST item, `ErrorCard` excepted). An activity item created while the assistant item is
  already open is INSERTED at that item's index (`appendOrInsertBeforeOpenAssistant`), not appended.
- **One tool group per turn**, closed only by a fresh `user`/`user_steer`. Tools and narration
  interleave within the one group.
- **Promoted tools bypass the group → `ChatItem.ToolCard`.** An allowlisted (`PromotedTools`) id whose
  call SUCCEEDED becomes its own top-level item. Three contract-tested invariants keep it safe: it
  returns BEFORE any group bookkeeping (never inflates "Used N tools", never disturbs
  `openToolGroupKey`); it still routes through `appendOrInsertBeforeOpenAssistant`, inheriting
  ordering; it upserts on key. **`success` is part of the gate** — a failed allowlisted call falls
  through to the normal fold, because a confident card for a failed tool is the UI lying.
- **`widget_ready` → `ChatItem.Widget`** (`foldWidget`), keyed by `widget_id`. BOTH `app.py` and
  `data.json` are required, or the payload degrades to `Unknown` and is dropped — never a
  half-rendered widget.
- **`user_question` → `ChatItem.Question`** (`foldUserQuestion`), keyed `question:<callId>`, its own
  item via the activity-precedes-narration insert. Carries `timeoutSeconds`/`notesPlaceholder`
  verbatim off the payload. **Only `outcome == "answered"` is a true settle, authoritative from the
  `user_question_answered` EVENT** — `foldUserQuestionAnswered` flips `resolution` to
  `Answered(answers, answeredVia, notes, delivery)`. Every other outcome
  (`timed_out`/`declined`/`interrupted`/`cancelled`, or any unknown future value) becomes
  `RunMovedOn(outcome)`: the RUN stopped waiting, which is not the same as the question being
  resolved, so the card STAYS interactive with honest copy that a submission now lands as a new
  message. The card settles even when ANOTHER surface answered; the local POST result only steers
  submit-state/toast. Orphan answered events are ignored, and a late `answered` for the same
  `call_id` upserts straight to `Answered`.
- **`ErrorCard` is gated on `completion.error` alone** — a successful run's residual `last_error`
  spawns NO card. It appends at the very end, never through the insert-before path.

## THE POISON ANCHOR — adopt the server `ts`, keep the client `key`

A `UserBubble.ts` is the `from_ts` that retry (`/recover`, matched by **exact string equality**) and
fork (`fork_session_at`, **lexicographic** compare) anchor on, consumed RAW by the backend. But
`ChatViewModel.send` stamps its optimistic echo with `Instant.now()` — a DEVICE clock in an ISO shape
the backend never emits (`…02.5Z`) versus Python `isoformat()`'s `…02.547517+00:00`.

**An unreconciled bubble anchors NOTHING:** retry 400s, and a branch silently cuts at a timestamp that
exists nowhere, dropping the very message you branched from. So `foldUserText`'s dedupe branch adopts
the server's `ts` **unconditionally**, and **the `key` must NOT follow** — key is LazyColumn identity
and must stay stable; ts is the server anchor and must be authoritative. Gating adoption on `pending`
does not save you: an ordinary fresh send is not pending, carries the same wrong ts, and skips
reconciliation. `user_steer` can anchor a FORK but never a RETRY (`/recover` matches `type == "user"`
only; fork reads `ts` alone), so `steer` is adopted down the same dedupe branch.

## The rest of the model

- **`SessionEvent`** — a sealed union with a polymorphic serializer and an `Unknown` fallback keyed on
  `type`, so an unknown future type never crashes parsing. **There is NO `error()` event type** on
  session streams. `user_steer` (not `user`) is recorded for a message enqueued into an active run;
  `todos` payloads REPLACE the whole checklist per event; `agent_message_delta` is the only
  incremental-text source, `agent_message` the replace-buffer, `assistant` the turn-finalizer.
  `context` decodes to `Unknown`, but `lastContextModel`/`lastContextProject`/`lastContextMcpTools`
  read the fields back out of the most recent one.
- **The session's PROJECT is live, not a load-time fact.** In auto-select mode
  (`ComposerScope.AUTO_PROJECT_KEY`) the model picks the project with `switch_project` and can move
  again mid-run, each move persisting its own `context` event. `adoptContextProject(event, current)`
  is the per-event sibling of `lastContextProject`, folded by `ChatViewModel.publish` through EVERY
  ingested event — one seam, so the live stream and a history replay converge by construction rather
  than by two readers agreeing. Two traps it encodes:
  1. "a context event naming no project" (⇒ Temporary) and "not a context event" (⇒ leave alone) are
  DIFFERENT facts. Collapsing them into one `String?` blanks the project on the first delta of
  every turn.
  2. These readers use `as? JsonPrimitive`, **never the `jsonPrimitive` accessor**, which THROWS on a
  nested value — survivable inside a history load's try/catch, fatal to the run's collector on the
  per-event path.
- **What the app adopts is `selectedProjectKey` — the value the NEXT `/query` resends — not a
  display-only field.** The backend re-resolves scope from every request's own context, so resending
  anything other than where the session actually IS migrates it. After a switch that means the
  switched-TO project, never the `auto` sentinel the turn opened with; resending the sentinel sends
  the session back to a scratch cwd mid-conversation. A user's `activeToolIds` narrowing is
  deliberately NOT reset by a model switch — only by their own `selectProject`, which re-fetches the
  catalog in the same breath.
- **`PromotedTools`** — the ONE allowlist. It is a data-side decision about which transcript items
  exist, never presentation; [`ui/chat/toolcards`](../../ui/chat/toolcards/CLAUDE.md) owns rendering
  plus a generic fallback, so the two can drift freely.
- **`ComposerScope.toolsFacetSummary`** groups active tools by `ToolSummary.scope` (order
  project→system→plugin→builtin→other), falling back to the plain "N tools" total when no active tool
  carries a recognized scope — never a lone, meaningless "N other". Counts sum to `activeToolCount` by
  construction (unit-tested).
- **`Timestamps.parseInstantOrNull` is the ONE ISO parser** (`OffsetDateTime.parse` first,
  `Instant.parse` fallback). Android's bundled `java.time` throws on a numeric offset (`+00:00`) that
  desktop JVMs accept, so a local unit test cannot catch a second copy going wrong. Every ts call site
  routes through it — never a local `Instant.parse`, even in tests.
- `ModelCatalog` (normalize the provider-prefixed `default` before comparing bare list entries),
  `ToolSummary`, `StagedAttachment`, `Session`/`SessionSummary` (`running: Boolean` is the liveness
  signal — `status` never literally reads `"running"`; `terminated`/`terminated_at` are durable),
  `ProjectSummary` (`contextKey` = bare name or `managed:<id>`), `ChatItem`.

## Device tools are stamped into the tool list, not fetched

`GET api/tools` enumerates the MCP/core registry; a `device_*` tool is DECLARED by the client on
each query and appears in no catalog response. So it appeared in no group of the picker — the one
family that acts on the user's own phone, including a shell at shell UID, was the only one missing
from the surface built for controlling tool exposure.

`SessionScopeRepository.tools()` appends them with `scope = FACET_DEVICE`, which is a real member of
`FACET_ORDER` (second, ahead of `system`) so the existing grouping, ordering and facet-count code
carries them with no special case. It appends the ADVERTISED list, so a tool switched off in
Settings or gated out by a missing permission is absent here too — the picker describes the session,
not the build.

**Toggling one there persists to the SAME store Settings writes.** The picker's allowlist reaches
the agent as `context.mcp_tools`, and device tools are appended AFTER that gate, so a picker-only
toggle would move the switch and leave the tool bound — a control that visibly does nothing.
