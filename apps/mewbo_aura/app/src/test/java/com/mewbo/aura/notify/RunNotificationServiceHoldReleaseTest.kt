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
 * **An app-wide grant must be released by the last DEVICE-CONTROL hold ending, never by the whole
 * watch map emptying.** This is what made the reported leak permanent rather than merely long, and
 * nothing else in this package can see it.
 *
 * The defect: the release lived only in `onDestroy`, reached only through `stopIfIdle()`, which
 * requires EVERY watch to have ended. But the grant is app-wide — one screen, one shell-UID service
 * — while watches are per session, and once a grant exists `RunRepository.deviceControlInPlay()` is
 * true for every later run, so each new session added a watch carrying its own 2h cap. The release
 * therefore waited on the last of an unbounded, user-renewable set of timers: asking about the
 * weather in a fresh session pushed an abandoned grant's expiry out by another two hours.
 *
 * **Plain JVM, no Robolectric, and the path is the production one.** The service is constructed with
 * no lifecycle — the shape `RunNotificationServiceControlHeldTest` already uses — and
 * [RunNotificationService.onWatchEnded] is the exact method every watch job's `finally` calls,
 * however that watch ended. A REAL [DeviceControlSession] stands behind it (both collaborators are
 * narrow `fun interface`s, so the state machine runs with no Shizuku and no Android), so these
 * assert the grant genuinely changing state rather than a mock recording a call.
 *
 * **Every case leaves at least one entry in `watches`, and that is a constraint rather than a
 * choice.** `stopIfIdle()` on an empty map calls `stopForeground`/`stopSelf`, which a service with
 * no lifecycle cannot survive. It costs nothing here: the claim under test is precisely that the
 * grant's release does NOT wait for that map to empty.
 *
 * Not covered by anything, still: that a real watch job's `finally` reaches `onWatchEnded`, and
 * `stopIfIdle`'s own platform calls. Both need a real service lifecycle.
 */
class RunNotificationServiceHoldReleaseTest {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)

    /** A service holding a live grant, plus one non-hold watch that never ends — see the class KDoc
     * for why that keepalive is mandatory rather than incidental. */
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
        service.watches["keepalive"] = Job()
        return service to session
    }

    /** Routed through the production bookkeeping ([RunNotificationService.armWatch]) rather than
     * writing the sets directly, so a change to what "arming a hold" means reaches these too. */
    private fun RunNotificationService.armHold(sessionId: String) {
        watches[sessionId] = Job()
        armWatch(sessionId, hold = true)
    }

    private fun RunNotificationService.armPlainWatch(sessionId: String) {
        watches[sessionId] = Job()
        armWatch(sessionId, hold = false)
    }

    /**
     * The hole itself: an unrelated watch is still running, and the last device-control hold ending
     * must release the grant anyway.
     *
     * Before the fix this watch's `finally` reached `stopIfIdle()`, found other entries in the map,
     * and returned without ever touching the grant.
     */
    @Test
    fun `the last hold ending releases the grant even while another watch runs`() {
        val (service, session) = heldService()
        service.armHold("driving")
        service.armPlainWatch("weather")

        service.onWatchEnded("driving")

        assertFalse("the grant is released, with unrelated watches still running", session.isActive())
        assertTrue("and the unrelated watches are untouched", service.watches.containsKey("weather"))
    }

    /** A non-hold watch ending is not a device-control event and must never touch the grant. */
    @Test
    fun `a non-hold watch ending leaves the grant alone`() {
        val (service, session) = heldService()
        service.armHold("driving")
        service.armPlainWatch("weather")

        service.onWatchEnded("weather")

        assertTrue("an agent is still driving", session.isActive())
    }

    /**
     * **A completion watch must not release a grant merely because no hold happens to be armed.**
     *
     * This is the case the membership guard exists for, and nothing else here reaches it: with the
     * hold set already empty, an unguarded `remove` reads "the last hold just ended" from a session
     * that never held one and revokes the grant under an agent still driving. A grant CAN be held
     * with no hold armed — a run whose foreground-service start was refused takes one all the same —
     * and the honest answer there is that a completion watch is not the thing that ends it.
     */
    @Test
    fun `a plain watch ending with no hold armed leaves the grant alone`() {
        val (service, session) = heldService()
        service.armPlainWatch("weather")

        assertFalse("nothing was holding — there is nothing to release", service.releaseGrantIfLastHold("weather"))
        assertTrue("an unrelated watch ending must never revoke a grant", session.isActive())
    }

    /** With two holds live, the FIRST to end must not take the channel out from under the second. */
    @Test
    fun `the grant survives until the last of several holds ends`() {
        val (service, session) = heldService()
        service.armHold("first")
        service.armHold("second")

        service.onWatchEnded("first")
        assertTrue("one hold remains — the channel is still needed", session.isActive())

        service.onWatchEnded("second")
        assertFalse("the last hold's end is the release", session.isActive())
    }

    /**
     * The exactly-once shape [RunNotificationService.releaseGrantIfLastHold] reports, asserted
     * directly: a session that never held one reports nothing, and a repeat of one that already did
     * reports nothing further. Every teardown path can legitimately race the others, so this answer
     * has to be single-valued.
     */
    @Test
    fun `an unknown or repeated session reports no release`() {
        val (service, _) = heldService()
        service.armHold("driving")

        assertFalse("never armed a hold", service.releaseGrantIfLastHold("stranger"))
        assertTrue("this call is what ended the grant", service.releaseGrantIfLastHold("driving"))
        assertFalse("already released", service.releaseGrantIfLastHold("driving"))
    }

    @After
    fun tearDown() {
        scope.cancel()
    }
}
