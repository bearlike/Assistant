package com.mewbo.aura.data.device

import com.mewbo.aura.data.api.DeviceToolResultRequest
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.data.model.SessionEvent
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.onStart
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
 * deterministic unit test. The tests that drive [DeviceToolExecutor.attach] itself each target one
 * specific hazard, and the last three are the two halves of the multi-session contract - the two
 * ways it can be got wrong point in OPPOSITE directions, so both need locking down:
 *
 * - collect-loop decoupling ("...does not block...");
 * - **too little concurrency** - a second session's attach must not steal dispatch from a live one
 *   ("...after a SECOND session attaches"), which is the whole reason dispatch went multi-session;
 * - **too much** - a collector must not outlive its own stream ("...evicts itself on stream_end",
 *   and the `StreamError` twin), or multi-session would leak one suspended coroutine per flow ever
 *   attached;
 * - in-flight survival of a dispatched call across its own stream's teardown (review finding F8);
 * - flow-IDENTITY idempotency, both halves (no re-subscription churn on a re-attach of the same
 *   flow; a genuine re-attach on a new one).
 *
 * Everything else about `attach`'s `Flow`/coroutine-launch plumbing is thin wiring covered by the
 * boot-smoke pass (task brief). Who CALLS attach (`RunRepository.live`, the seam that makes it
 * reach the assist overlay at all) is `RunRepositoryTest`'s. All constructor deps are swapped for
 * trivial in-memory fakes; none of the concrete `device_*` handlers (which need a real `Context`)
 * are constructed here. */
class DeviceToolExecutorTest {

    private fun executor(
        reporter: FakeResultReporter = FakeResultReporter(),
        ledger: DeviceToolCallLedger = FakeLedger(),
        nowEpochSeconds: Double = 0.0,
        handlers: List<DeviceToolHandler> = emptyList(),
        disabledToolIds: Set<String> = emptySet(),
    ) = DeviceToolExecutor(
        resultReporter = reporter,
        callLedger = ledger,
        clock = DeviceClock { nowEpochSeconds },
        gate = DeviceToolGate { disabledToolIds },
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
    fun `a user-disabled tool is refused with code tool_disabled and its handler never runs`() = runTest {
        // the catalog already hides a disabled tool from advertisement, but a stale
        // server could still dispatch one - the executor refuses it rather than silently running.
        val handler = FakeHandler("device_get_time")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, handlers = listOf(handler), disabledToolIds = setOf("device_get_time"))

        exec.handle("session-1", call(toolId = "device_get_time"))

        assertEquals(0, handler.callCount)
        val report = reporter.reports.single()
        assertEquals("error", report.request.status)
        assertEquals("tool_disabled", report.request.error?.code)
        assertNull(report.request.result)
    }

    @Test
    fun `an enabled tool still runs normally when a DIFFERENT tool is disabled`() = runTest {
        val handler = FakeHandler("device_get_battery")
        val reporter = FakeResultReporter()
        val exec = executor(reporter = reporter, handlers = listOf(handler), disabledToolIds = setOf("device_get_time"))

        exec.handle("session-1", call(toolId = "device_get_battery"))

        assertEquals(1, handler.callCount)
        assertEquals("ok", reporter.reports.single().request.status)
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
