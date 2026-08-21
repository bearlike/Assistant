package com.mewbo.aura.notify

import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.model.AgentMessagePayload
import com.mewbo.aura.data.model.SessionEvent
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock

/**
 * The device-control hold's idle bound — the thing that decides a grant has been abandoned.
 *
 * **What is at stake.** Before this bound existed, `holdChannelOpen` had no exit condition at all:
 * it returned only by cancellation, so a grant taken by a run whose session then died — the backend
 * losing the connection, or the model concluding the session server-side, neither of which reaches
 * this client as an event — outlived everything that could have ended it. Measured over real grant
 * windows, 6 of 24 never saw a `device_control_stop`, and the overlay announcing that an agent is
 * driving the device stayed lit, surviving the app being closed.
 *
 * **The clock IS the test scheduler's virtual clock**, wired through the primary constructor's
 * `nowMs`. That is what makes a fifteen-minute bound assertable in milliseconds of wall time, and it
 * is the reason the production constructor takes the durations and the clock as arguments at all
 * (the `VeilFade` precedent). A real clock here would leave the bound permanently unreachable while
 * every `delay` inside the class completed instantly — green, and proving nothing.
 *
 * **`runCurrent()`, never `advanceUntilIdle()`.** Two of these fixtures suspend forever on purpose,
 * which is exactly what a lost connection looks like; `advanceUntilIdle` would leap the virtual
 * clock to that far-future resumption and sail past the bound before any assertion ran.
 *
 * `RunNotifier` is mocked rather than faked: it is pure notification I/O (the house rule is to stub
 * only I/O boundaries) and it is `final`, which Mockito 5's inline mock maker handles — the same
 * reason `StagedAttachmentsReducerTest` mocks `Uri`.
 *
 * **Known weakness, stated rather than hidden: defeating the bound makes these tests HANG rather
 * than fail.** Mutating `idleFor >= holdIdleBoundMs` to a condition that never holds leaves
 * `awaitIdleBound` delaying forever, so the suite times out instead of going red. That is real
 * evidence of sensitivity to the bound, but it is weaker than a clean red and it is the shape this
 * module's test guide warns reads as a slow suite rather than a bug.
 * `RunNotificationServiceHoldReleaseTest` next door DOES fail cleanly under its own mutation, and
 * it covers the defect that actually produced the reported leak.
 */
class DeviceControlHoldIdleBoundTest {

