> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Repositories — data/repo/

Scope: `data/repo/` — the repositories that compose `sse` + `api` + `model` into `Flow`/`suspend`
surfaces the UI consumes. No Compose/Android-UI imports. Retrofit for REST, `SessionStreamClient` for
streams; the UI sees only domain objects and `Flow<SessionEvent>`.

## `RunRepository` — the one run-following seam

- **`@Singleton` (since the turn-completion notification feature, 2026-07-14).** `live()` hands out a
  per-session multicast (`liveStreams`, self-evicting session-keyed cache) so chat, the overlay, AND
  the notification watcher share ONE SSE connection — that only holds if they share the repo instance.
  It was safe to be unscoped before ONLY because the `@Singleton DeviceToolExecutor`'s ledger deduped
  device answers across per-injector repos; a third follower that wants to SHARE the connection forces
  a true singleton. Its only state is that cache (no per-injector state → safe).
- **`live()` builds `DeviceToolDispatch` INTO the pipeline** (`onEach`, UPSTREAM of `shareIn`) — the law
  that advertise-the-tools and answer-them live at the SAME seam, so every follower services
  `device_tool_call`s by construction. Wiring answers into a ViewModel instead cost a 66.8s overlay
  turn (two 30s timeouts) once. Dispatch is a pipeline step, NOT a second subscriber: a subscriber
  would have to terminate on the `stream_end` VALUE, which `SessionStreamClient` may DROP under buffer
  pressure (`trySend` + `terminated = true` regardless of landing — [`data/sse/CLAUDE.md`](../sse/CLAUDE.md)),
  so the collector would never end and `WhileSubscribed` could never stop a dead session's reconnect.
  Correctness comes from backlog-replay + `DeviceToolCallLedger.recordIfNew`, never a frame we hope
  arrives. `LIVE_STREAM_STOP_TIMEOUT_MS = 5_000` is not a tuning knob — it closes the `1→0→1`
  subscriber-transit window `subscribeLive`'s cancel-then-collect opens (a zero timeout tears down +
  re-opens the real SSE mid-run). Full story: [`data/device/CLAUDE.md`](../device/CLAUDE.md).
- **`errorFor` is the ONE send seam** (pulled top-level for JVM-testability like `buildMulticastLiveFlow`).
  It reads the `410` body `{"error":{"code":"session_terminated",…,"retryable":false}}` and raises a typed
  `SessionTerminatedException` instead of a bare `HttpException`, so envelope parsing lives HERE, never
  per-screen; a body that isn't the envelope falls through to `HttpException` unchanged. `send`/`sendQuery`/
  `retryFrom` all route through it (409 running / 410 terminated), because each starts a run.
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
  2+ to the config-default model + a temp-dir cwd). `mcp_tools` omitted ⇒ all tools; non-empty ⇒ allowlist.
- `ModelRepository` (lazy catalog), `SessionScopeRepository`, `AttachmentRepository` (the two-step
  multipart flow, [`data/api/CLAUDE.md`](../api/CLAUDE.md)).

**Session learnings about repository behavior accrue here.**
