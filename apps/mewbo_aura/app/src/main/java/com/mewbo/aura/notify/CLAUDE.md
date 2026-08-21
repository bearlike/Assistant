> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura Turn-Completion Notifications — notify/

Scope: `notify/` — a `dataSync` foreground service that follows a backgrounded run to its terminal
event and posts a completion notification. Four atomic classes, one Hilt binding
([`di/NotifyModule`](../di/CLAUDE.md)). **The backend owns no push infrastructure; this is a
CLIENT-side watch, never FCM** — the completion event already streams to any follower over the one SSE
connection, so FCM would only add a server round-trip + token plumbing for what
`RunRepository.live()` already delivers.

## The seam — declared DOWN in data/, implemented here

`RunNotifications` is a `fun interface` declared in
[`data/repo/RunRepository.kt`](../data/repo/CLAUDE.md), NOT here — dependency flows strictly down, so
the repo never imports `notify/`/`MainActivity`. `RunNotificationLauncher` implements it and is bound
in `di/NotifyModule`; the SAME seam-and-binding shape `DeviceModule` uses for
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
- **`specialUse` above API 34, `dataSync` below it — and the runtime value must be a SUBSET of the
  manifest's** or `startForeground` throws. `shortService` was never viable (~3min cap). **`dataSync`
  stopped being viable at targetSdk 35+:** it carries a 6h/24h budget SHARED across every service of
  that type in the app, and once exhausted the next start throws — which `RunNotificationLauncher`
  swallows, so the channel would vanish with no signal on either side. `specialUse` is the documented
  home for a case that fits no other type and cannot be expressed as a job, and carries no timeout;
  it needs the `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` justification in the manifest (reviewed by Play,
  which does not gate the sideloaded enterprise flavor). API 33 has no `specialUse` and no budget
  rule, so it keeps `dataSync` and loses nothing. Permissions: `POST_NOTIFICATIONS` (runtime,
  API33+), `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_DATA_SYNC` + `FOREGROUND_SERVICE_SPECIAL_USE`.
- **A device-control hold gets its OWN reap, and it is not 20 minutes.** 20 min is a generous bound
  on "how long until a run emits its terminal event" and a short one on "how long a person spends
  ordering dinner" — reaping there would take the command channel down mid-session and reproduce the
  exact failure the hold exists to prevent. 2h, still bounded so an abandoned session cannot pin a
  service, and the user can end it from the notification at any time.
- **`RunRepository` is `@Singleton` because of this feature** — the watcher shares the ONE multicast
  SSE connection chat/overlay already ride; a non-shared instance would open a redundant second stream
  per backgrounded run. The controller is a purely PASSIVE collector: it never advertises or answers
  device tools. See [`data/repo/CLAUDE.md`](../data/repo/CLAUDE.md).

## The device-control HOLD — why the watch outlives its run

A device-control session arms the watch with `holdForDeviceControl`, and the collector stays
subscribed past the run's terminal event. **This is not a notification concern; it is the transport.**
Device-tool dispatch is a step in `live()`'s pipeline, which stops ~5s after its last subscriber
leaves — and the UI collector leaves the moment Aura is backgrounded, which is exactly what
`device_action(action="launch")` does on its way to the app it was told to open. So the tool that
navigates destroys the transport for the tools after it: measured in the replayed incident as a 30s
`device_timeout`, then instant `device_unavailable` for every later call including a trivial clock
read.

The hold is a plain `collect {}` on the same shared flow, because the subscriber COUNT is the whole
mechanism. It is armed per session (`RunNotifications.onRunStarted(deviceControl=)`, asked of
`DeviceToolCatalog.advertisesDeviceControl()`), never for every run — a hold costs a live SSE
connection and a persistent notification, and a session with no device tools must pay neither.
Released by the notification's **Stop** action, and — without waiting for the service — by the hold's
own idle bound below.

