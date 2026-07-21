> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura Turn-Completion Notifications — notify/

Scope: `notify/` — a `dataSync` foreground service that follows a backgrounded run to its terminal
event and posts a completion notification. Four atomic classes, one Hilt binding
([`di/NotifyModule`](../di/CLAUDE.md)). Landed 2026-07-14. **The backend owns no push
infrastructure; this is a CLIENT-side watch, never FCM** — the completion event already streams to
any follower over the one SSE connection, so FCM would only add a server round-trip + token plumbing
for something `RunRepository.live()` already delivers.

## The seam — declared DOWN in data/, implemented here

`RunNotifications` is a `fun interface` declared in
[`data/repo/RunRepository.kt`](../data/repo/CLAUDE.md), NOT here — dependency flows strictly down, so
the repo never imports `notify/`/`MainActivity`. `RunNotificationLauncher` implements it and is bound
in `di/NotifyModule`; this is the SAME seam-and-binding shape `DeviceModule` uses for
`DeviceToolDispatch`←`DeviceToolExecutor`. `onRunStarted` fires on the THREE run-START paths only
(`send` 200 / `sendQuery` 202 / `retryFrom`), never `Enqueued`/slash-handled/`user_steer` — a `202`
steer into an already-watched run re-arms nothing.

## Foreground-service laws (each shipped-or-avoided a real OS failure)

- **Start at run-start (foreground), NEVER lazily on backgrounding.** Android 12+ forbids starting an
  FGS from the background. `RunNotificationLauncher.onRunStarted` starts the service while the user is
  still on screen; it then legally survives the subsequent backgrounding. The start is wrapped in
  `catch (IllegalStateException)` (`ForegroundServiceStartNotAllowedException` is a subclass) and
  degrades SILENTLY — a rare overlay-teardown race can still refuse it, and a missed background alert
  is not worth a crash (the run completes server-side; the user sees it on reopen).
- **`foregroundServiceType="dataSync"`, NOT `shortService`.** `shortService` caps ~3min and would
  kill a long agentic turn — the whole case. The manifest type must match the
  `ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC` passed to `startForeground`. Manifest permissions:
  `POST_NOTIFICATIONS` (runtime, API33+), `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_DATA_SYNC`
  (install-time).
- **`RunRepository` is `@Singleton` because of this feature.** The watcher shares the ONE multicast
  SSE connection chat/overlay already ride; a non-shared repo instance would open a redundant second
  stream per backgrounded run. The controller is a purely PASSIVE collector — it never advertises or
  answers device tools (that lives in `live()`'s pipeline upstream of the multicast, and the singleton
  executor's ledger dedupes regardless). See [`data/repo/CLAUDE.md`](../data/repo/CLAUDE.md).

## Suppression + the announce decision (pure, unit-tested)

`RunNotificationController.completionNotice(terminal, appVisible)` is the whole decision:

- **`appVisible` ⇒ always `null`.** Suppression REUSES the existing `AppForegroundChecker`
  (foreground-importance OR a showing overlay) — do NOT add a fresh importance read. The notifier's
  own FGS pins process importance at `IMPORTANCE_FOREGROUND_SERVICE` (125), which correctly reads
  "not foreground" (`125 <= 100` is false), so a watch never suppresses its OWN notification; a
  visible activity is 100 → suppress; a showing overlay renders the result live → suppress. Same
  100-vs-125 fact the `canStartActivityNow` chain turns on ([`data/device/CLAUDE.md`](../data/device/CLAUDE.md)).
- **Failure keys on `completion.error` ALONE, never `last_error`** — mirrors DESIGN.md §6 error-card
  law: recovered tool-error residue under a successful run announces "finished," not "failed." Only
  `SessionEvent.Completion` announces; `StreamEnd`/`StreamError` stop the watch (release the FGS) but
  carry no honest run outcome, so they announce nothing. A `withTimeoutOrNull(20min)` reaps a wedged
  stream so it can't pin a zombie service.

`RunNotifier` is the ONE place a `Notification`/channel is built. Two channels (`IMPORTANCE_LOW`
ongoing "working…" + `IMPORTANCE_HIGH` completion); `createNotificationChannel` is idempotent so
`ensureChannels()` on every start is free. Completion posts with `notify(tag = sessionId, id)` so
concurrent sessions never overwrite; the ongoing id is fixed (one entry regardless of watch count).
Tap → `MainActivity.EXTRA_HANDOFF_SESSION_ID` — reuses the assist-overlay handoff route, ZERO new nav
code.

## `RunNotificationService` self-reaping

`watches: ConcurrentHashMap<sessionId, Job>`; one watch per session, a duplicate start is a no-op.
The job is started **lazily and only after** it is in the map, so a fast-completing run's `finally`
(which removes it) can't fire before the entry exists and strand a reap. `stopIfIdle()` (`@Synchronized`)
stops the service the moment `watches` empties. Build fact: inherited Java `static` constants on
`Service` (`START_NOT_STICKY`, `STOP_FOREGROUND_REMOVE`) need `Service.`-qualification in Kotlin —
unqualified is not in subclass scope.

## `POST_NOTIFICATIONS` — OS grant is the sole gate

Requested at first query-send via `RunNotificationLauncher.runStarted` (`replay = 1`, so an
overlay-kicked run that started before `MainActivity` began observing still triggers the request when
the app opens). `MainActivity` observes it and calls the `registerForActivityResult` launcher —
declared as a FIELD (the framework requires registration before `STARTED`). **No in-app pre-consent
dialog** (hard directive — the OS runtime grant is the only gate; never add a second consent surface).

## Ask-user question alerts (share the ONE watch)

The `user_question` event (agent blocked on `ask_user_question`) rides the SAME per-session watch —
the collector changed from `firstOrNull{terminal}` to `transformWhile`+collect so it can observe
mid-run events and still stop at terminal. `questionNotice(event, appVisible)` /
`questionNotificationBody(payload)` are the pure, unit-tested decisions (suppress-when-foreground —
the card is on screen; body = first question text `+N more`). Posts on its own
`mewbo_run_question` channel (`IMPORTANCE_HIGH`), fixed id 3 + per-session tag (never collides with
completion id 2), autoCancel, tap → the same session-handoff intent. Cleared on the matching
`user_question_answered`, on tap, and unconditionally at run-end. Known bound: past the 20-min
watch reap the notification persists until tapped (the in-app card still settles via live stream).

Not device-verified as of landing — FGS-from-background rules, tap-nav, and the permission prompt are
OS-behavior-dependent (redroid is a weak witness for FGS restrictions; the physical Pixel is the gate).
The question alert shares that caveat (posted/cleared paths are unit-tested, not OS-verified).
