> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Repositories — data/repo/

Scope: `data/repo/` — the repositories that compose `sse` + `api` + `model` into `Flow`/`suspend`
surfaces the UI consumes. No Compose/Android-UI imports. Retrofit for REST, `SessionStreamClient` for
streams; the UI sees only domain objects and `Flow<SessionEvent>`.

## `RunRepository` — the one run-following seam

- **`@Singleton`.** `live()` hands out a per-session multicast (`liveStreams`, a self-evicting
  session-keyed cache) so chat, the overlay, AND the notification watcher share ONE SSE connection —
  which only holds if they share the repo instance. An unscoped repo was survivable only while the
  `@Singleton DeviceToolExecutor`'s ledger deduped device answers across per-injector instances; a
  follower that wants to SHARE the connection forces a true singleton. Its only state is that cache
  (no per-injector state → safe).
- **`live()` builds `DeviceToolDispatch` INTO the pipeline** (`onEach`, UPSTREAM of `shareIn`) — the
  advertise-and-answer-at-the-same-seam law ([`data/CLAUDE.md`](../CLAUDE.md)), so every follower
  services `device_tool_call`s by construction. **Dispatch is a pipeline step, NOT a second
  subscriber:** a subscriber would have to terminate on the `stream_end` VALUE, which
  `SessionStreamClient` may DROP under buffer pressure ([`data/sse/CLAUDE.md`](../sse/CLAUDE.md)), so
  the collector would never end and `WhileSubscribed` could never stop a dead session's reconnect.
  Correctness comes from backlog-replay + `DeviceToolCallLedger.recordIfNew`, never a frame we hope
  arrives. `LIVE_STREAM_STOP_TIMEOUT_MS = 5_000` is not a tuning knob — it closes the `1→0→1`
  subscriber-transit window `subscribeLive`'s cancel-then-collect opens (a zero timeout tears down +
  re-opens the real SSE mid-run).
- **`errorFor` is the ONE send seam** (pulled top-level for JVM-testability like `buildMulticastLiveFlow`).
  It reads the `410` body `{"error":{"code":"session_terminated",…,"retryable":false}}` and raises a typed
  `SessionTerminatedException` instead of a bare `HttpException`, so envelope parsing lives HERE, never
  per-screen; a body that isn't the envelope falls through to `HttpException` unchanged. `send`/`sendQuery`/
  `retryFrom` all route through it, because each starts a run.
