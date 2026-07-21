package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.DeviceToolResultRequest
import com.mewbo.aura.data.api.RecoverSessionRequest
import com.mewbo.aura.data.api.RecoverSessionResponseDto
import com.mewbo.aura.data.device.DeviceClock
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.DeviceToolCallLedger
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.device.DeviceToolDispatch
import com.mewbo.aura.data.device.DeviceToolExecutor
import com.mewbo.aura.data.device.DeviceToolGate
import com.mewbo.aura.data.device.DeviceToolHandler
import com.mewbo.aura.data.device.DeviceToolResultReporter
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TextPayload
import com.mewbo.aura.data.sse.SessionStreamClient
import java.io.IOException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.take
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`
import retrofit2.HttpException
import retrofit2.Response

/**
 * [buildMulticastLiveFlow] is [RunRepository.live]'s testable core - and its ONLY logic; a real SSE
 * connection (`SessionStreamClient`/OkHttp, which needs a `SettingsStore`) isn't constructible in a
 * plain-JVM test, so these drive it with a synthetic upstream `Flow` instead, exercising the
 * eviction-on-completion and exception-materialization logic (review findings F1/F2) plus the
 * device-tool attach directly. Collecting a `shareIn`-wrapped `SharedFlow` never completes on its
 * own (the exact trap those findings are about) - every test here bounds its own collection with
 * [take] rather than an unbounded `toList()`/`collect{}`, mirroring the `transformWhile` fix in
 * `ChatViewModel.subscribeLive`.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class RunRepositoryTest {

    /** The three pre-existing tests below predate the device-tool attach and don't exercise it. */
    private val noDispatch = DeviceToolDispatch { _, _ -> }

    @Test
    fun `a session whose upstream completes on stream_end is evicted, so the NEXT call builds a fresh flow`() = runTest {
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()
        val completingUpstream: Flow<SessionEvent> = flow {
            emit(SessionEvent.Assistant(ts = "t1", payload = TextPayload("hi")))
            emit(SessionEvent.StreamEnd)
        }

        val first = buildMulticastLiveFlow("s1", completingUpstream, cache, backgroundScope, noDispatch)
        val collected = first.take(2).toList()

        assertEquals(listOf(true, true), listOf(collected[0] is SessionEvent.Assistant, collected[1] is SessionEvent.StreamEnd))
        assertTrue("expected the completed session to be evicted from the cache", cache.isEmpty())

        // A second call for the SAME session id, now that the entry was evicted, must build a
        // genuinely NEW SharedFlow - the whole point of eviction (F1: without it, this returns the
        // now-dead flow and every later turn in the same session silently receives nothing).
        val secondUpstream: Flow<SessionEvent> = flow { emit(SessionEvent.Assistant(ts = "t2", payload = TextPayload("second turn"))) }
        val second = buildMulticastLiveFlow("s1", secondUpstream, cache, backgroundScope, noDispatch)

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

        val liveFlow = buildMulticastLiveFlow("s2", failingUpstream, cache, backgroundScope, noDispatch)
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

        val a = buildMulticastLiveFlow("s3", upstream, cache, backgroundScope, noDispatch)
        val b = buildMulticastLiveFlow("s3", upstream, cache, backgroundScope, noDispatch)

        assertSame(a, b)
    }

    /**
     * THE regression test for the overlay device-tool bug. The assist overlay
     * (`AuraSession`/`AssistTurnMachine`) follows a run through exactly ONE call -
     * `liveEvents = { id -> runRepository.live(id) }` - and collects it for RENDERING only; it never
     * touched `DeviceToolExecutor`. Since `RunRepository.sendQuery` advertises all nine `device_*`
     * tools to the server unconditionally, an overlay turn that called one got no answer at all: the
     * server blocked for its full `DEVICE_TOOL_TIMEOUT_S` (30s, twice over in the reported session)
     * and returned `device_timeout`, so the model never reached the tool it wanted next (the "clock
     * and alarm are not exposed" report). The server's own `has_subscribers()` safety net can't
     * catch it either - the overlay IS an SSE subscriber, just not an executor.
     *
     * So the assertion is made from the caller site that only ever calls [live] and then renders: a
     * `device_tool_call` arriving on that flow must be dispatched to the real handler and its result
     * reported, with NOTHING device-tool-shaped done by the caller. A REAL [DeviceToolExecutor] runs
     * here (only its two I/O boundaries - the result POST and the handler's device access - are
     * faked), because the bug was never in the executor's logic; it was in who, if anyone, had wired
     * it up.
     */
    @Test
    fun `a caller that only follows the run via live - the assist overlay - still gets device_tool_call dispatched and reported`() = runTest {
        val reporter = RecordingReporter()
        val handler = FakeHandler("device_get_time", buildJsonObject { put("iso", "2026-07-12T09:00:00+00:00") })
        val executor = executor(reporter, handler)
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()
        val upstream: Flow<SessionEvent> = flow {
            emit(SessionEvent.DeviceToolCall(ts = "t1", payload = deviceToolCall()))
            emit(SessionEvent.StreamEnd)
        }

        // The overlay's ENTIRE interaction with the run: one live() call, then collect it to render
        // the transcript. Nothing device-tool-shaped at all. (This is buildMulticastLiveFlow rather
        // than RunRepository.live only because live()'s other collaborators aren't JVM-constructible;
        // live()'s whole body is this call.)
        buildMulticastLiveFlow("s-overlay", upstream, cache, shareScope(), executor).take(2).toList()
        advanceUntilIdle()

        assertEquals(1, handler.callCount)
        val report = reporter.reports.single()
        assertEquals("s-overlay", report.sessionId)
        assertEquals("call-1", report.callId)
        assertEquals("tok-1", report.request.callToken)
        assertEquals("ok", report.request.status)
    }

    /**
     * The steal bug this design makes unrepresentable. An earlier fix had the executor SUBSCRIBE to
     * each session's flow, keeping one collector and cancelling the previous one on every new attach
     * - so opening the assist overlay while a chat run was mid-flight silently stopped answering the
     * chat run's tools (and vice versa), re-creating the same 30s `device_timeout` through a narrower
     * door. Dispatch now rides each session's OWN pipeline, so there is no shared slot to steal: a
     * call on session A's flow must still be dispatched after session B's flow exists and is live.
     */
    @Test
    fun `a device_tool_call on one session's flow is still dispatched after a SECOND session's flow goes live`() = runTest {
        val reporter = RecordingReporter()
        val handler = FakeHandler("device_get_time", buildJsonObject { put("iso", "2026-07-12T09:00:00+00:00") })
        val executor = executor(reporter, handler)
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()

        // Session A (the chat) is mid-run: its stream is open and has emitted nothing terminal.
        val chatUpstream = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 8)
        val chatLive = buildMulticastLiveFlow("s-chat", chatUpstream, cache, shareScope(), executor)
        val chatCollector = launch { chatLive.collect { } }
        advanceUntilIdle()

        // Session B (the assist overlay) is invoked and goes live on its own flow.
        val overlayUpstream = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 8)
        val overlayLive = buildMulticastLiveFlow("s-overlay", overlayUpstream, cache, shareScope(), executor)
        val overlayCollector = launch { overlayLive.collect { } }
        advanceUntilIdle()

        // The CHAT's still-live run now calls a device tool. Under the cancelling-attach design this
        // was answered by nobody.
        chatUpstream.emit(SessionEvent.DeviceToolCall(ts = "t1", payload = deviceToolCall(callId = "chat-call")))
        overlayUpstream.emit(SessionEvent.DeviceToolCall(ts = "t2", payload = deviceToolCall(callId = "overlay-call")))
        advanceUntilIdle()

        assertEquals(2, handler.callCount)
        // Each reported under the session it actually arrived on - a shared/mixed-up dispatch would
        // surface here as a call POSTed against the wrong session id.
        assertEquals("s-chat", reporter.reports.single { it.callId == "chat-call" }.sessionId)
        assertEquals("s-overlay", reporter.reports.single { it.callId == "overlay-call" }.sessionId)

        chatCollector.cancel()
        overlayCollector.cancel()
    }

    /**
     * Dispatch is UPSTREAM of the `shareIn`, so it does not depend on who is subscribed when the
     * event arrives. That matters because the `SharedFlow` has `replay = 0`: an event emitted while
     * no collector is attached is simply gone from the SharedFlow's point of view. Here the only
     * client takes ONE event and leaves; the `device_tool_call` lands afterwards, with zero
     * subscribers, and must STILL be dispatched and reported. (It also exercises
     * [LIVE_STREAM_STOP_TIMEOUT_MS] - the upstream is still alive at that point precisely because the
     * stop timeout hasn't elapsed.)
     */
    @Test
    fun `a device_tool_call arriving while NO collector is subscribed is still dispatched`() = runTest {
        val reporter = RecordingReporter()
        val handler = FakeHandler("device_get_time", buildJsonObject { put("iso", "2026-07-12T09:00:00+00:00") })
        val executor = executor(reporter, handler)
        val cache = mutableMapOf<String, SharedFlow<SessionEvent>>()
        val upstream: Flow<SessionEvent> = flow {
            emit(SessionEvent.Assistant(ts = "t0", payload = TextPayload("thinking")))
            delay(100)
            emit(SessionEvent.DeviceToolCall(ts = "t1", payload = deviceToolCall()))
            emit(SessionEvent.StreamEnd)
        }

        val live = buildMulticastLiveFlow("s1", upstream, cache, shareScope(), executor)
        live.take(1).toList() // the only client renders the first event, then goes away
        advanceUntilIdle()

        assertEquals(1, handler.callCount)
        assertEquals("ok", reporter.reports.single().request.status)
    }

    // ---- errorFor: the 410 session_terminated envelope parse ----
    //
    // errorFor is the ONE seam where both send routes read a non-2xx body; like buildMulticastLiveFlow
    // it's a top-level function so these drive it with a synthetic Response.error(...) rather than a
    // full RunRepository (whose SSE collaborators aren't JVM-constructible). This is the regression
    // guard for the bug: send()/sendQuery() used to `throw HttpException(response)` and discard the
    // structured envelope, so a permanently-terminated session read as a generic retryable error.

    private val json = Json { ignoreUnknownKeys = true }

    private fun errorResponse(code: Int, body: String): Response<Unit> =
        Response.error(code, body.toResponseBody("application/json".toMediaType()))

    @Test
    fun `errorFor maps a 410 session_terminated envelope to SessionTerminatedException`() {
        val body =
            """{"error":{"code":"session_terminated","reason":"Session is permanently terminated","retryable":false}}"""
        val thrown = errorFor(json, errorResponse(410, body))

        assertTrue("expected SessionTerminatedException, got $thrown", thrown is SessionTerminatedException)
        assertEquals("Session is permanently terminated", (thrown as SessionTerminatedException).reason)
    }

    @Test
    fun `errorFor defaults the reason when the terminated envelope omits one`() {
        val thrown = errorFor(json, errorResponse(410, """{"error":{"code":"session_terminated"}}"""))

        assertEquals("This session is permanently terminated", (thrown as SessionTerminatedException).reason)
    }

    @Test
    fun `errorFor falls back to HttpException for a non-terminated error envelope`() {
        val body = """{"error":{"code":"not_found","reason":"session xyz not found","retryable":false}}"""

        assertTrue(errorFor(json, errorResponse(404, body)) is HttpException)
    }

    @Test
    fun `errorFor falls back to HttpException when the body is not the envelope shape at all`() {
        // A non-JSON / non-envelope body must never throw out of the PARSE - it degrades to the
        // generic HttpException, exactly the earlier behaviour for every ordinary transport error.
        assertTrue(errorFor(json, errorResponse(500, "upstream exploded, not json")) is HttpException)
    }

    // ---- interpretAnswerResponse: the ask-user answer-POST status mapping (top-level, like errorFor) ----
    //
    // The card's authoritative settle is the user_question_answered SSE event, so this only steers the
    // answer UX: 200 accepted, 404/409 already-resolved-elsewhere (settle silently, NOT an error), every
    // other non-2xx a genuine failure the card re-enables + toasts for.

    private fun okResponse(): Response<okhttp3.ResponseBody> =
        Response.success("""{"resolved":true}""".toResponseBody("application/json".toMediaType()))

    private fun answerError(code: Int): Response<okhttp3.ResponseBody> =
        Response.error(code, """{"message":"nope"}""".toResponseBody("application/json".toMediaType()))

    @Test
    fun `interpretAnswerResponse maps 200 to Resolved`() {
        assertEquals(QuestionAnswerResult.Resolved, interpretAnswerResponse(okResponse()))
    }

    @Test
    fun `interpretAnswerResponse maps 404 and 409 to AlreadyResolved`() {
        // 404 (unknown/superseded/already-read) and 409 (already delivered) both mean the question is
        // resolved elsewhere — the answered event settles the card, so this is not an error.
        assertEquals(QuestionAnswerResult.AlreadyResolved, interpretAnswerResponse(answerError(404)))
        assertEquals(QuestionAnswerResult.AlreadyResolved, interpretAnswerResponse(answerError(409)))
    }

    @Test
    fun `interpretAnswerResponse maps 403 422 and 410 to Failed`() {
        // 403 bad token / 422 arity / 410 terminated — genuine failures; the card stays interactive.
        assertEquals(QuestionAnswerResult.Failed, interpretAnswerResponse(answerError(403)))
        assertEquals(QuestionAnswerResult.Failed, interpretAnswerResponse(answerError(422)))
        assertEquals(QuestionAnswerResult.Failed, interpretAnswerResponse(answerError(410)))
    }

    // ---- retryFrom: the retry/rewind mutation, driven through a REAL RunRepository ----
    //
    // Unlike the tests above (which exercise buildMulticastLiveFlow/errorFor as top-level
    // functions), retryFrom is a genuine method on the class, so a real RunRepository is
    // constructed here. Only `api` needs scripted behavior; the SSE/device-tool collaborators are
    // untouched by this method and get the cheapest real/mock stand-ins available - no new fake
    // classes: SessionStreamClient is a plain mock (mockito-core's inline mock maker handles the
    // concrete class fine, same as StagedAttachmentsReducerTest's Uri mock), and DeviceToolCatalog
    // is a REAL instance built the same way DeviceToolCatalogTest does.

    @Test
    fun `retryFrom returns RunStarted on a 202`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.recoverSession("s1", RecoverSessionRequest(action = "retry", fromTs = "t0", model = "m1")))
            .thenReturn(Response.success(202, RecoverSessionResponseDto(sessionId = "s1", accepted = true, runId = "run-9")))

        val result = runRepository(api).retryFrom("s1", fromTs = "t0", model = "m1")

        assertTrue(result is SendResult.RunStarted)
        assertEquals("run-9", (result as SendResult.RunStarted).runId)
    }

    @Test
    fun `retryFrom falls back to the session id when the 202 body omits run_id`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.recoverSession("s1", RecoverSessionRequest(action = "retry", fromTs = "t0", model = null)))
            .thenReturn(Response.success(202, RecoverSessionResponseDto(sessionId = "s1", accepted = true)))

        val result = runRepository(api).retryFrom("s1", fromTs = "t0", model = null) as SendResult.RunStarted

        assertEquals("s1", result.runId)
    }

    @Test
    fun `retryFrom throws SessionTerminatedException on a 410 session_terminated envelope`() = runTest {
        val api = mock(AuraApi::class.java)
        val body =
            """{"error":{"code":"session_terminated","reason":"Session is permanently terminated","retryable":false}}"""
        `when`(api.recoverSession("s1", RecoverSessionRequest(action = "retry", fromTs = "t0", model = null)))
            .thenReturn(Response.error(410, body.toResponseBody("application/json".toMediaType())))
        val repo = runRepository(api)

        try {
            repo.retryFrom("s1", fromTs = "t0", model = null)
            throw AssertionError("expected SessionTerminatedException")
        } catch (e: SessionTerminatedException) {
            assertEquals("Session is permanently terminated", e.reason)
        }
    }

    private fun TestScope.runRepository(api: AuraApi): RunRepository = RunRepository(
        api = api,
        streamClient = mock(SessionStreamClient::class.java),
        deviceToolCatalog = DeviceToolCatalog(DevicePermissionChecker { true }, DeviceToolGate { emptySet() }),
        deviceToolDispatch = noDispatch,
        json = json,
        runNotifications = RunNotifications { _, _ -> },
        scope = shareScope(),
    )

    /** The `shareIn` scope: an INDEPENDENT scope on the test's own scheduler, per the house idiom in
     * `app/src/test/java/com/mewbo/aura/CLAUDE.md` - `advanceUntilIdle()` drives it (unlike
     * `backgroundScope` in this project's pinned coroutines-test version), and `runTest`'s leak
     * check ignores it (unlike the `TestScope` itself), which matters because a `shareIn` sharing
     * coroutine never completes on its own. */
    private fun TestScope.shareScope(): CoroutineScope =
        CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob())

    /** A REAL executor with only its two I/O boundaries faked (the result POST, and the handler's
     * device access). Unconfined, matching `DeviceToolExecutorTest`'s house idiom: each dispatched
     * call then runs eagerly rather than needing its own scheduler dance. */
    private fun executor(reporter: RecordingReporter, handler: DeviceToolHandler) = DeviceToolExecutor(
        resultReporter = reporter,
        callLedger = FakeLedger(),
        clock = DeviceClock { 0.0 },
        gate = DeviceToolGate { emptySet() },
        handlers = listOf(handler),
        scope = CoroutineScope(Dispatchers.Unconfined),
    )

    private fun deviceToolCall(callId: String = "call-1") = DeviceToolCallPayload(
        callId = callId,
        callToken = "tok-1",
        toolId = "device_get_time",
        args = buildJsonObject {},
        expiresAt = 100.0,
    )

    private class RecordingReporter : DeviceToolResultReporter {
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

    private class FakeHandler(override val toolId: String, private val result: JsonObject) : DeviceToolHandler {
        var callCount = 0
            private set

        override suspend fun execute(args: JsonObject): JsonObject {
            callCount++
            return result
        }
    }
}
