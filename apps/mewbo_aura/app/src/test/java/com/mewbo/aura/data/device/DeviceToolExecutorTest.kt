package com.mewbo.aura.data.device

import com.mewbo.aura.data.api.DeviceToolResultRequest
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.data.model.SessionEvent
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** [DeviceToolExecutor.handle] is exercised directly (it's `internal`, visible from this test
 * source set) for dedup/staleness/error-mapping - the actual decision logic worth a fast,
 * deterministic unit test. The one test that exercises [DeviceToolExecutor.attach] itself
 * ("...does not block...") targets the collect-loop decoupling specifically (review finding);
 * everything else about `attach`'s `Flow`/coroutine-launch plumbing is thin wiring covered by the
 * boot-smoke pass (task brief). All constructor deps are swapped for trivial in-memory fakes;
 * none of the concrete `device_*` handlers (which need a real `Context`) are constructed here. */
class DeviceToolExecutorTest {

    private fun executor(
        reporter: FakeResultReporter = FakeResultReporter(),
        ledger: DeviceToolCallLedger = FakeLedger(),
        nowEpochSeconds: Double = 0.0,
        handlers: List<DeviceToolHandler> = emptyList(),
    ) = DeviceToolExecutor(
        resultReporter = reporter,
        callLedger = ledger,
        clock = DeviceClock { nowEpochSeconds },
        handlers = handlers,
        scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined),
    )

    private fun call(
        callId: String = "call-1",
        callToken: String = "token-1",
        toolId: String = "device_get_time",
        expiresAt: Double = 100.0,
    ) = DeviceToolCallPayload(callId = callId, callToken = callToken, toolId = toolId, args = buildJsonObject {}, expiresAt = expiresAt)

    @Test
    fun `same call_id handled twice only executes the handler once`() = runTest {
        val handler = FakeHandler("device_get_time")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, handlers = listOf(handler))
        val theCall = call()

        exec.handle("session-1", theCall)
        exec.handle("session-1", theCall)

        assertEquals(1, handler.callCount)
        assertEquals(1, reporter.reports.size)
    }

    @Test
    fun `a replayed call_id is dropped even against a fresh executor instance sharing the same ledger`() = runTest {
        val ledger = FakeLedger()
        val handler1 = FakeHandler("device_get_time")
        executor(ledger = ledger, handlers = listOf(handler1)).handle("session-1", call())

        val handler2 = FakeHandler("device_get_time")
        val reporter2 = FakeResultReporter()
        executor(reporter = reporter2, ledger = ledger, handlers = listOf(handler2)).handle("session-1", call())

        assertEquals(0, handler2.callCount)
        assertTrue(reporter2.reports.isEmpty())
    }

    @Test
    fun `a call within the skew grace period still executes normally`() = runTest {
        // now is 30s past expiresAt - within the 60s grace (review finding F5: the device clock
        // has no guaranteed sync with the server's, so a strict now-greater-than cut would break
        // every call the instant the device clock runs even slightly ahead).
        val handler = FakeHandler("device_get_time")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, nowEpochSeconds = 1_030.0, handlers = listOf(handler))

        exec.handle("session-1", call(expiresAt = 1_000.0))

        assertEquals(1, handler.callCount)
        assertEquals("ok", reporter.reports.single().request.status)
    }

    @Test
    fun `a call past the skew grace period is reported stale_call - handler never runs, but a result IS posted`() = runTest {
        // now is 90s past expiresAt - beyond the 60s grace.
        val handler = FakeHandler("device_get_time")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, nowEpochSeconds = 1_090.0, handlers = listOf(handler))

        exec.handle("session-1", call(callId = "stale-1", callToken = "tok-stale", expiresAt = 1_000.0))

        assertEquals(0, handler.callCount)
        val report = reporter.reports.single()
        assertEquals("stale-1", report.callId)
        assertEquals("tok-stale", report.request.callToken)
        assertEquals("error", report.request.status)
        assertEquals("stale_call", report.request.error?.code)
        assertNull(report.request.result)
    }

    @Test
    fun `a call at exactly its expiry is NOT dropped - only strictly-after expires it`() = runTest {
        val handler = FakeHandler("device_get_time")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, nowEpochSeconds = 1_000.0, handlers = listOf(handler))

        exec.handle("session-1", call(expiresAt = 1_000.0))

        assertEquals(1, handler.callCount)
        assertEquals("ok", reporter.reports.single().request.status)
    }

    @Test
    fun `a successful handler reports status ok with its result and the call token`() = runTest {
        val resultJson = buildJsonObject { put("level_percent", 42) }
        val handler = FakeHandler("device_get_battery", result = resultJson)
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, handlers = listOf(handler))

        exec.handle("session-9", call(callId = "c5", callToken = "tok-5", toolId = "device_get_battery"))

        val report = reporter.reports.single()
        assertEquals("session-9", report.sessionId)
        assertEquals("c5", report.callId)
        assertEquals("ok", report.request.status)
        assertEquals("tok-5", report.request.callToken)
        assertEquals(resultJson, report.request.result)
        assertNull(report.request.error)
    }

    @Test
    fun `a throwing handler reports status error with code handler_error and the exception message`() = runTest {
        val handler = FakeHandler("device_set_alarm", throwable = IllegalArgumentException("bad hour"))
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, handlers = listOf(handler))

        exec.handle("session-1", call(toolId = "device_set_alarm"))

        val report = reporter.reports.single()
        assertEquals("error", report.request.status)
        assertEquals("handler_error", report.request.error?.code)
        assertEquals("bad hour", report.request.error?.message)
        assertNull(report.request.result)
    }

    @Test
    fun `an unknown tool_id reports status error with code unknown_tool without invoking any handler`() = runTest {
        val handler = FakeHandler("device_get_time")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, handlers = listOf(handler))

        exec.handle("session-1", call(toolId = "device_does_not_exist"))

        assertEquals(0, handler.callCount)
        val report = reporter.reports.single()
        assertEquals("error", report.request.status)
        assertEquals("unknown_tool", report.request.error?.code)
    }

    @Test
    fun `attach decouples per-call handling - a handler that suspends indefinitely does not block a later, different call_id`() = runTest {
        val slowHandler = FakeHandler("device_get_time", suspendForever = true)
        val fastHandler = FakeHandler("device_get_battery")
        val reporter = FakeResultReporter()
        val exec = DeviceToolExecutor(
            resultReporter = reporter,
            callLedger = FakeLedger(),
            clock = DeviceClock { 0.0 },
            handlers = listOf(slowHandler, fastHandler),
            // Unconfined (the same manual scope the executor() helper above uses) rather than
            // runTest's own scope/backgroundScope: the slow handler's child coroutine is
            // INTENTIONALLY left suspended forever by this test, and it lives entirely outside
            // runTest's own job tree this way, so there's nothing for runTest's leak detection to
            // trip on. Unconfined also runs attach()'s collector - and each event's dispatched
            // handle() - eagerly/synchronously, so plain emit() calls are enough on their own; no
            // advanceUntilIdle() timing dance needed to get the collector subscribed first.
            scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined),
        )
        val events = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 4)

        exec.attach("session-1", events)
        events.emit(SessionEvent.DeviceToolCall(ts = "t1", payload = call(callId = "slow-call", toolId = "device_get_time")))
        events.emit(SessionEvent.DeviceToolCall(ts = "t2", payload = call(callId = "fast-call", toolId = "device_get_battery")))

        // The slow call WAS picked up (proves it isn't silently dropped)...
        assertEquals(1, slowHandler.callCount)
        // ...but never finishes, so it never reaches the reporter...
        assertTrue(reporter.reports.none { it.callId == "slow-call" })
        // ...and critically, the SECOND, unrelated call_id was still processed instead of being
        // stuck behind the first one in the collect loop (this assertion is what fails without
        // the attach() fix: with handle() awaited inline, the collector's own coroutine is the one
        // that ends up parked on the slow handler's awaitCancellation(), so it never gets back
        // around to the second emitted event and the fast call's handler never runs).
        assertEquals(1, fastHandler.callCount)
        assertTrue(reporter.reports.any { it.callId == "fast-call" && it.request.status == "ok" })
    }

    @Test
    fun `an in-flight handle survives detach - a session switch does not cancel an already-dispatched call`() = runTest {
        // Review finding F8: if the child coroutine running handle() were parented to attach()'s
        // own collect job, detach()'s job.cancel() would abort it mid-flight - between a handler's
        // real-world side effect (e.g. an SMS actually sent) and the result POST that tells the
        // server it succeeded, risking a server-side timeout-and-retry that sends a genuine
        // duplicate. This gate simulates exactly that in-flight window.
        val gate = CompletableDeferred<Unit>()
        val handler = object : DeviceToolHandler {
            override val toolId = "device_send_sms"
            override suspend fun execute(args: JsonObject): JsonObject {
                gate.await()
                return buildJsonObject {}
            }
        }
        val reporter = FakeResultReporter()
        val exec = DeviceToolExecutor(
            resultReporter = reporter,
            callLedger = FakeLedger(),
            clock = DeviceClock { 0.0 },
            handlers = listOf(handler),
            scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined),
        )
        val events = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 4)

        exec.attach("session-1", events)
        events.emit(SessionEvent.DeviceToolCall(ts = "t1", payload = call(callId = "in-flight-call", toolId = "device_send_sms")))
        // handle() is now suspended on gate.await() - simulate a session switch happening WHILE
        // the call is genuinely in flight, exactly as ChatViewModel.bind() would trigger.
        exec.detach()

        gate.complete(Unit) // release the held-open handler

        val report = reporter.reports.single()
        assertEquals("in-flight-call", report.callId)
        assertEquals("ok", report.request.status)
    }

    private class FakeResultReporter : DeviceToolResultReporter {
        data class Report(val sessionId: String, val callId: String, val request: DeviceToolResultRequest)

        val reports = mutableListOf<Report>()

        override suspend fun report(sessionId: String, callId: String, request: DeviceToolResultRequest) {
            reports += Report(sessionId, callId, request)
        }
    }

    private class FakeLedger : DeviceToolCallLedger {
        private val seen = mutableSetOf<String>()

        override suspend fun recordIfNew(callId: String): Boolean = seen.add(callId)
    }

    private class FakeHandler(
        override val toolId: String,
        private val result: JsonObject = buildJsonObject {},
        private val throwable: Throwable? = null,
        private val suspendForever: Boolean = false,
    ) : DeviceToolHandler {
        var callCount = 0
            private set

        override suspend fun execute(args: JsonObject): JsonObject {
            callCount++
            if (suspendForever) awaitCancellation()
            throwable?.let { throw it }
            return result
        }
    }
}
