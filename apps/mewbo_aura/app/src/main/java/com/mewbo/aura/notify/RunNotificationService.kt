package com.mewbo.aura.notify

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.device.DeviceControlSession
import dagger.hilt.android.AndroidEntryPoint
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Foreground service that keeps the process alive while a backgrounded run finishes, so its
 * completion can be observed and announced. Deliberately thin: all watch/notify LOGIC is
 * [RunNotificationController]'s; this class owns only the Android lifecycle — `startForeground`, the
 * per-session watch jobs, and self-reaping when the last one ends.
 *
 * Foreground-service type is decided by [foregroundType], which owns that reasoning — `specialUse`
 * above API 34, `dataSync` below it. `shortService` was never viable (~3min cap, against a case built
 * for long agentic turns).
 *
 * **Must be started while the app is foreground.** [RunNotificationLauncher] fires this the moment a
 * run STARTS (from `RunRepository`), i.e. while the user is still on screen, because Android 12+
 * forbids starting a foreground service from the background — start it at send time and it legally
 * survives the app being backgrounded, which is the point.
 */
@AndroidEntryPoint
class RunNotificationService : Service() {

    @Inject lateinit var controller: RunNotificationController
    @Inject lateinit var notifier: RunNotifier
    @Inject lateinit var deviceControlSession: DeviceControlSession

    /** "Is the user somewhere this session is not already on screen" — the ONE predicate
     * [returnToApp] and the completion-notification suppression both ask. */
    @Inject lateinit var appForeground: AppForegroundChecker

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    /** sessionId → its watch job. One watch per session; a duplicate start for a session already
     * being watched is ignored. Guards self-reaping — the service stops only when this empties. */
    internal val watches = ConcurrentHashMap<String, Job>()

    /**
     * The sessions whose watch is holding the device-control channel open — a SUBSET of [watches],
     * and the set whose emptying releases the grant.
     *
     * **Gating the release on [watches] emptying instead was a real, measured leak, and the two sets
     * are not interchangeable.** The grant is app-wide (one screen, one shell-UID service), while
     * [watches] is per session; and once a grant exists `RunRepository.deviceControlInPlay()` is
     * true for EVERY later run, so every new session added a fresh watch with its own 2h cap. The
     * release therefore waited for the LAST of an unbounded, user-renewable set of timers: asking
     * about the weather in a new session pushed the grant's expiry out by another two hours, and an
     * ordinarily active user could keep an abandoned grant alive indefinitely. Ending the grant when
     * the last HOLD ends is still exactly one release path with one owner — it corrects WHICH set
     * that owner's end is computed from. Completion-only watches keep the service up as before.
     *
     * `internal` alongside [watches] so the claim is asserted rather than argued
     * (`RunNotificationServiceHoldReleaseTest`); nothing outside this class writes either.
     */
    internal val holdWatches = ConcurrentHashMap.newKeySet<String>()

    /**
     * Whether an agent currently holds control, mirrored off [DeviceControlSession.active].
     *
     * **This is the PRESENCE, and it is deliberately not the same fact as the hold being armed.**
     * The hold is armed from the user's opt-in, so it is armed for every run an opted-in user
     * makes — including asking about the weather. Titling the notification off the arming would tell
     * that user their phone can be driven when no grant was ever taken: the inverse of the bug this
     * feature exists to remove, and just as dishonest.
     */
    @Volatile private var controlHeld = false

    /**
     * When the CURRENT grant was first observed held, on [nowMs]'s monotonic clock — `null` while no
     * grant exists. The anchor for [releaseGrantIfExpired], and the reason that ceiling cannot be
     * renewed: it is stamped once per GRANT, never per watch.
     */
    @Volatile private var grantHeldSinceMs: Long? = null

    /** The watchdog that fires [releaseGrantIfExpired]; alive only while a grant is. */
    @Volatile private var grantCeilingJob: Job? = null

    /**
     * The clock the grant ceiling is measured on. `SystemClock.elapsedRealtime` for the same reason
     * [RunNotificationController] uses it — it is monotonic and keeps counting through the deep
     * sleep a backgrounded hold spends most of its life in, where a wall clock can step in the
     * direction that extends an abandoned grant indefinitely.
     *
     * `internal var` purely so a plain-JVM test can drive a 45-minute ceiling without waiting 45
     * minutes; nothing in production reassigns it, and the default is a method REFERENCE, so a test
     * that overrides it never touches Android at all.
     */
    internal var nowMs: () -> Long = SystemClock::elapsedRealtime

