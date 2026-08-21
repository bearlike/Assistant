package com.mewbo.aura.notify

import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.model.AgentMessagePayload
import com.mewbo.aura.data.model.SessionEvent
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock

/**
 * **The idle bound over a hold that is RECONNECTING — the shape production actually runs, and the
 * one no existing test reached.**
 *
 * `DeviceControlHoldIdleBoundTest` next door drives fixtures that never complete, so
 * `watchOneEpoch` never returns and `holdChannelOpen` — the rebuild loop — is never entered at all.
 * Every held grant on a real device is in that loop within milliseconds: the server reads liveness
 * before choosing a blocking timeout, so an idle session's stream is closed immediately
 * (`backend.py`'s stream generator: `queue.get(timeout=heartbeat if running else 0.0)` → drain → a
 * `session_state` frame → `stream_end`). The hold therefore rebuilds its subscription every
 * `POLL_IDLE_MS`, forever.
 *
 * **What that made true, and what these tests pin.** The bound used to be stamped on event ARRIVAL,
 * and each rebuild delivers frames the reconnect itself produced on a session where nothing
 * happened: a `session_state` frame, a `stream_end`, and — because the server's `after` cursor is
 * INCLUSIVE — the newest real event over again. So the stamp was refreshed about three times every
 * fifteen seconds, and the bound could never be reached on any device, on any session, ever. The
 * only thing left releasing an abandoned grant was a ceiling every new run pushed further out,
 * which is the overlay-only-a-force-stop-could-remove the device owner reported.
 *
 * These fixtures COMPLETE, exactly as the real upstream does, so they exercise the rebuild loop.
 * Under the arrival stamp all four hang past the bound; under the ts-advance stamp
 * (`StreamIdleClock`) they end on time.
 */
class DeviceControlHoldReconnectIdleTest {

