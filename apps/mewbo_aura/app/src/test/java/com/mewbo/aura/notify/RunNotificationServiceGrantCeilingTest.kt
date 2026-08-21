package com.mewbo.aura.notify

import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.shizuku.DeviceControlBinder
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.DeviceControlStatusSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **The two fallbacks that survive an actively-used app** — a grant ceiling that nothing renews, and
 * hold membership that a second run cannot silently drop.
 *
 * Every other bound on a grant is per WATCH, and a watch is created per run. Once a grant exists
 * `RunRepository.deviceControlInPlay()` is true for EVERY later run, so each new session mints a
 * fresh hold watch with fresh timers while the release waits for the whole set to empty. An
 * ordinarily active user renews an abandoned grant indefinitely without doing anything unusual —
 * the two longest leaked windows in the event store ran 15.7 and 26.5 hours, on a device whose
 * shipped build carried both the idle bound and the ceiling.
 *
 * Plain JVM, the shape `RunNotificationServiceHoldReleaseTest` established: the service is
 * constructed with no lifecycle, a REAL [DeviceControlSession] stands behind it (both collaborators
 * are narrow `fun interface`s), and the clock is injected so a 45-minute ceiling is assertable in
 * microseconds of wall time. Every case leaves an entry in `watches`, because `stopIfIdle()` on an
 * empty map calls `stopForeground`/`stopSelf`, which a lifecycle-less service cannot survive.
 *
 * **Not covered here, and it needs a real lifecycle:** that the `active` collector actually arms the
 * ceiling watchdog, and that `onStartCommand` reaches [RunNotificationService.admitWatch] — that
 * method calls `startForeground` before it gets there.
 */
class RunNotificationServiceGrantCeilingTest {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
    private var now = 0L

    /** A service holding a live grant, with the grant anchor stamped through the production
     * bookkeeping ([RunNotificationService.recordGrantPresence]) rather than written directly. */
    private fun heldService(): Pair<RunNotificationService, DeviceControlSession> {
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        val session = DeviceControlSession(
            statusSource = DeviceControlStatusSource { status },
            binder = DeviceControlBinder { true },
            scope = scope,
        )
        runBlocking { session.start() }
        assertTrue("precondition: the grant is held", session.isActive())

        val service = RunNotificationService()
        service.deviceControlSession = session
        service.nowMs = { now }
        service.watches["keepalive"] = Job()
        service.recordGrantPresence(true)
        return service to session
    }

    /**
     * The ceiling fires on a grant nothing has ended, while a hold watch is still very much alive.
     *
     * This is the case no per-watch bound can reach: the watch has not returned, its own cap has not
     * elapsed, and the set is not empty — yet the grant has to go.
     */
    @Test
    fun `the grant ceiling releases a grant while a hold watch is still running`() {
        val (service, session) = heldService()
        service.armHold("driving")

        now = CEILING_MS - 1
        assertFalse("one millisecond short of the ceiling is not the ceiling", service.releaseGrantIfExpired())
        assertTrue("and the grant is untouched", session.isActive())

        now = CEILING_MS
        assertTrue("the ceiling is what ended it", service.releaseGrantIfExpired())
        assertFalse("the grant is gone with a hold watch still running", session.isActive())
    }

    /**
     * **The renewal defect, stated as a test.** New runs keep arriving — each one arming its own
     * hold watch, each one a fresh per-watch timer — and the ceiling must not move.
     *
     * Against a per-watch anchor this passes only by accident of ordering; against a grant-scoped
     * one it holds however many watches come and go.
     */
    @Test
    fun `later runs cannot push the ceiling out`() {
        val (service, session) = heldService()
        service.armHold("driving")

        // A user going about their day: a fresh session every ten minutes, each arming its own hold
        // because a grant exists, each re-asserting the presence the way the grant flow does.
        for (minute in longArrayOf(10, 20, 30, 40)) {
            now = minute * 60_000L
            service.armHold("session-$minute")
            service.recordGrantPresence(true)
            assertFalse(
                "not yet — but nothing here may extend the ceiling either (t=${minute}min)",
                service.releaseGrantIfExpired(),
            )
        }

        now = CEILING_MS
        assertTrue("the ceiling is anchored to the grant, not to the newest watch", service.releaseGrantIfExpired())
        assertFalse("the grant is released despite five live holds", session.isActive())
    }

    /** No grant, nothing to expire — the ceiling must never report a release it did not perform. */
    @Test
    fun `the ceiling reports nothing when no grant is held`() {
        val (service, _) = heldService()
        service.recordGrantPresence(false)

        now = CEILING_MS * 10
        assertFalse("there is no grant to expire", service.releaseGrantIfExpired())
    }

    /** A grant taken, released, and taken again starts a FRESH ceiling — the anchor is cleared on
     * the drop, so the second grant is not born already expired. */
    @Test
    fun `a new grant gets a full ceiling of its own`() {
        val (service, session) = heldService()
        service.armHold("first")

        now = CEILING_MS
        assertTrue(service.releaseGrantIfExpired())

        runBlocking { session.start() }
        service.recordGrantPresence(true)
        service.armHold("second")

        now = CEILING_MS * 2 - 1
        assertFalse("the second grant's own clock started at the second grant", service.releaseGrantIfExpired())
        now = CEILING_MS * 2
        assertTrue("and expires a full ceiling after it", service.releaseGrantIfExpired())
    }

    /**
     * **The arming gap: a session's SECOND run is the one that arms the hold.**
     *
     * Shizuku is started between two runs of one session, so `deviceControlInPlay()` is false at the
     * first and true at the second. The first run's watch is still draining, so the second start
     * creates no job — and when arming lived inside that guard, the hold membership was dropped on
     * the floor. A grant taken by that second run then had no entry in `holdWatches`, so the last
     * hold ending could never release it.
     */
    @Test
    fun `a hold armed by a later run of an already-watched session is still recorded`() {
        val (service, session) = heldService()

        // Run 1: no grant possible yet, so a plain completion watch.
        assertTrue("a fresh session needs a watch job", service.admitWatch("driving", hold = false))
        service.watches["driving"] = Job()

        // Run 2, while run 1's watch is still draining: now the run may take control.
        assertFalse("the session is already watched — no second job", service.admitWatch("driving", hold = true))

        assertTrue(
            "the hold armed by the second run is what releases the grant",
            service.releaseGrantIfLastHold("driving"),
        )
        assertFalse(session.isActive())
    }

    /** Routed through the production bookkeeping rather than writing the sets directly. */
    private fun RunNotificationService.armHold(sessionId: String) {
        watches[sessionId] = Job()
        armWatch(sessionId, hold = true)
    }

    @After
    fun tearDown() {
        scope.cancel()
    }

    private companion object {
        /** Production's own grant ceiling. Scaling it down here would quietly change what these
         * tests are about — the claim is that a REAL 45 minutes cannot be renewed. */
        const val CEILING_MS = 45L * 60L * 1000L
    }
}