    /** The newest run label, kept so the presence can re-post the ongoing notification without
     * losing the text identifying WHICH request is running. */
    @Volatile private var currentLabel = ""

    /** The session [currentLabel] belongs to — the ongoing notification's tap target
     * ([RunNotifier.ongoingTapTarget]). Set in lockstep with the label at every start, never
     * separately: the notification names one run, and tapping it must open THAT run. */
    @Volatile private var currentSessionId = ""

    /**
     * Keeps the display on for exactly as long as an agent holds the grant, and not one moment
     * longer. Created on the first grant and reused; `null` until then, so a process that never
     * takes control never allocates one.
     *
     * **Why a deprecated `SCREEN_BRIGHT_WAKE_LOCK` and not `FLAG_KEEP_SCREEN_ON`.** The flag is a
     * WINDOW attribute and this is a service — it owns no window. The only window Aura could set it
     * on during control is the `ui/control` overlay, which is gated on `SYSTEM_ALERT_WINDOW`, an
     * optional special permission the feature deliberately works without. Hanging the screen on an
     * optional grant would make it fail silently on exactly the devices that skipped that prompt.
     * `PowerManager` has no non-deprecated screen-level lock, so the deprecated one is the whole
     * available surface, not a shortcut past a better API.
     *
     * **It cannot leak, by three independent mechanisms — the first two by construction:**
     * 1. `setReferenceCounted(false)`, so one `release()` is absolute no matter how many acquires
     *    preceded it. A reference-counted lock is how an unbalanced pair strands a held lock.
     * 2. A BOUNDED `acquire(timeout)` matching [MAX_DEVICE_HOLD_DURATION_MS]. The platform drops it
     *    at the timeout even if every line of release code below is wrong or never runs.
     * 3. A wake lock is held by a binder token owned by this process, so process death releases it.
     *    That is what covers a kill the service never observes — no `onDestroy` is guaranteed.
     *
     * The code paths on top of those: the [DeviceControlSession.active] collector releases the
     * moment the grant drops (including a grant LOST to a dead binder, which no call site sees),
     * and [onDestroy] releases unconditionally, without asking whether it is held.
     */
    private var screenLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // **A grant can end with nobody calling `stop()`** — the Shizuku binder dies with its host
        // process, and the grant demotes itself. Only a collector sees that, which is why the
        // presence is driven by the flow rather than set at the call sites that take and release it.
        // A notification reading "Mewbo can control this device" outliving the binder behind it is
        // the toggle-that-lies failure this whole seam exists to remove.
        scope.launch {
            deviceControlSession.active.collect { held ->
                val dropped = recordControlHeld(held)
                // Anchored and armed from the SAME collector as everything else that follows the
                // grant, and for the same reason: a grant can start or end with nobody calling
                // start()/stop(), and only a collector sees that.
                recordGrantPresence(held)
                armGrantCeiling(held)
                // The screen follows the GRANT, on the same collector as the notification, for the
                // same reason: a grant can end with nobody calling `stop()`, and only a collector
                // sees that. Driving it from the call sites that take and release control would
                // leave the display pinned on after a binder death.
                setScreenAwake(held)
                // Only meaningful while we are foreground; a post before `startForeground` would be
                // a second, orphaned notification rather than an update to the service's own.
                if (watches.isNotEmpty()) {
                    notifier.updateOngoing(currentSessionId, currentLabel, deviceControl = held)
                }
                // LAST, after the grant's own visible teardown: the handoff is the least important
                // thing on this path and must never delay the notification or the screen lock.
                if (dropped) returnToApp()
            }
        }
    }

    /**
     * Records the grant's presence and reports whether THIS write is the true→false transition —
     * the trigger for [returnToApp], claimed exactly once.
     *
     * **Reading before writing is what guards the initial emission.** A `StateFlow` replays its
     * current value to a new collector, so a plain `!held` would fire on a `false` no grant ever
     * preceded.
     *
     * **`@Synchronized` because two threads race for the same drop, by design.** The collector runs
     * on `Dispatchers.Default`; the notification's Stop claims the drop from `onStartCommand` on the
     * main thread so it cannot be lost to the teardown that follows it. Whichever observes it first
     * wins, and the loser computes `false` and does nothing — but only if the read and the write are
     * one step. Left as a bare `@Volatile` read-then-write, both could read `true` and both would
     * launch. `controlHeld` stays `@Volatile` for the readers OUTSIDE this lock ([onStartCommand]'s
     * `buildOngoing`), which need visibility rather than atomicity.
     *
     * `internal` rather than private so the exactly-once claim above is asserted rather than
     * argued — including the race, which a comment can only promise. Nothing outside this class
     * calls it (`RunNotificationServiceControlHeldTest`).
     */
    @Synchronized
    internal fun recordControlHeld(held: Boolean): Boolean {
        val wasHeld = controlHeld
        controlHeld = held
        return wasHeld && !held
    }

    /**
     * Stamps [grantHeldSinceMs] when a grant is taken and clears it when one ends — the anchor
     * [releaseGrantIfExpired] measures from.
     *
     * **`?:` rather than a plain assignment, and that is the whole feature.** A re-assertion of
     * `true` — the Stop branch writes the presence too, and a future emission could repeat one —
     * must not push an existing grant's ceiling forward. A ceiling anything can renew is the bound
     * that was already there and already failing.
     *
     * Deliberately NOT folded into [recordControlHeld], which every teardown path calls and which
     * must stay free of the clock: reading one there would drag `SystemClock` into a plain-JVM
     * test that is only asking about the drop-claim race.
     */
    @Synchronized
    internal fun recordGrantPresence(held: Boolean) {
        grantHeldSinceMs = if (held) (grantHeldSinceMs ?: nowMs()) else null
    }

    /**
     * Starts or stops the grant-scoped ceiling watchdog. Idempotent; the previous watchdog is always
     * cancelled first, so a re-emission cannot leave two running.
     */
    @Synchronized
    private fun armGrantCeiling(held: Boolean) {
        grantCeilingJob?.cancel()
        grantCeilingJob = if (!held) null else scope.launch {
            while (true) {
                val remaining = remainingGrantMs() ?: return@launch
                if (remaining <= 0L) {
                    releaseGrantIfExpired()
                    return@launch
                }
                delay(remaining)
            }
        }
    }

    /** Milliseconds left on the current grant's ceiling, or `null` when no grant is held. */
    @Synchronized
    private fun remainingGrantMs(): Long? =
        grantHeldSinceMs?.let { MAX_GRANT_DURATION_MS - (nowMs() - it) }

    /**
     * **The one fallback that survives an actively-used app, and the only one that does.**
     *
     * Every other bound here is per WATCH — the idle bound, the per-watch cap, the last-hold-ends
     * release — and a watch is created per run. Once a grant exists `RunRepository.deviceControlInPlay()`
     * is true for EVERY later run, so each new session mints a fresh hold watch carrying fresh
     * timers, and the release waits for the whole set to empty. An ordinarily active user therefore
     * renews an abandoned grant indefinitely without doing anything unusual: the measured leak
     * windows ran 15.7 and 26.5 hours. This ceiling is anchored to the moment the GRANT was taken
     * and is renewed by nothing, so no amount of later activity can extend it.
     *
     * `internal` and clock-driven so the claim is asserted rather than argued, without waiting out
     * the ceiling in wall time.
     */
    @Synchronized
    internal fun releaseGrantIfExpired(): Boolean {
        val since = grantHeldSinceMs ?: return false
        if (nowMs() - since < MAX_GRANT_DURATION_MS) return false
        return releaseGrant()
    }

    /**
     * **The ONE place a grant is released.** Four things can decide a grant is over — the last hold
     * ending, the grant ceiling, the user's Stop, the service being destroyed — and each is a
     * TRIGGER, never its own release path. Two independent opinions about how long an agent may
     * drive the phone is the disease `data/device/CLAUDE.md` records this design as removing; four
     * scattered `deviceControlSession.stop()` calls is how that disease comes back.
     *
     * Clearing [holdWatches] here is what keeps the bookkeeping honest for the triggers that do NOT
     * arrive through a watch's end: after the grant is gone, a later watch ending must not read
     * "the last hold just ended" and report a release that already happened.
     *
     * Reports whether THIS call is what ended a grant, so a caller may key a user-visible action
     * (the return-to-app handoff) on it exactly once.
     */
    @Synchronized
    internal fun releaseGrant(): Boolean {
        holdWatches.clear()
        grantHeldSinceMs = null
        return deviceControlSession.stop()
    }

    /**
     * **Puts the user back in the app, on the session that was driving, when a grant ends while they
     * are somewhere else.** An agent that navigates into another app otherwise leaves them there,
     * holding a phone showing a screen they did not ask for, with the session that moved it nowhere
     * on screen.
     *
     * **Two guards, and both are the feature rather than hygiene:**
     * - **Not while they are already looking at Aura.** [AppForegroundChecker] is the existing
     *   predicate (process importance OR the assist overlay on screen); a second one would drift
     *   from the two other surfaces that ask this. Pulling someone into an app they are reading is
     *   strictly worse than doing nothing, and this fires on every grant a phone-driving session
     *   ends — including the ones that never left the app.
     * - **Not without somewhere to go.** [RunNotifier.ongoingTapTarget] is reused rather than a
     *   blank check, so the tapless return and the notification's tap resolve the SAME target: what
     *   the ongoing entry says is running is what comes back. A blank id would navigate to an empty
     *   session arg rather than doing nothing.
     *
     * **Silent on failure, deliberately.** A start refused by the platform is a no-op the user reads
     * as "it just didn't happen"; a throw here would take down the collector that releases the wake
     * lock and drops the notification, so a failed convenience would break the grant's teardown.
     *
     * **Called from TWO places, and the second is the primary one.** The
     * [DeviceControlSession.active] collector sees every drop including a grant LOST to a dead
     * binder, which no call site observes; the notification/pill Stop claims its own drop in
     * [onStartCommand], because Stop is the likeliest way this feature is ever exercised and the
     * teardown it triggers would otherwise race the collector away. [recordControlHeld] makes the
     * pair exactly-once. **Still best-effort at the 2h reap**, where `onDestroy` calls `stop()` and
     * then `scope.cancel()` with nobody claiming the drop — a session abandoned for two hours is one
     * the user long since walked away from, which is the one case where returning them is arguably
     * wrong anyway.
     *
     * **NOT VERIFIED — reasoned, not measured.** Android forbids starting an activity from the
     * background, and a foreground service is documented as an exemption; a held grant implies one
     * is running, since it is what keeps the command channel alive. Nothing here has run on
     * hardware. A blocked start is SILENT (the platform maps the refusal to success before a caller
     * sees it), so this cannot be confirmed from inside the app — only by watching a physical device
     * come back to Aura. If it turns out to be dropped, that is the finding; do not work around it
     * by weakening a guard above.
     */
    private fun returnToApp() {
        val sessionId = RunNotifier.ongoingTapTarget(currentSessionId) ?: return
        if (appForeground.isForeground()) return
        runCatching { notifier.openSession(sessionId) }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val sessionId = intent?.getStringExtra(EXTRA_SESSION_ID)?.takeIf { it.isNotBlank() }
        if (sessionId == null) {
            stopIfIdle()
            return Service.START_NOT_STICKY
        }
        val label = intent.getStringExtra(EXTRA_LABEL).orEmpty()
        val holdForDeviceControl = intent.getBooleanExtra(EXTRA_DEVICE_CONTROL, false)

        // The user's own off-switch. A service that keeps a channel open so an
        // agent can drive the phone must be stoppable from the notification it
        // is already required to show — the alternative is a persistent
        // notification the user cannot act on for a capability they cannot
        // revoke without force-stopping the app.
        if (intent.getBooleanExtra(EXTRA_STOP, false)) {
            // Released HERE and not left to teardown: Stop is the user saying "stop driving my
            // phone", and that must take effect at the tap, not whenever the service finishes
            // unwinding. `stop()` is idempotent, so the release in [onDestroy] repeating it is free.
            releaseGrant()
            // **The PRIMARY return path, claimed here rather than left to the collector.** Both Stop
            // affordances reach this branch — the notification's action and `ui/control`'s Stop pill,
            // which routes through [stopIntent] precisely so there is ONE way to end a grant — and
            // the pill is on screen exactly when the user is in somebody else's app. So this is the
            // likeliest way the return is ever exercised, not a marginal one. Everything below tears
            // the service down (`stopSelf` → `onDestroy` → `scope.cancel()`) without waiting for the
            // collector to observe the drop, so leaving it to the collector would lose it on the very
            // path that matters most. [recordControlHeld] is what keeps that from double-firing: the
            // collector's own emission then computes `false` and does nothing.
            if (recordControlHeld(false)) returnToApp()
            watches.values.forEach { it.cancel() }
            watches.clear()
            // Cleared HERE so the cancelled jobs' `finally` finds nothing to release: [stop] above
            // has already done it, at the tap rather than whenever the jobs finish unwinding.
            holdWatches.clear()
            stopIfIdle()
            return Service.START_NOT_STICKY
        }

        // Re-asserting foreground on every start keeps us inside the 5s startForegroundService
        // deadline and refreshes the ongoing notification; the type must match the manifest.
        notifier.ensureChannels()
        currentLabel = label
        currentSessionId = sessionId
        startForeground(
            RunNotifier.ONGOING_NOTIFICATION_ID,
            // [controlHeld], never [holdForDeviceControl] — see the field. A run STARTING is not a
            // grant being taken, and a grant already held must survive the next run's start.
            notifier.buildOngoing(sessionId, label, deviceControl = controlHeld),
            foregroundType(),
        )

        // One watch per session, but the HOLD is recorded either way — see [admitWatch]. The job is
        // started LAZILY and only after it is in the map, so a fast-completing run's `finally`
        // (which removes it) can never fire before the entry exists and strand a reap.
        if (admitWatch(sessionId, hold = holdForDeviceControl)) {
            val job = scope.launch(start = CoroutineStart.LAZY) {
                try {
                    val cap =
                        if (holdForDeviceControl) MAX_DEVICE_HOLD_DURATION_MS else MAX_WATCH_DURATION_MS
                    withTimeoutOrNull(cap) {
                        controller.watchAndNotify(sessionId, label, holdForDeviceControl)
                    }
                } finally {
                    onWatchEnded(sessionId)
                }
            }
            watches[sessionId] = job
            job.start()
        }
        // The ONE return that asks for redelivery, and only because this intent alone is enough to
        // rebuild the watch: session id, label and the device-control flag are its whole input, and
        // the stream is re-read from the server rather than resumed from client state. A sticky
        // restart therefore resumes a real watch, not a half-initialised one — the failure mode worth
        // more than the restart. Bound worth knowing: Android redelivers the LAST intent only, so a
        // kill while several sessions were watched restores one of them; the others surface when the
        // user reopens the app. START_NOT_STICKY stays on both paths above, which carry nothing to
        // redeliver (no session) or explicitly mean stop.
        return Service.START_REDELIVER_INTENT
    }

    /**
     * The runtime type, which must be a SUBSET of the manifest's or
     * `startForeground` throws.
     *
     * `specialUse` above API 34 because `dataSync` carries a 6h/24h budget at
     * targetSdk 35+, shared across every service of that type in the app —
     * exhausting it makes the NEXT start throw, and that throw is swallowed by
     * the launcher, so the channel would vanish with no signal to either side.
     * API 33 has no `specialUse`, so it keeps `dataSync`; that device also has
     * no budget rule, so nothing is lost there.
     */
    private fun foregroundType(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        } else {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        }

    /**
     * Raises or drops [screenLock]. Idempotent in both directions — the grant flow re-emits, and
     * [onDestroy] calls this after the collector may already have released.
     *
     * `SCREEN_BRIGHT_WAKE_LOCK` and not `ACQUIRE_CAUSES_WAKEUP`: the job is to stop the screen
     * sleeping under an agent that is driving it, never to wake a phone the user put down.
     */
    @Suppress("DEPRECATION") // No windowless replacement exists — see [screenLock].
    private fun setScreenAwake(awake: Boolean) {
        if (!awake) {
            screenLock?.takeIf { it.isHeld }?.release()
            return
        }
        val lock = screenLock ?: getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK, WAKE_LOCK_TAG)
            .also {
                it.setReferenceCounted(false)
                screenLock = it
            }
        if (!lock.isHeld) lock.acquire(MAX_DEVICE_HOLD_DURATION_MS)
    }

    /**
     * Records that [sessionId] has a watch, and whether that watch is holding the device-control
     * channel open. The counterpart of [releaseGrantIfLastHold].
     *
     * Called BEFORE the job exists, for the same reason the job is started lazily: a fast-completing
     * run's `finally` must never run against a set this session was not yet in, or its release is
     * skipped and the grant outlives the only thing that would have ended it.
     *
     * `internal` and named rather than an inline `add`, so the release rule can be asserted end to
     * end without a service lifecycle (`RunNotificationServiceHoldReleaseTest` arms through this
     * exact path, not a test-only one). `@Synchronized` alongside the release for the same reason it
     * is: the two mutate one set from different threads.
     */
    @Synchronized
    internal fun armWatch(sessionId: String, hold: Boolean) {
        if (hold) holdWatches.add(sessionId)
    }

    /**
     * Records this start's hold membership and reports whether a watch JOB still has to be created.
     *
     * **The two are separated because collapsing them dropped holds silently.** Arming used to live
     * INSIDE the "is this session already watched" guard, so a session's SECOND run could be the one
     * that armed a hold — Shizuku started between the two runs, making `deviceControlInPlay()` false
     * at the first and true at the second — and while the first run's watch was still draining, the
     * guard skipped the whole block. The hold was never recorded, so a grant taken by that run had
     * no entry in [holdWatches] at all, [releaseGrantIfLastHold] answered `false` for it forever,
     * and nothing short of destroying the service could end it.
     *
     * Arming is unconditional and idempotent: it is a set add, so re-arming an already-held session
     * is free and a later `hold = false` start never REMOVES membership a hold start established.
     *
     * `internal` and split out because [onStartCommand] calls `startForeground`, which no plain-JVM
     * test can survive — this is the seam where the ordering can be asserted rather than argued
     * (`RunNotificationServiceGrantCeilingTest`). `onStartCommand` is single-threaded on the main
     * thread, so the check-then-create needs no locking beyond what [armWatch] already takes.
     */
    @Synchronized
    internal fun admitWatch(sessionId: String, hold: Boolean): Boolean {
        armWatch(sessionId, hold)
        return !watches.containsKey(sessionId)
    }

    /**
     * Every watch arrives here when it ends, however it ended — its own return (the idle bound, or a
     * terminal event on a non-hold watch), the 2h cap, a cancel from the Stop branch, or the scope
     * being torn down.
     *
     * The grant is released FIRST and independently of [stopIfIdle], which is the whole correction:
     * an app-wide grant must not wait on watches that have nothing to do with device control.
     */
    internal fun onWatchEnded(sessionId: String) {
        releaseGrantIfLastHold(sessionId)
        watches.remove(sessionId)
        stopIfIdle()
    }

    /**
     * Releases the grant when [sessionId]'s watch was the last one holding the device-control
     * channel open. Reports whether this call is what ended a grant.
     *
     * `@Synchronized` alongside [stopIfIdle] and [recordControlHeld]: two hold watches can end
     * concurrently (each on its own coroutine), and the release must be decided by exactly one of
     * them. A `remove`-then-`isEmpty` left unguarded lets both observe an empty set and both call
     * `stop()` — harmless in itself, since `stop()` is idempotent, but it makes the "this call
     * ended it" answer wrong for one of them, and that answer is what a caller would key a
     * user-visible action on.
     *
     * `internal` so the claim is asserted rather than argued (`RunNotificationServiceHoldReleaseTest`).
     * Nothing outside this class calls it.
     */
    @Synchronized
    internal fun releaseGrantIfLastHold(sessionId: String): Boolean {
        // A non-hold watch ending is not a device-control event at all — it must never touch the
        // grant, even when it happens to be the last watch running.
        if (!holdWatches.remove(sessionId)) return false
        if (holdWatches.isNotEmpty()) return false
        return releaseGrant()
    }

    @Synchronized
    private fun stopIfIdle() {
        if (watches.isEmpty()) {
            stopForeground(Service.STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    /**
     * The BACKSTOP release, covering a teardown no watch observed — the process being stopped, the
     * service being destroyed by the platform. The release that matters in normal operation is
     * [releaseGrantIfLastHold], on the hold's own end.
     *
     * A grant may only outlive the thing keeping its channel alive if something is left to end it,
     * and nothing else is: `device_control_stop` is the model choosing to release, which a run that
     * fails, is interrupted, or simply forgets never reaches.
     *
     * **Deliberately NOT gated on the grant looking active.** A grant whose binder already died is
     * `LOST`, not released: the hold and the notification behind it are still real and still ours to
     * end, and `stop()` reports true for precisely that case.
     *
     * Note this is the END of the service, never the end of an EPOCH. The hold cycles its connection
     * every poll by design; releasing there would revoke the grant a few seconds after every run and
     * restore the failure the hold was built to fix.
     */
    override fun onDestroy() {
        // FIRST, and before `scope.cancel()` takes the collector that would otherwise do it. Also
        // unconditional: an unheld release is a no-op on a non-reference-counted lock, so asking
        // "is it held" here could only ever be wrong in the direction that leaves it held.
        setScreenAwake(false)
        releaseGrant()
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val EXTRA_SESSION_ID = "com.mewbo.aura.notify.SESSION_ID"
        private const val EXTRA_LABEL = "com.mewbo.aura.notify.LABEL"

        /** Hold the session's event subscription past the run's terminal event,
         * so device tools stay answerable while the user is in another app. */
        private const val EXTRA_DEVICE_CONTROL = "com.mewbo.aura.notify.DEVICE_CONTROL"

        /** The notification's Stop action — releases every watch at once. */
        private const val EXTRA_STOP = "com.mewbo.aura.notify.STOP"

        /** Belt-and-suspenders reap: the watch normally ends promptly on `completion`/`stream_end`,
         * but a run that emits neither (a wedged stream past the server's own idle close) must not pin
         * a foreground service forever. Generous — above any realistic turn, far under the platform's
         * `dataSync` runtime cap. Hitting it drops the notification but frees the service; the run
         * continues server-side and the user sees it on reopen. */
        private const val MAX_WATCH_DURATION_MS = 20L * 60L * 1000L

        /**
         * The reap for a DEVICE-CONTROL hold, which is a different thing from a
         * notification watch.
         *
         * 20 minutes is a generous bound on "how long until a run emits its
         * terminal event"; it is a short one on "how long a person spends
         * ordering dinner". Reaping the hold at 20 minutes would take the
         * command channel down mid-session and reproduce the exact failure this
         * hold exists to prevent — so it gets its own, longer bound. Still
         * bounded: an abandoned session must not pin a foreground service, and
         * the user can end it from the notification at any time.
         *
         * **This is a ceiling, not the working bound.** What normally ends a hold
         * is `RunNotificationController`'s IDLE bound — silence on the stream, not
         * elapsed time. It stays for the case the idle bound cannot reach: a
         * stream still delivering events into a watch nobody is reading.
         *
         * **Two hours, measured against real windows, was never defensible.** The
         * longest legitimately-closed grant window in the event store ran 20.1
         * minutes; 45 is 2.2× that, and 2.7× shorter than what shipped. Two hours
         * was chosen when the idle bound was believed to be doing the work, and
         * it turned out to be the ONLY bound a held grant ever reached.
         */
        private const val MAX_DEVICE_HOLD_DURATION_MS = 45L * 60L * 1000L

        /**
         * The absolute lifetime of a GRANT, measured from the moment it was taken
         * and renewed by nothing — [releaseGrantIfExpired]'s bound.
         *
         * Deliberately the same length as [MAX_DEVICE_HOLD_DURATION_MS] and NOT
         * the same mechanism: that one bounds a single watch coroutine and dies
         * with it, this one bounds the grant across however many watches come and
         * go underneath it. A user asking about the weather in a fresh session
         * mints a new hold watch with new timers; it does not move this.
         */
        private const val MAX_GRANT_DURATION_MS = 45L * 60L * 1000L

        fun intent(
            context: Context,
            sessionId: String,
            label: String?,
            holdForDeviceControl: Boolean = false,
        ): Intent =
            Intent(context, RunNotificationService::class.java)
                .putExtra(EXTRA_SESSION_ID, sessionId)
                .putExtra(EXTRA_LABEL, label.orEmpty())
                .putExtra(EXTRA_DEVICE_CONTROL, holdForDeviceControl)

        /** Platform convention for a wake-lock tag is `app:reason`; it is what shows up in
         * `dumpsys power`, which is the only place a held lock is visible. */
        private const val WAKE_LOCK_TAG = "mewbo:device-control"

        /** The Stop action's intent — same service, no session, stop flag set. */
        fun stopIntent(context: Context): Intent =
            Intent(context, RunNotificationService::class.java)
                .putExtra(EXTRA_SESSION_ID, "stop")
                .putExtra(EXTRA_STOP, true)
    }
}