    /**
     * The defect itself, at its smallest: a session that will never say anything again, whose
     * stream the server closes on every connect.
     *
     * Each epoch delivers only the two frames a reconnect manufactures. Neither carries a `ts`, so
     * neither is progress, and the hold ends at the bound however many times it reconnected.
     */
    @Test
    fun `a hold reconnecting onto a dead session still ends at the idle bound`() = runTest {
        var epochs = 0
        val closesImmediately = flow<SessionEvent> {
            epochs++
            emit(sessionStateFrame())
            emit(SessionEvent.StreamEnd)
        }
        var returned = false
        val watch = launch {
            controller(closesImmediately).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        advanceTimeBy(BOUND_MS - 1)
        runCurrent()
        assertFalse("one millisecond short of the bound is not the bound", returned)
        assertTrue(
            "the fixture must actually have reconnected many times, or this asserts nothing about " +
                "the rebuild loop (epochs=$epochs)",
            epochs >= BOUND_MS / POLL_MS / 2,
        )

        advanceTimeBy(2)
        runCurrent()
        assertTrue("reconnect churn is not progress — the bound is reached", returned)

        watch.join()
    }

    /**
     * **The inclusive `after` cursor, which is the subtler half of the same defect.**
     *
     * `SessionStreamClient` resumes with `?after=<newest ts seen>` and the server's cursor is
     * inclusive by design, so every rebuilt epoch re-delivers the session's newest real event. That
     * one carries a genuine `ts` — an arrival stamp cannot tell it from a fresh event, while a
     * ts-advance stamp sees it is the same event it already had.
     *
     * Without this case a denylist of `stream_end`/`session_state` frames would look like a
     * sufficient fix, and it is not.
     */
    @Test
    fun `a re-delivered duplicate event is not progress`() = runTest {
        val replaysTheSameEvent = flow<SessionEvent> {
            emit(event(FIXED_TS))
            emit(SessionEvent.StreamEnd)
        }
        var returned = false
        val watch = launch {
            controller(replaysTheSameEvent).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        advanceTimeBy(BOUND_MS - 1)
        runCurrent()
        assertFalse("still inside the bound", returned)

        advanceTimeBy(2)
        runCurrent()
        assertTrue("the same event arriving forever is a dead session, not a live one", returned)

        watch.join()
    }

    /**
     * The counterweight, and without it every case above would also pass against a hold that can
     * never survive at all: a session genuinely making progress across reconnects must NOT be
     * reaped. Taking the command channel down under an agent that is still driving is a worse
     * regression than the leak.
     *
     * Each epoch emits a STRICTLY newer `ts` — a real agent whose work spans several rebuilt
     * connections — and the run continues for twice the bound.
     */
    @Test
    fun `a hold whose session keeps advancing survives the bound across reconnects`() = runTest {
        var minted = 0
        val advancing = flow<SessionEvent> {
            minted++
            emit(event(tsAt(minted)))
            emit(SessionEvent.StreamEnd)
        }
        var returned = false
        val watch = launch {
            controller(advancing).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        // Stepped rather than advanced in one jump, so a hold that ends early is caught at the step
        // it ended on rather than only at the finish line.
        repeat(STEPS) { step ->
            advanceTimeBy(POLL_MS)
            runCurrent()
            assertFalse("a session still producing new events must survive step $step", returned)
        }

        assertTrue("the fixture must actually be minting new events", minted >= STEPS)
        assertTrue(
            "the run must outlast the bound, or this asserts nothing",
            STEPS * POLL_MS > BOUND_MS,
        )
        watch.cancelAndJoin()
    }

    /**
     * Progress THEN silence: an agent finishes driving, the session goes quiet, and the reconnect
     * loop carries on rebuilding regardless. The bound is measured from the last real event, never
     * from the last frame that happened to arrive.
     */
    @Test
    fun `a hold that advances and then falls silent ends at the bound`() = runTest {
        var minted = 0
        val stopsAdvancing = flow<SessionEvent> {
            if (minted < ADVANCES) minted++
            emit(event(tsAt(minted)))
            emit(SessionEvent.StreamEnd)
        }
        var returned = false
        val watch = launch {
            controller(stopsAdvancing).watchAndNotify("s", "Session", holdForDeviceControl = true)
            returned = true
        }
        runCurrent()

        // Epoch 1 runs at t=0 and each later one at a POLL_MS boundary, so the LAST advancing epoch
        // (the third) lands at t = 2 × POLL_MS. Everything after it replays the same `ts`.
        val lastAdvanceAt = (ADVANCES - 1) * POLL_MS
        advanceTimeBy(lastAdvanceAt)
        runCurrent()
        assertFalse("still advancing", returned)
        assertTrue("the fixture must have stopped advancing by now", minted == ADVANCES)

        advanceTimeBy(BOUND_MS - 1)
        runCurrent()
        assertFalse(
            "the bound runs from the last ADVANCE — with one millisecond to go it must not have fired",
            returned,
        )

        advanceTimeBy(2)
        runCurrent()
        assertTrue("silence past the bound ends the hold", returned)

        watch.join()
    }

    /** A `session_state` frame: real, yielded unconditionally on every connect, and carrying no
     * `ts`. It decodes to [SessionEvent.Unknown] because no variant claims that type. */
    private fun sessionStateFrame(): SessionEvent =
        SessionEvent.Unknown(type = "session_state", ts = "", raw = JsonObject(emptyMap()))

    private fun event(ts: String): SessionEvent =
        SessionEvent.AgentMessage(ts, AgentMessagePayload(text = "step", agentId = "root"))

    /** The backend's own shape — microseconds and a NUMERIC offset, never a bare `Z`. */
    private fun tsAt(n: Int): String = "2026-08-09T12:%02d:00.000000+00:00".format(n)

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
        /** Production's own bound and poll pacing — the separation this relies on is measured in
         * real minutes, so scaling either down here would quietly change what the test is about. */
        const val BOUND_MS = 8L * 60L * 1000L
        const val POLL_MS = 15_000L
        const val FIXED_TS = "2026-08-09T12:00:00.000000+00:00"

        /** 40 × 15s = 10 minutes, past the bound. */
        const val STEPS = 40

        /** How many epochs of the falling-silent fixture carry a fresh `ts`. */
        const val ADVANCES = 3
    }
}