**Two bounds now, doing different jobs — do not collapse them.**
`RunNotificationController`'s `holdIdleBoundMs` (15 min, `SystemClock.elapsedRealtime` — monotonic,
survives deep sleep) is the WORKING release: it fires when the stream has delivered nothing for that
long, measured from the last event on either the first epoch or a rebuilt one, and it is what calls
`RunNotificationService.releaseGrantIfLastHold`. `RunNotificationService`'s
`MAX_DEVICE_HOLD_DURATION_MS` (2h) is a ceiling only, kept as a last-resort cap on an abandoned
session; it stopped being the only thing standing between a taken grant and an app-wide leak once the
idle bound existed. Release is keyed on the `holdWatches` subset of `watches` emptying, not on
`watches` itself — `armWatch` records hold membership before the watch job exists, so a fast-completing
non-hold session can come and go without touching the count that gates a held one.

**The leak this closed, and the numbers that justify the bound (do not re-derive or re-tune them):**
release used to be gated on the WHOLE `watches` map emptying, while
`RunRepository.deviceControlInPlay()` is `isActive() || canTakeControl()` — so once a grant existed,
every later run (device-control or not) armed its own 2h-capped watch, and starting any new session
pushed the grant's expiry another two hours out. `holdChannelOpen` itself had no exit condition at
all — it returned only by cancellation — and `SessionStreamClient` never gives up on a dead connection
(swallows `IOException`, reconnects with a 15s ceiling, no attempt counter), so a lost connection
emits neither `StreamEnd` nor `StreamError` and the epoch never ends: the 2h cap was genuinely the
only thing left holding the line. Measured over 24 real grant windows: **6 never saw
`device_control_stop`.** Gaps between events inside a held grant (n=1993): p50 0.0s, p90 3.8s, p99
24.3s; the eight largest were `88.7, 92.2, 157.8, 180.0, 256.0, 56627.6, 78992.3, 95239.7` seconds —
cleanly bimodal, largest live gap ~4.3 min vs. smallest leaked gap 15.7h, nothing between. The
longest legitimately-closed window was 20.1 minutes and is correctly unaffected, because the bound
measures SILENCE, not total duration. That bimodal separation is also why a server-authoritative
liveness probe (`GET /sessions/{id}/events?after=`, already bound) was considered and rejected: a
local idle bound discriminates the two cases just as well, for the cost of nothing instead of a REST
call every 15s per held grant. The SSE retry-forever behaviour above is a real finding, left alone
deliberately — fixing it reaches every consumer of `SessionStreamClient`, not just this one.

**What the two suites pin, and the line they stop at.** `DeviceControlHoldIdleBoundTest` drives the
real `watchAndNotify` on the test scheduler's virtual clock (the constructor takes `nowMs` and both
durations for exactly this) — it asserts the hold RETURNS on silence past the bound and, as an
absence assertion over eighty bounds' worth of 4-minute gaps, that it does NOT return while events
keep arriving. `RunNotificationServiceHoldReleaseTest` drives the production `onWatchEnded` against a
REAL `DeviceControlSession` behind fake Shizuku seams, so the grant genuinely changes state. Both are
plain JVM: no Robolectric, and every case leaves an entry in `watches` because `stopIfIdle()` on an
empty map calls `stopForeground`/`stopSelf`, which a lifecycle-less service cannot survive.
**Unwitnessed by either:** that a real watch job's `finally` reaches `onWatchEnded` at all, and
`stopIfIdle`'s platform calls. Both need a real service lifecycle, and nothing on device has yet run
against this bound.

**Still open, not built here:** nothing currently OWNS the grant's lifetime — it is computed from
`watches`, a per-watch timer, and `RunRepository.deviceControlInPlay()`, three places that must agree
rather than one. `RunNotificationService` itself has four independent reasons to change (completion
notifications, the FGS hold, the wake lock, grant release); the next pass on this surface should
decompose it rather than add another conditional.

**The persistent notification is the honest surface of that hold**, not a formality: it is the only
indication a user has that an agent may act on their screen while they are in another app, and the
only place they can stop it. It says "Mewbo can control this device" and carries Stop.

## Suppression + the announce decision (pure, unit-tested)

`RunNotificationController.completionNotice(terminal, appVisible)` is the whole decision:

- **`appVisible` ⇒ always `null`.** Suppression REUSES the existing `AppForegroundChecker`
  (foreground-importance OR a showing overlay) — do NOT add a fresh importance read. The notifier's own
  FGS pins process importance at `IMPORTANCE_FOREGROUND_SERVICE` (125), which correctly reads "not
  foreground" (`125 <= 100` is false), so a watch never suppresses its OWN notification; a visible
  activity is 100 → suppress; a showing overlay renders the result live → suppress. Same 100-vs-125
  fact the `canStartActivityNow` chain turns on ([`data/device/CLAUDE.md`](../data/device/CLAUDE.md)).
- **Failure keys on `completion.error` ALONE, never `last_error`** — mirrors DESIGN.md's error-card
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

## The status-bar glyph states the mode — measured facts, not the ones you would expect

The ongoing entry spans a whole run, so its status-bar glyph is the ONE piece of chrome visible
without expanding anything. Between `device_control_start` and `device_control_stop` it must say so.
Three findings, all confirmed against a running API 33 device — the first two contradict the obvious
implementation:

- **`setColor` cannot reach the status-bar glyph, so the mode is carried by SHAPE.** SystemUI draws
  every glyph in the status bar through its own single foreground tint, for contrast against any
  wallpaper. `setColor`/`setColorized` reach the SHADE (measured: the expanded entry's small-icon
  badge circle and app name do render clay `#C15F3C`) and nothing above it. A colour-only signal
  would therefore be invisible in the exact place the state has to be readable. `ongoingSmallIcon`
  swaps `ic_launcher_monochrome` → `ic_stat_device_control`, a broken ring around a solid core:
  unmistakably not the clay-flower mark at 18px. `setColor` still ships, as a SECOND carrier of the
  same fact in the surface where it does render, never as the only one.
- **The rotation is an `<animation-list>` (`AnimationDrawable`), NOT an `<animated-vector>`.** An
  `AnimationDrawable` with more than one frame starts itself from `Drawable.setVisible(true, …)`,
  which is what `ImageView` — and therefore SystemUI's `StatusBarIconView` — calls when it takes the
  drawable. That is the same mechanism the framework's own `stat_sys_download` rides.
  `AnimatedVectorDrawable.setVisible` only RESUMES an already-started animator set, and nothing in
  the notification path issues the `start()` it needs, so an AVD renders as a still frame. Measured:
  8 successive `screencap`s of the status bar show 8 distinct ring angles. **Never re-post the
  notification on a timer to fake this** — the drawable owns the animation, so nothing wakes the app
  to keep it turning.
- **Inline the frames with `aapt:attr`, don't write 12 sibling drawables.** AAPT2 accepts a nested
  `<aapt:attr name="android:drawable"><vector/></aapt:attr>` inside an `<animation-list>` `<item>`;
  only the group's `android:rotation` varies, and a dozen near-identical files invite one drifting.

## Keeping the screen on for the grant — and why the wake lock cannot leak

The display must not sleep while an agent is driving it. `RunNotificationService.screenLock` holds a
`SCREEN_BRIGHT_WAKE_LOCK` (permission `WAKE_LOCK`) for exactly the span of the grant, raised and
dropped by the SAME `DeviceControlSession.active` collector that drives the notification presence —
a grant can end with nobody calling `stop()` (the Shizuku binder dies with its host process), and
only a collector sees that.

**`FLAG_KEEP_SCREEN_ON` is not the alternative, though the docs say it is.** It is a WINDOW
attribute and a service owns no window. The only window Aura has during control is the `ui/control`
overlay, gated on `SYSTEM_ALERT_WINDOW` — an optional special permission this feature deliberately
works without, and measured NOT granted on the dev container. Hanging the screen on it would fail
silently on exactly the devices that skipped that prompt. `PowerManager` has no non-deprecated
screen-level lock, so the deprecated one is the whole available surface, not a shortcut.

**Three independent guarantees, the first two by construction:**

