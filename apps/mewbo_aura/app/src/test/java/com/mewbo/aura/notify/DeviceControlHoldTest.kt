package com.mewbo.aura.notify

import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.buildMulticastLiveFlow
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.shareIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The subscriber count is what keeps device tools answerable.
 *
 * Dispatch is a step in `live()`'s pipeline, and that pipeline stops shortly
 * after its LAST subscriber leaves. Chat's collector dies the moment Aura is
 * backgrounded — which is exactly what `device_action(action="launch")` does on
 * its way to the app it was told to open. So without a subscriber that outlives
 * the run, the tool that navigates destroys the transport for the tools after
 * it: the replayed incident shows a 30s timeout, then instant unavailable, for
 * every subsequent call including a trivial clock read.
 *
 * These drive the sharing behaviour directly rather than through the FGS, which
 * needs a real Android service — the property under test is "does a subscriber
 * remain", and that is observable here without one.
 *
 * ⚠️ **A `MutableSharedFlow` upstream never COMPLETES, so the first two tests below are
 * structurally incapable of witnessing a whole class of failure — and one of them shipped.** They
 * model "a subscriber went away", which a hot flow can express; they cannot model "the upstream
 * ENDED", which is what the real `SessionStreamClient` does on `stream_end` and what actually broke
 * the hold. A fake that cannot enter the failing state is not a weak test, it is a test of a
 * different subject. The last two tests use a COLD flow that completes, through the real
 * `buildMulticastLiveFlow`, precisely to cover that gap — keep both shapes.
 */
class DeviceControlHoldTest {

