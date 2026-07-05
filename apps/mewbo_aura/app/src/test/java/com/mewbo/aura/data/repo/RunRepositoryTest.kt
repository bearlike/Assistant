package com.mewbo.aura.data.repo

import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TextPayload
import java.io.IOException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.take
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [buildMulticastLiveFlow] is [RunRepository.live]'s testable core - a real SSE connection
 * (`SessionStreamClient`/OkHttp) isn't constructible in a plain-JVM test, so these drive it with a
 * synthetic upstream `Flow` instead, exercising the eviction-on-completion and
 * exception-materialization logic directly (review findings F1/F2). Collecting a `shareIn`-wrapped
 * `SharedFlow` never completes on its own (the exact trap these findings are about) - every test
 * here bounds its own collection with [take] rather than an unbounded `toList()`/`collect{}`,
 * mirroring the `transformWhile` fix in `ChatViewModel.subscribeLive`.
 */
class RunRepositoryTest {

    @Test
    fun `a session whose upstream completes on stream_end is evicted, so the NEXT call builds a fresh flow`() = runTest {
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()
        val completingUpstream: Flow<SessionEvent> = flow {
            emit(SessionEvent.Assistant(ts = "t1", payload = TextPayload("hi")))
            emit(SessionEvent.StreamEnd)
        }

        val first = buildMulticastLiveFlow("s1", completingUpstream, cache, backgroundScope)
        val collected = first.take(2).toList()

        assertEquals(listOf(true, true), listOf(collected[0] is SessionEvent.Assistant, collected[1] is SessionEvent.StreamEnd))
        assertTrue("expected the completed session to be evicted from the cache", cache.isEmpty())

        // A second call for the SAME session id, now that the entry was evicted, must build a
        // genuinely NEW SharedFlow - the whole point of eviction (F1: without it, this returns the
        // now-dead flow and every later turn in the same session silently receives nothing).
        val secondUpstream: Flow<SessionEvent> = flow { emit(SessionEvent.Assistant(ts = "t2", payload = TextPayload("second turn"))) }
        val second = buildMulticastLiveFlow("s1", secondUpstream, cache, backgroundScope)

        assertNotSame(first, second)
        assertEquals("second turn", (second.take(1).toList().single() as SessionEvent.Assistant).payload.text)
    }

    @Test
    fun `an upstream exception is materialized as a StreamError value, never thrown out of the flow`() = runTest {
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()
        val failingUpstream: Flow<SessionEvent> = flow {
            emit(SessionEvent.Assistant(ts = "t1", payload = TextPayload("before failure")))
            throw IOException("connection reset")
        }

        val liveFlow = buildMulticastLiveFlow("s2", failingUpstream, cache, backgroundScope)
        // Must complete normally after 2 items (Assistant + the materialized StreamError) rather
        // than propagating the IOException out of collection - a throw here would fail the test
        // via an uncaught exception, exactly what F2 says happens in the real app today.
        val collected = liveFlow.take(2).toList()

        val error = collected.filterIsInstance<SessionEvent.StreamError>().singleOrNull()
        assertTrue("expected a StreamError value, got: $collected", error != null)
        assertEquals("connection reset", error!!.message)
    }

    @Test
    fun `two calls for the SAME still-cached session id return the SAME SharedFlow instance`() = runTest {
        // Invariant (iv): the transcript collector and DeviceToolExecutor must share ONE
        // connection while a run is live - verified at the caching layer, the seam that actually
        // guarantees it (RunRepository.live is a thin wrapper with no logic of its own).
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()
        val upstream: Flow<SessionEvent> = flow { }

        val a = buildMulticastLiveFlow("s3", upstream, cache, backgroundScope)
        val b = buildMulticastLiveFlow("s3", upstream, cache, backgroundScope)

        assertSame(a, b)
    }
}