1. `setReferenceCounted(false)` — one `release()` is absolute regardless of how many acquires
   preceded it. A reference-counted lock is how an unbalanced pair strands a held lock.
2. A BOUNDED `acquire(MAX_DEVICE_HOLD_DURATION_MS)` — the platform drops it at the timeout even if
   every line of release code is wrong or never runs.
3. A wake lock is held by a binder token owned by the process, so process death releases it. That
   covers a kill the service never observes; no `onDestroy` is guaranteed.

On top of those: the collector releases when the grant drops, and `onDestroy` releases
unconditionally — FIRST, before `scope.cancel()` takes the collector that would otherwise do it, and
without asking whether it is held (on a non-reference-counted lock that question could only ever be
wrong in the direction that leaves it held). Measured end to end in `dumpsys power`: `ACQ
mewbo:device-control (screen-bright)` at the grant, `REL` on the notification's Stop, `Wake Locks:
size=0` after.

## Coming back to the app when the grant ends

An agent given the phone can drive it into another app, so a grant frequently ends with the user
standing in somebody else's UI, on a screen they did not ask for, with the session that moved them
nowhere in sight. `RunNotificationService.returnToApp` brings `MainActivity` forward on the session
that was driving.

**It hangs off the SAME `DeviceControlSession.active` collector as the notification presence and the
wake lock**, and for the same reason: a grant can end with nobody calling `stop()`. A true→false
transition is the whole trigger — the read happens before the write, because a `StateFlow` replays
to a new collector and a plain `!held` would fire on an initial `false` no grant ever preceded.

**Stop is the PRIMARY path, so it claims its own drop.** `ui/control`'s Stop pill routes through
`stopIntent` (one way to end a grant, by design) and that pill is on screen exactly when the user is
in somebody else's app — so "tapped Stop while outside Aura" is the likeliest way this feature is
ever exercised, not a marginal case. The `EXTRA_STOP` branch then tears the service down
(`stopSelf` → `onDestroy` → `scope.cancel()`) without waiting for the collector, so leaving the
return to the collector would lose it precisely there. The branch calls `returnToApp` itself, right
after `stop()`.

**`recordControlHeld` is what makes the pair exactly-once, and it is `@Synchronized` for a reason.**
It records the presence and returns whether THIS write was the true→false transition; whichever of
the two threads observes the drop first wins and the other computes `false`. The collector runs on
`Dispatchers.Default` and the Stop branch on the main thread, so a bare `@Volatile` read-then-write
would let both read `true` and both launch. `controlHeld` stays `@Volatile` for the readers outside
the lock, which need visibility rather than atomicity. No new state field: the write IS the dedup.

**The Stop intent's dummy session id never reaches `currentSessionId`** — `onStartCommand` assigns it
only AFTER the `EXTRA_STOP` branch returns, so the return targets the genuine last session rather
than one literally named `stop`. That ordering is load-bearing; moving the assignment above the
branch would navigate into nothing.

**Placement is the decision, not the launch.** `ui/control/` renders and decides nothing about
control, so bringing an Activity to the front does not belong in the overlay's teardown; the service
is already the one thing watching the grant end, and a **foreground service is the documented
background-activity-launch exemption** the start needs.

Three guards, each of which is the feature rather than hygiene:

- **The existing `AppForegroundChecker`, reused verbatim** — the third surface to ask it, after
  completion suppression and the narration bubbles. Yanking someone into Aura who is already reading
  it is worse than doing nothing, and this fires on every phone-driving session's end including the
  ones that never left.
- **`RunNotifier.ongoingTapTarget`, not a blank check** — so the tapless return and the ongoing
  entry's tap resolve the SAME session. What the notification says is running is what comes back.
- **`runCatching`** — a refused start is a no-op the user reads as "it didn't happen", but a throw
  would take down the collector that releases the wake lock and drops the notification. A failed
  convenience must never break the grant's teardown.

**It reuses `openSessionIntent`'s construction rather than copying it:** `RunNotifier.sessionIntent`
is now the ONE builder, feeding both the `PendingIntent` every notification taps through and
`openSession`, the tapless start. `FLAG_ACTIVITY_NEW_TASK` is load-bearing for both (a `Service` is
not an `Activity` context). **Deliberately not `openSessionIntent(id).send()`**, though that reuses
more: creator and sender would be this same process, so it grants no privilege the direct start
lacks while adding a second background-activity-start question on top of the same one.

**Best-effort at the 2h ceiling only** — there `onDestroy` calls `stop()` and then `scope.cancel()`
with nobody claiming the drop, so the return can be lost. A session abandoned for two hours is one
the user long since walked away from, which is the one case where returning them is arguably wrong
anyway. Every other path is covered: `device_control_stop`, a grant lost to a dead binder, both Stop
affordances, and — since it became the working release — **the 15-minute idle bound**, which reaches
`releaseGrantIfLastHold` while the service is still alive, so the `active` collector is there to see
the drop and claim it. The gap therefore now applies only to the ceiling, not to the ordinary way an
abandoned grant ends.

## The ongoing notification: taps go somewhere, Open is a button, and there is NO run Stop

**All three notifications deep-link, the ongoing one included** — it opens the session it is
reporting on, through the same `openSessionIntent` completion and question alerts use. It shipped
once with no `setContentIntent` at all: posted correctly, ongoing flag set, and inert to a tap.

**A tappable body advertises nothing, so the destination is also a labelled `Open` BUTTON.** It
reuses `openSessionIntent` verbatim, which means it introduces no new request code to collide with
`STOP_REQUEST_CODE` — same intent plus the same `sessionId.hashCode()` yields the same
`PendingIntent`, confirmed in `dumpsys notification`, where the action and the `contentIntent` print
the SAME `PendingIntentRecord` hash. `ongoingActions` takes the ALREADY-RESOLVED tap target rather
than a raw session id so "there is somewhere to open" is decided once and the button and the content
intent cannot disagree; an Open button that opens nothing is worse than no button. `Stop` needs no
target (it releases the app-wide grant), which is why its own intent is allowed to name none.
Declaration order is render order and OPEN goes first deliberately: the leftmost button is the one a
thumb reaches from a pocket, so the harmless action takes that slot.

One notification covers N watched sessions, so the tap needs a single well-defined target.
`RunNotifier.ongoingTapTarget` (pure, unit-tested) owns the rule: **the session whose label the
entry is currently showing** — the service holds `currentSessionId` alongside `currentLabel` and sets
them together, so what the entry SAYS and where it GOES can never disagree. A second run re-posts in
place and moves both. A blank id (before any watch; the Stop intent carries none) yields no content
intent at all — `AuraNavHost` gates its handoff effect on `null`, not on blank, so an empty extra
would navigate to `chat?sessionId=` rather than doing nothing.

**Why there is no Stop action for an ordinary run — do not add one, and the reason is not the one
you would guess.** It is not that `AuraApi` lacks the binding (it does lack it — the interface
carries no `/interrupt` and no `/terminate`). It is that **no server operation means "stop this run
and keep the session"**, so every candidate button is dishonest in a different way:

| Candidate | What it actually does | Why it must not be the button |
|---|---|---|
| `ChatViewModel.stop()` | cancels the stream job, barges in on speech, flips `runPhase` to `Idle` | CLIENT-SIDE DETACH — the run keeps going server-side |
| `POST /sessions/{id}/interrupt` | interrupts the current STEP and appends a `user_steer` "[Interrupted by user]" event | **`interrupt_step`'s own contract is "the loop continues after the interrupted step with error results"** — it is a STEER, not a stop. The agent keeps working, so the button lies exactly as the detach does |
| `POST /sessions/{id}/terminate` | genuinely cancels the active run | one-way door: irreversible, appends `session_terminated`, and every later mutating call (query, message, interrupt, recover, fork) returns `410`. A destructive, unconfirmable action behind a single mis-tappable shade button |

A Stop that leaves the agent working is a lie told on the one surface whose entire justification is
honest disclosure of background work — worse than no button, because the user stops watching.

**The console already answered this, and its answer is why the shade cannot copy it.** The console's
Stop calls `/terminate` (`useSessionQuery.ts`'s `stopM`), NOT `/interrupt` — which it wires as a
separate mutation. So "Stop means terminate" is the product's settled semantic, not an open
question. But the console spends a **danger-toned confirmation popover** on it first
(`InputComposerBody`: click the Stop pill → "Stop all (N agents)" / Cancel; Esc in the textarea opens
the same confirm). **A notification action cannot host a confirmation** — it is one tap, mis-tappable
from a pocket, and the thing behind it is irreversible. That asymmetry, not the absence of an
endpoint, is why the shade gets no Stop.

The honest design is the one now shipped: **the tap opens the session, and Stop lives in-app where
its confirmation can.** Revisit only if a run-scoped cancel that leaves the session usable appears —
a client binding alone cannot fix this. The device-control **Stop** is a different thing and stays:
`deviceControlSession.stop()` genuinely releases the grant, which is precisely why that one earns a
button — and it is reversible, the user simply grants again.

## `RunNotificationService` self-reaping

`watches: ConcurrentHashMap<sessionId, Job>`; one watch per session, a duplicate start is a no-op. The
job is started **lazily and only after** it is in the map, so a fast-completing run's `finally` (which
removes it) can't fire before the entry exists and strand a reap. `stopIfIdle()` (`@Synchronized`)
stops the service the moment `watches` empties.

Build fact: inherited Java `static` constants on `Service` (`START_NOT_STICKY`,
`STOP_FOREGROUND_REMOVE`) need `Service.`-qualification in Kotlin — unqualified is not in subclass
scope.

## `POST_NOTIFICATIONS` — OS grant is the sole gate

Requested at first query-send via `RunNotificationLauncher.runStarted` (`replay = 1`, so an
overlay-kicked run that started before `MainActivity` began observing still triggers the request when
the app opens). `MainActivity` observes it and calls the `registerForActivityResult` launcher —
declared as a FIELD (the framework requires registration before `STARTED`). **No in-app pre-consent
dialog** — the OS runtime grant is the only gate; never add a second consent surface.

## Ask-user question alerts (share the ONE watch)

The `user_question` event (agent blocked on `ask_user_question`) rides the SAME per-session watch — the
collector is `transformWhile`+collect rather than `firstOrNull{terminal}` so it can observe mid-run
events and still stop at terminal. `questionNotice(event, appVisible)` / `questionNotificationBody`
are the pure, unit-tested decisions (suppress when foreground — the card is on screen; body = first
question text `+N more`). Posts on its own `mewbo_run_question` channel (`IMPORTANCE_HIGH`), fixed id 3
+ per-session tag (never collides with completion id 2), autoCancel, tap → the same session-handoff
intent. Cleared on the matching `user_question_answered`, on tap, and unconditionally at run-end.

**Known bound:** past the 20-min watch reap the notification persists until tapped (the in-app card
still settles via live stream).

**What IS device-verified (API 33 container), and what is not.** Verified against a live grant:
the glyph swap, the rotation, the shade tint, both action buttons, the Open action sharing the
content intent's `PendingIntentRecord`, and the wake lock's acquire/release/absent-after.

Still NOT device-verified, and redroid is a weak witness for all of it — the physical Pixel is the
gate: FGS-from-background restrictions, the `POST_NOTIFICATIONS` prompt, completion/question alert
posting and clearing, and whether SystemUI throttles a perpetually-animating status-bar icon under
real battery-saver conditions (the framework animates its own `stat_sys_download` the same way, so
this is an expectation, not a measurement).

**`returnToApp` is reasoned, not measured, and it cannot be measured from inside the app.** That a
foreground service exempts the start from the background-activity-launch restriction is documented
behaviour, not something observed here; and a BLOCKED start is silent — the platform maps the
refusal to success before a caller sees it, the same fact `data/device/CLAUDE.md` records for the
clock handoff. So nothing short of watching a physical device come back to Aura confirms this. If it
turns out to be dropped, record that; do not weaken a guard to work around it.