    /**
     * The house idiom for coroutines that by design never complete (`test/CLAUDE.md`).
     *
     * Every collector here rides a `SharedFlow`, whose `collect` never returns, and every sharing
     * coroutine outlives the body that started it. Launched on the `TestScope` they are structural
     * CHILDREN of the test job, so `runTest`'s leak check throws `UncompletedCoroutinesError` at
     * body end — and `cancel()` does not save it, because cancellation is dispatched, not immediate:
     * the check can run first. That is why this file passed alone and failed under concurrent Gradle
     * load, which is the worst way for it to fail, since the next person to see it red deletes it and
     * we lose the regression it was written to catch.
     *
     * An INDEPENDENT scope on the SAME `testScheduler` gets both halves right: `advanceUntilIdle()`
     * still drives it, and the leak check ignores it. `backgroundScope` is NOT the alternative —
     * `test/CLAUDE.md` records that its work does not run under this project's `advanceUntilIdle()`,
     * probed rather than assumed.
     */
    private fun TestScope.machineScope(): CoroutineScope =
        CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob())

    /** Hot-upstream shape: models a subscriber LEAVING. Cannot model the upstream ENDING — see the
     * class KDoc; the cold-upstream tests at the bottom of this file own that half. */
    @Test
    fun `a shared pipeline stops once its last subscriber leaves`() = runTest {
        // The defect, reproduced: with only a run-scoped collector, the upstream
        // stops after the run ends and nothing is left to answer a device call.
        val scope = machineScope()
        val upstream = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 8)
        var active = 0
        val shared = upstream
            .shareIn(
                scope = scope,
                started = SharingStarted.WhileSubscribed(stopTimeoutMillis = 5_000),
                replay = 0,
            )

        val runScoped = scope.launch {
            active++
            try {
                shared.collect { }
            } finally {
                active--
            }
        }
        advanceUntilIdle()
        assertEquals("the run's collector is subscribed", 1, active)

        runScoped.cancel() // the app is backgrounded / the turn ends
        advanceTimeBy(6_000)
        advanceUntilIdle()

        assertEquals("nothing is left subscribed", 0, active)
        scope.cancel()
    }

    @Test
    fun `a second collector keeps the pipeline alive when the first goes away`() = runTest {
        // The fix, in the only terms that matter: the FGS-scoped hold is a
        // second subscriber, so the run-scoped one leaving does not take the
        // channel with it.
        val scope = machineScope()
        val upstream = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 8)
        var active = 0
        val shared = upstream.shareIn(
            scope = scope,
            started = SharingStarted.WhileSubscribed(stopTimeoutMillis = 5_000),
            replay = 0,
        )

        scope.launch {
            active++
            try {
                shared.collect { }
            } finally {
                active--
            }
        }
        val runScoped = scope.launch {
            active++
            try {
                shared.collect { }
            } finally {
                active--
            }
        }
        advanceUntilIdle()
        assertEquals(2, active)

        runScoped.cancel() // backgrounded, exactly as before
        advanceTimeBy(6_000)
        advanceUntilIdle()

        assertTrue("the hold is still subscribed, so device calls still land", active >= 1)
        scope.cancel()
    }

    @Test
    fun `the hold is opt-in per session, not armed for every run`() {
        // A hold costs a live SSE connection and a persistent notification; a
        // session with no device tools must not pay either.
        val armed = mutableListOf<Pair<String, Boolean>>()
        val notifications = com.mewbo.aura.data.repo.RunNotifications { id, _, deviceControl ->
            armed += id to deviceControl
        }

        notifications.onRunStarted("plain", null, deviceControl = false)
        notifications.onRunStarted("device", null, deviceControl = true)

        assertEquals(listOf("plain" to false, "device" to true), armed)
    }

    /**
     * The measured defect, in the ONE shape the two tests above cannot express.
     *
     * Both of them stand a `MutableSharedFlow` in for the upstream — and a `MutableSharedFlow` never
     * COMPLETES, so no amount of holding can ever observe what actually happens. The real upstream is
     * `SessionStreamClient.stream`, a COLD flow that completes on `stream_end`, and the server sends
     * `stream_end` immediately for a session with no run in flight. Once that upstream completes,
     * `WhileSubscribed` does not restart it and `SharedFlow.collect` never returns: the hold sits on a
     * flow that will never emit again. On device that read as the persistent "Mewbo can control this
     * device" notification showing over a process holding zero TCP sockets.
     */
    @Test
    fun `a completed upstream leaves the shared flow permanently dead`() = runTest {
        val scope = machineScope()
        val cache = mutableMapOf<String, kotlinx.coroutines.flow.SharedFlow<SessionEvent>>()
        var upstreamsBuilt = 0
        // A cold upstream that ends the way the real one does: the terminal frame, then completion.
        val coldUpstream = flow {
            upstreamsBuilt++
            emit(SessionEvent.StreamEnd)
        }

        val first = buildMulticastLiveFlow("s", coldUpstream, cache, scope, dispatch = { _, _ -> })
        scope.launch { first.collect { } }
        advanceUntilIdle()

        var lateEvents = 0
        val late = scope.launch { first.collect { lateEvents++ } }
        advanceUntilIdle()

        assertEquals("the upstream ran once and completed", 1, upstreamsBuilt)
        assertEquals("a collector subscribing after completion receives nothing", 0, lateEvents)
        assertTrue("and it never returns — this is the hold, holding nothing", late.isActive)
        scope.cancel()
    }

    /**
     * The fix's shape: the epoch ENDS at the transport terminal and the channel is rebuilt by asking
     * for a fresh flow, which the cache eviction makes a genuinely new upstream. Re-subscribing to the
     * old handle would not have been enough — that is the test above.
     */
    @Test
    fun `asking for a fresh flow after the upstream completes rebuilds the channel`() = runTest {
        val scope = machineScope()
        val cache = mutableMapOf<String, kotlinx.coroutines.flow.SharedFlow<SessionEvent>>()
        var upstreamsBuilt = 0
        val coldUpstream = flow {
            upstreamsBuilt++
            emit(SessionEvent.StreamEnd)
        }
        fun live() = buildMulticastLiveFlow("s", coldUpstream, cache, scope, dispatch = { _, _ -> })

        scope.launch { live().collect { } }
        advanceUntilIdle()
        assertEquals(1, upstreamsBuilt)

        // A second epoch — exactly what the hold's loop does once its epoch ends.
        scope.launch { live().collect { } }
        advanceUntilIdle()

        assertEquals("the channel was genuinely rebuilt, not re-attached to the dead one", 2, upstreamsBuilt)
        scope.cancel()
    }

    @Suppress("unused")
    private val unusedRepositoryReference: Class<RunRepository> = RunRepository::class.java
}