- **`RunPhase` is not a liveness fact, so `sendQuery` treats a `409` as a ROUTE CORRECTION, not an
  error.** `ChatViewModel` picks steer-vs-fresh from `RunPhase` before the call — but that phase says
  what the client is WATCHING, and three paths deliberately stop it watching a run that is still
  going: `stop()` (a client-side detach by contract), a `stream_error` ("you are no longer seeing
  this run", not "the run failed"), and a failed live collector. Each leaves the phase outside
  `Sending`/`Streaming` while the server still reports `running`, so the follow-up takes `/query`
  into a busy session. Measured on the deployed API — **`/query` while running ⇒ `409`
  `{"message": "Session is already running."}`; `/message` while running ⇒ `202`; `/message` while
  idle ⇒ `200` + a fresh `run_id`.** `/message` is therefore correct in BOTH states and only the
  `/query` leg can be wrong about liveness, so `sendQuery` re-routes its `409` onto `send` and
  returns `Enqueued`. Before that, the `409` fell through to a bare `HttpException` and the user's
  follow-up rendered as an `HTTP 409` ErrorCard instead of a queued message.
  - **Re-route, don't pre-check.** A liveness read is stale the moment it returns; the `409` IS the
    server's answer about the very request being made, and it keeps the happy path at one round
    trip. The web console routes on a polled `activeSession.running` and carries the same race with
    no recovery — this is console parity in OUTCOME, one step better in mechanism.
  - **Only `409` re-routes.** A `410` must keep reaching `errorFor`, or a terminated session would
    steer forever instead of flipping the composer's terminal state.
  - Attachments cannot follow onto `/message` (no such field), so a re-routed turn is text-only —
    the identical limitation the console's running branch has, which skips the upload entirely.
- **`RunNotifications` `fun interface` is declared HERE** (dependency flows down — the repo never
  imports `notify/`). `onRunStarted` fires on the three run-START paths only: `send` (200), `sendQuery`
  (202), `retryFrom` (`preview = null`, no fresh query text). Impl `RunNotificationLauncher` in
  [`notify/`](../../notify/CLAUDE.md), bound in `di/NotifyModule`.

## `SessionRepository` — mutations degrade to null (not typed exceptions)

- **`@Singleton`.** Its last-fetched-list + per-session-transcript caches must be SHARED
  across injectors, or "render immediately on navigation" is a lie: the drawer's `SessionsViewModel`
  ([`ui/sessions/CLAUDE.md`](../../ui/sessions/CLAUDE.md)) is recreated per chat back-stack entry, so a
  per-injector repo handed each fresh VM an EMPTY cache and blanked the rail to the skeleton for a full
  `GET /sessions` round-trip. One instance (same reasoning as `RunRepository`'s own `@Singleton` above)
  means the last-loaded list survives that recreation AND a mutation (rename/archive) through one
  injector is visible to all. Its only state is those two in-memory caches — no per-injector state, so
  the singleton is safe.

`createSession`/`renameSession`/`archiveSession`/`forkSession` follow the degrade-to-`null` idiom, NOT
`RunRepository`'s typed-exception one. Even though `AuraApi.forkSession` is `Response`-wrapped (so a
409/410 doesn't throw via Retrofit's implicit-throw-on-plain-DTO path the way `renameSession` relies
on), `forkSession` checks `isSuccessful` explicitly and treats 409/410/transport failure identically
(null). On success it fires a best-effort `refreshSessions()` in its OWN `runCatching` so that refresh
failing can never discard the already-forked session id.

**retry vs fork (the destructive/non-destructive split):** `POST /recover {action:"retry"}` is a
destructive REWIND of the SAME session (truncate the transcript at `from_ts`, then re-run);
`POST /fork` creates a NEW session shell, source untouched (null `from_ts` copies the whole transcript).
`retryFrom` starts a run → routes through `errorFor`; `forkSession` does not.

## The rest

- **`ConnectionProbe`** pins the call + `ResponseBody.string()` to `Dispatchers.IO` — an `enqueue`-`await()`
  resumes on the caller's dispatcher, and a blocking socket read from a Main-dispatched ViewModel is a
  fatal `NetworkOnMainThreadException` on the not-yet-buffered 401 body.
- **`SessionContext`** is the SINGLE scope-assembly point — the backend re-resolves model/project/tools
  from EACH `/query`'s own context, so context is RESENT every turn (omitting it silently reverts turn
  2+ to the config-default model + a temp-dir cwd).
  - **Both tool lists are THREE-STATE, and only `null` may omit the key.** `mcp_tools` omitted ⇒ all
    tools, `[]` ⇒ a real ceiling of zero, non-empty ⇒ that allowlist. `device_tools` is the same
    shape with a DIFFERENT absent arm: omitted ⇒ silence, and the server falls back to the newest
    context event that carries the key, so an omitted empty list leaves a revoked device tool bound.
    Testing either for truthiness (`isNullOrEmpty`) is a fail-open the server cannot detect —
    absence and never-declared are the same bytes — and it shipped: 525 persisted `aura-android`
    context events carried `mcp_tools: []` exactly zero times. The write seam
    (`buildSessionContext`), the derivation (`ComposerScope.toolsNarrowed`/`mcpToolsForContext`) and
    the hydration read (`SessionEvent.lastContextMcpTools`) must agree on all three states or the
    session re-widens a turn later; each looks correct in isolation, so the ROUND TRIP is the test.
  - **A MOBILE-origin session is `purpose_bound` server-side, so `allowed_tools` is refused on every
    `/query` after creation** (`SessionSpec.OVERRIDABLE_WHEN_UNBOUND`, refusal only logged). The
    ceiling this client sends therefore binds at session CREATION and nowhere else — a mid-session
    change to the picker reaches the wire correctly and is dropped on arrival.
- `ModelRepository` (lazy catalog), `SessionScopeRepository`, `AttachmentRepository` (the two-step
  multipart flow, [`data/api/CLAUDE.md`](../api/CLAUDE.md)).

**Session learnings about repository behavior accrue here.**