    /**
     * A hold over a stream that has gone silent ends ITSELF — and that return is what releases the
     * grant, one level up in `RunNotificationService.onWatchEnded`.
     *
     * The fixture emits nothing and never completes, which is precisely a lost connection: no
     * `stream_end` and no `stream_error` either, because `SessionStreamClient` swallows the
     * `IOException` and reconnects forever. Silence is the only signal there is.
     */
    @Test
    fun `a hold whose stream goes silent ends itself once the idle bound elapses`() = runTest {
        var returned = false
        val watch = launch {
            controller(silent()).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        advanceTimeBy(BOUND_MS - 1)
        runCurrent()
        assertFalse("one millisecond short of the bound is not the bound", returned)

        advanceTimeBy(2)
        runCurrent()
        assertTrue("silence past the bound ends the hold", returned)

        watch.join()
    }

    /**
     * **The absence assertion, and the long wait is what gives it power.** A hold reaped while an
     * agent is genuinely mid-task is a worse regression than the leak: it takes the command channel
     * down under a session that is still working.
     *
     * Events arrive every 4 minutes — just past the 256s largest gap measured inside a real LIVE
     * grant — and the run continues for eighty times the bound, far beyond the 20.1-minute longest
     * legitimately-closed window. Asserted after a shorter run this passes against a build with no
     * bound at all, which is the whole trap.
     */
    @Test
    fun `a hold whose stream keeps delivering never ends, however long it runs`() = runTest {
        var delivered = 0
        val busy = flow {
            while (true) {
                delay(LIVE_GAP_MS)
                delivered++
                emit(event(tsAt(delivered)))
            }
        }
        var returned = false
        val watch = launch {
            controller(busy).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        // Stepped rather than advanced in one jump, so a hold that ends early is caught at the step
        // it ended on rather than only at the finish line.
        repeat(STEPS) { step ->
            advanceTimeBy(LIVE_GAP_MS)
            runCurrent()
            assertFalse("a live grant must survive gap $step — an agent is still driving", returned)
        }

        assertTrue("the fixture must actually be delivering events", delivered >= STEPS)
        assertTrue(
            "the run must outlast the bound many times over, or this asserts nothing",
            STEPS * LIVE_GAP_MS > BOUND_MS * 20,
        )
        watch.cancelAndJoin()
    }

    /**
     * Once a busy hold's traffic does stop, the bound still applies. Without this the test above
     * would also pass against a hold that can never end at all — the very defect under repair.
     */
    @Test
    fun `a hold that falls silent after a busy run still ends`() = runTest {
        val briefly = flow {
            repeat(3) {
                delay(LIVE_GAP_MS)
                emit(event(tsAt(it + 1)))
            }
            delay(Long.MAX_VALUE / 4)
        }
        var returned = false
        val watch = launch {
            controller(briefly).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        advanceTimeBy(3 * LIVE_GAP_MS)
        runCurrent()
        assertFalse("still inside the traffic", returned)

        advanceTimeBy(BOUND_MS + 1)
        runCurrent()
        assertTrue("the bound is measured from the LAST event, not from the watch's start", returned)

        watch.join()
    }

    /** A watch with no hold is unchanged: it ends at the run's terminal event, never at the bound. */
    @Test
    fun `a watch that is not holding for device control is untouched by the bound`() = runTest {
        val endsAtOnce = flow<SessionEvent> {
            emit(SessionEvent.StreamEnd)
            delay(Long.MAX_VALUE / 4)
        }
        var returned = false
        val watch = launch {
            controller(endsAtOnce).watchAndNotify("s", "Session", holdForDeviceControl = false)
            returned = true
        }
        runCurrent()

        assertTrue("a completion watch ends on its terminal, with no bound involved", returned)
        watch.join()
    }

    /** Emits nothing and never completes — the lost-connection shape. */
    private fun silent(): Flow<SessionEvent> = flow { delay(Long.MAX_VALUE / 4) }

    private fun event(ts: String): SessionEvent =
        SessionEvent.AgentMessage(ts, AgentMessagePayload(text = "step", agentId = "root"))

    /**
     * A STRICTLY increasing backend-shaped timestamp — microseconds and a numeric offset, never a
     * bare `Z`.
     *
     * **Monotonicity is load-bearing now, and it was not before.** The bound reads progress off the
     * event `ts` rather than off arrival ([StreamIdleClock]), so a fixture whose timestamps merely
     * LOOK distinct proves nothing about a busy grant. The shape here previously interpolated the
     * counter into the fractional field, where `1` and `10` both render 0.1 seconds — a hundred
     * consecutive non-advancing events, which under the current rule reads as a dead session.
     */
    private fun tsAt(n: Int): String =
        java.time.Instant.parse("2026-07-14T00:00:00Z").plusSeconds(n.toLong()).toString()
            .removeSuffix("Z") + "+00:00"

    private fun TestScope.controller(stream: Flow<SessionEvent>) =
        RunNotificationController(
            live = { stream },
            notifier = mock(RunNotifier::class.java),
            appForegroundChecker = AppForegroundChecker { false },
            nowMs = { testScheduler.currentTime },
            holdIdleBoundMs = BOUND_MS,
            pollIdleMs = POLL_MS,
        )

    private companion object {
        /** Production's own bound — the separation it relies on is measured in real minutes, so
         * scaling it down here would quietly change what the test is about. */
        const val BOUND_MS = 8L * 60L * 1000L

        /** Production's poll pacing, for the same reason. */
        const val POLL_MS = 15_000L

        /** Just past the 256s largest gap observed inside a real live grant. */
        const val LIVE_GAP_MS = 4L * 60L * 1000L

        /** 300 × 4min = 20 hours, i.e. 80× the bound. */
        const val STEPS = 300
    }
}
