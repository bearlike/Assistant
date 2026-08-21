package com.mewbo.aura.ui.chat

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.SendMessageRequest
import com.mewbo.aura.data.api.SendMessageResponseDto
import com.mewbo.aura.data.api.SessionCreateRequest
import com.mewbo.aura.data.api.SessionCreateResponseDto
import com.mewbo.aura.data.api.SessionEventsResponseDto
import com.mewbo.aura.data.api.SessionInterruptResponseDto
import com.mewbo.aura.data.api.SessionQueryRequest
import com.mewbo.aura.data.api.SessionQueryResponseDto
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.DeviceShape
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.device.DeviceToolDispatch
import com.mewbo.aura.data.device.DeviceToolGate
import com.mewbo.aura.data.device.shizuku.DeviceControlGate
import com.mewbo.aura.data.model.AgentMessageDeltaPayload
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.CompletionPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TextPayload
import com.mewbo.aura.data.repo.AttachmentRepository
import com.mewbo.aura.data.repo.ModelRepository
import com.mewbo.aura.data.repo.RunNotifications
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.SessionRepository
import com.mewbo.aura.data.repo.SessionScopeRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.data.sse.SessionStreamClient
import com.mewbo.aura.voice.NoOpAuraHaptics
import com.mewbo.aura.voice.SynthEvent
import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import com.mewbo.aura.voice.TranscriberError
import com.mewbo.aura.voice.TranscriberEvent
import java.time.Instant
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.mockito.Mockito.doAnswer
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`
import retrofit2.Response

/**
 * The four [ChatViewModel] behaviours a user would notice going wrong, and that no existing suite can
 * witness. The pure helpers this class delegates to already have their own tests
 * ([SessionBindingTest], [SendDecisionTest], [ChatCompletionPhaseTest], [ChatWidgetGateTest]) — each
 * pins a DECISION in isolation, and every defect below lives in the WIRING around those decisions
 * instead, where an isolated predicate test is structurally incapable of seeing it.
 *
 * Nothing here reaches a model: [AuraApi] is faked and the live stream is a plain
 * `MutableSharedFlow<SessionEvent>` handed to a mocked [SessionStreamClient], the same seam
 * `RunRepositoryTest`/`DeviceControlHoldTest` drive. `viewModelScope` dispatches on
 * `Dispatchers.Main`, so the house ViewModel idiom applies verbatim — `setMain(StandardTestDispatcher)`
 * plus `runTest(dispatcher)` on the one shared scheduler (`SessionsViewModelTest` is the reference;
 * `machineScope()` is for a class with an infinite `init` collector, which this is not).
 *
 * **Mockito appears three times and only where a hand-rolled double is impossible.**
 * [SettingsStore] (Context + DataStore), [AttachmentRepository] (Context) and [SessionStreamClient]
 * (OkHttp `EventSource.Factory`) cannot be constructed on a plain JVM, and [DeviceControlSession] is
 * a final class with no interface to implement — every other collaborator here is the REAL class or a
 * hand-written fake.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModelTest {

    private val dispatcher = StandardTestDispatcher()
    private val json = Json { ignoreUnknownKeys = true }

    /**
     * The ONE ordering witness. Every fake that takes part in [ChatViewModel.stop] appends to this
     * same list, because two independent booleans can say that both effects happened and still not
     * say which came first — and "which came first" is the whole property (test 1).
     */
    private val order = mutableListOf<String>()

    private val api = FakeAuraApi(order)
    private val streamClient = mock(SessionStreamClient::class.java)

    /** Grant released ⇒ `order` records it. See the class KDoc for why this one is a mock. */
    private val grant: DeviceControlSession = mock(DeviceControlSession::class.java).also {
        `when`(it.changes).thenReturn(emptyFlow())
        doAnswer { _ -> order += RELEASED_GRANT; true }.`when`(it).stop()
    }

    @Before fun setUp() = Dispatchers.setMain(dispatcher)

    @After fun tearDown() = Dispatchers.resetMain()

    // ---- 1. Stop releases the device-control grant first, and unconditionally ----
    //
    // ChatViewModel.stop() releases the grant OUTSIDE its `if (sessionId != null)` guard and BEFORE
    // the interrupt POST, and both are load-bearing rather than incidental ordering. Moving the
    // release inside the guard is the obvious tidy-up and leaves an agent driving the phone after
    // the user tapped Stop on a chat that has not created its session yet; moving it after the call
    // makes the release depend on the network, which is exactly what a stop must not do.

    @Test
    fun `stop releases the device-control grant BEFORE it asks the server to interrupt`() = runTest(dispatcher) {
        api.history("s1", running = false)
        val vm = viewModel()
        vm.bind("s1")
        advanceUntilIdle()
        order.clear()

        vm.stop()
        advanceUntilIdle()

        // Not two booleans: the sequence. A release fired from inside the interrupt's own coroutine
        // would still set both flags, and would still leave the phone drivable until the socket
        // answered.
        assertEquals(listOf(RELEASED_GRANT, "interrupt s1"), order)
    }

    @Test
    fun `stop releases the grant on a fresh chat that has no session bound yet`() = runTest(dispatcher) {
        val vm = viewModel()
        vm.bind(null)
        advanceUntilIdle()
        order.clear()

        vm.stop()
        advanceUntilIdle()

        // The release is the ONLY effect available here — there is no run id to interrupt — so it is
        // also the only thing standing between a tap on Stop and an agent that keeps driving.
        assertEquals(listOf(RELEASED_GRANT), order)
    }

    @Test
    fun `a failed interrupt is never surfaced to the user`() = runTest(dispatcher) {
        // Reporting the failure would imply that its success meant the run had stopped, which is the
        // one thing the endpoint measurably does not promise. What IS true after stop() is true on
        // both paths, so neither path may paint an error.
        api.history("s1", running = false)
        api.interruptFailure = IllegalStateException("socket closed")
        val vm = viewModel()
        vm.bind("s1")
        advanceUntilIdle()

        vm.stop()
        advanceUntilIdle()

        val state = vm.state.value
        assertTrue("a failed interrupt painted an error card: ${state.items}", state.items.none { it is ChatItem.ErrorCard })
        assertEquals(RunPhase.Idle, state.runPhase)
        assertFalse(state.sessionEnded)
        // The call was made and it failed; the grant is gone either way.
        assertEquals(listOf(RELEASED_GRANT, "interrupt s1"), order)
    }

    // ---- 2. A session switch never folds the session being LEFT into the one binding next ----

    /**
     * One [ChatViewModel] outlives every session id, so its reducer, title and `sessionEnded` are
     * shared mutable state a load started for session A can still write into after the user has
     * opened session B. [SessionBindingTest] pins the pure `rebindTo` predicate and structurally
     * cannot witness this: the corruption is in a coroutine that resumes AFTER the predicate has
     * already answered correctly.
     *
     * The stall resumes through a NON-cancellable `suspendCoroutine` deliberately. A cancellable one
     * would let `historyJob.cancel()` alone win, and the test would pass without ever exercising the
     * `binding.isCurrent(id)` re-check that `loadHistoryAndFollow` documents as the actual defence —
     * cancellation is cooperative, and the fetch is the last suspension point before a long
     * non-suspending run of state mutation.
     */
    @Test
    fun `a history load for the session being left never folds into the session bound next`() = runTest(dispatcher) {
        api.history("A", running = false, title = "Session A", events = listOf(userEvent("alpha from A", TS_A)))
        api.history("B", running = false, title = "Session B", events = listOf(userEvent("bravo from B", TS_B)))
        api.stall("A")
        val vm = viewModel()

        vm.bind("A")
        advanceUntilIdle() // A's fetch is now suspended mid-load.

        vm.bind("B")
        advanceUntilIdle() // B loads to completion while A's load is still in flight.

        api.release("A")
        advanceUntilIdle() // A's load resumes into a binding that is no longer its own.

        val state = vm.state.value
        assertEquals("B", state.sessionId)
        assertEquals(listOf("bravo from B"), state.items.filterIsInstance<ChatItem.UserBubble>().map { it.text })
        assertEquals("Session B", state.title)
    }

    // ---- 3. A 409 re-routed onto the steer path re-subscribes to the live run ----

    /**
     * `RunRepository.sendQuery`'s `409 ⇒ Enqueued` mapping is already pinned by `RunRepositoryTest`.
     * What is not pinned anywhere is this side of it: the re-subscribe IS the recovery. A `409` means
     * the client's `RunPhase` was stale — the run never ended — so after the message is queued the
     * user must be put back on the run they just steered, or their follow-up lands into a transcript
     * that never moves again.
     */
    @Test
    fun `a 409-rerouted send re-subscribes to the live run`() = runTest(dispatcher) {
        api.history("s1", running = false)
        val upstream = upstream("s1")
        api.queryResponse = conflict()
        api.sendMessageResponse = Response.success(202, SendMessageResponseDto(sessionId = "s1", enqueued = true))
        val vm = viewModel()
        vm.bind("s1")
        advanceUntilIdle()

        vm.send("follow-up")
        advanceUntilIdle()

        assertEquals(RunPhase.Streaming, vm.state.value.runPhase)
        // The phase alone would also be set by a re-subscribe that attached to nothing. An event
        // arriving on the wire and landing in the transcript is what proves a live collector.
        upstream.emit(SessionEvent.Assistant(ts = TS_C, payload = TextPayload("back on the run")))
        advanceUntilIdle()

        assertEquals(
            listOf("back on the run"),
            vm.state.value.items.filterIsInstance<ChatItem.AssistantMessage>().map { it.text },
        )
    }

    @Test
    fun `a steer that never reaches the backend clears its own optimistic bubble`() = runTest(dispatcher) {
        // The pending echo is only ever cleared by a real server `user`/`user_steer` event dedupe-
        // matching it, and a send that failed will never produce one — so without the explicit
        // re-fold the bubble is stuck at 70% opacity for the rest of the session.
        api.history("s1", running = true)
        upstream("s1")
        api.sendMessageFailure = IllegalStateException("connection reset")
        val vm = viewModel()
        vm.bind("s1")
        advanceUntilIdle()
        assertEquals(RunPhase.Streaming, vm.state.value.runPhase)

        vm.send("steered message")
        // The optimistic fold is synchronous inside send(); the network call is not.
        assertEquals(listOf(true), pendingFlags(vm, "steered message"))

        advanceUntilIdle()

        assertEquals(listOf(false), pendingFlags(vm, "steered message"))
    }

    // ---- 4. Completion phase and the finalized reply land in ONE emission ----

    /**
     * A `completion` fold flips the just-finalized [ChatItem.AssistantMessage.isStreaming] to false,
     * so the transcript renders a settled reply. If `runPhase` leaves `Streaming` in a LATER
     * emission, the thinking spark (`showThinking = isRunLive`) flashes on for one frame underneath
     * an already-finished answer. The frames are recorded on an independent unconfined scope, which
     * observes each distinct state rather than only the value that survives conflation.
     */
    @Test
    fun `a failed speech service tells the user instead of losing the recording silently`() = runTest(dispatcher) {
        // The user held the mic, spoke, and the gateway refused. `DictationDecision` maps every
        // error to Idle, so without the notice the composer just returns to rest and the whole
        // utterance vanishes with nothing on screen to explain it — and the cause is a Settings
        // choice the composer cannot show. Today this is EVERY server-STT call: the gateway's
        // transcription credential is dead while `capabilities` still reports it available.
        val notices = mutableListOf<String>()
        val vm = viewModel(transcriber = FailingTranscriber(TranscriberError.ServiceFailed))
        vm.bind(null)
        advanceUntilIdle()

        vm.startDictation { notices += it }
        advanceUntilIdle()

        assertEquals(1, notices.size)
        assertTrue("the notice must point at the remedy, got ${notices.single()}", notices.single().contains("Settings"))
        // The mic is not wedged and not disabled: the capture ended, and on-device dictation is
        // still one Settings row away.
        assertEquals(DictationState.Idle, vm.state.value.dictation)
        assertTrue("the mic must stay tappable — the SERVICE failed, not the device", vm.state.value.dictationAvailable)
    }

    @Test
    fun `an ordinary recognizer error stays silent and disables nothing`() = runTest(dispatcher) {
        // The other side of the split: saying nothing must not raise a notice.
        val notices = mutableListOf<String>()
        val vm = viewModel(transcriber = FailingTranscriber(TranscriberError.NoMatch))
        vm.bind(null)
        advanceUntilIdle()

        vm.startDictation { notices += it }
        advanceUntilIdle()

        assertTrue("NoMatch is a quiet cancel, got $notices", notices.isEmpty())
        assertEquals(DictationState.Idle, vm.state.value.dictation)
        assertTrue(vm.state.value.dictationAvailable)
    }

    @Test
    fun `a device with no recognizer disables the mic, and says nothing`() = runTest(dispatcher) {
        val notices = mutableListOf<String>()
        val vm = viewModel(transcriber = FailingTranscriber(TranscriberError.Unavailable))
        vm.bind(null)
        advanceUntilIdle()

        vm.startDictation { notices += it }
        advanceUntilIdle()

        assertTrue(notices.isEmpty())
        assertFalse("Unavailable means this device cannot dictate at all", vm.state.value.dictationAvailable)
    }

    @Test
    fun `a completed turn never renders a settled reply while the run still reads as live`() = runTest(dispatcher) {
        api.createdSessionId = "s-new"
        val upstream = upstream("s-new")
        api.queryResponse = Response.success(202, SessionQueryResponseDto(sessionId = "s-new", accepted = true))
        val vm = viewModel()
        vm.bind(null)
        advanceUntilIdle()

        val frames = mutableListOf<Frame>()
        val observer = observe(vm, frames)

        vm.send("hello")
        // The user's own bubble is on screen before any network call resolves.
        assertEquals(listOf("hello"), vm.state.value.items.filterIsInstance<ChatItem.UserBubble>().map { it.text })
        assertEquals(RunPhase.Sending, vm.state.value.runPhase)

        advanceUntilIdle()
        assertEquals(RunPhase.Streaming, vm.state.value.runPhase)

        upstream.emit(SessionEvent.AgentMessageDelta(ts = TS_C, payload = AgentMessageDeltaPayload("Half a repl", "root")))
        advanceUntilIdle()
        upstream.emit(SessionEvent.AgentMessageDelta(ts = TS_C, payload = AgentMessageDeltaPayload("y.", "root")))
        upstream.emit(SessionEvent.Completion(ts = TS_D, payload = CompletionPayload(done = true)))
        upstream.emit(SessionEvent.StreamEnd)
        advanceUntilIdle()

        val reply = vm.state.value.items.filterIsInstance<ChatItem.AssistantMessage>().single()
        assertEquals("Half a reply.", reply.text)
        assertFalse("the reply is still marked streaming after completion", reply.isStreaming)
        assertEquals(RunPhase.Done, vm.state.value.runPhase)

        // The observer genuinely sees intermediate states, so the absence below is a real absence.
        assertTrue("expected to observe the live phases, saw ${frames.map { it.phase }}", frames.any { it.phase == RunPhase.Streaming })
        val split = frames.firstOrNull { frame ->
            frame.phase == RunPhase.Streaming && frame.items.filterIsInstance<ChatItem.AssistantMessage>().any { !it.isStreaming }
        }
        assertNull("a settled reply was rendered while runPhase still read Streaming: $split", split)
        observer.cancel()
    }

    // ---- 5. A television reads a TYPED reply aloud; a handheld still does not ----
    //
    // The decision itself is pinned in SpeechControllerTest. What only this suite can witness is the
    // WIRING: ChatViewModel constructs the controller, and a shape that never reaches it leaves the
    // whole feature inert with every unit test still green.

    @Test
    fun `a typed turn on a television is read aloud as it streams`() = runTest(dispatcher) {
        val synth = RecordingSynthesizer()
        api.history("s1", running = false)
        val upstream = upstream("s1")
        val vm = viewModel(synthesizer = synth, deviceShape = DeviceShape.Television)
        vm.bind("s1")
        advanceUntilIdle()

        // No modality argument: this is the ordinary typed send every surface on a television makes,
        // since the assistant role - the only voice entry point - is unreachable there.
        vm.send("what is on tonight")
        advanceUntilIdle()

        upstream.emit(SessionEvent.AgentMessageDelta(ts = TS_C, payload = AgentMessageDeltaPayload("The film starts at eight. ", "root")))
        advanceUntilIdle()
        upstream.emit(SessionEvent.AgentMessageDelta(ts = TS_C, payload = AgentMessageDeltaPayload("Channel four.", "root")))
        upstream.emit(SessionEvent.Completion(ts = TS_D, payload = CompletionPayload(done = true)))
        upstream.emit(SessionEvent.StreamEnd)
        advanceUntilIdle()

        // Each sentence exactly once, in order, with nothing re-spoken across the folds that
        // carried the first sentence forward.
        assertEquals(listOf("The film starts at eight.", "Channel four."), synth.spoken)
    }

    @Test
    fun `the same typed turn on a handheld stays silent`() = runTest(dispatcher) {
        val synth = RecordingSynthesizer()
        api.history("s1", running = false)
        val upstream = upstream("s1")
        val vm = viewModel(synthesizer = synth, deviceShape = DeviceShape.Handheld)
        vm.bind("s1")
        advanceUntilIdle()

        vm.send("what is on tonight")
        advanceUntilIdle()

        upstream.emit(SessionEvent.AgentMessageDelta(ts = TS_C, payload = AgentMessageDeltaPayload("The film starts at eight.", "root")))
        upstream.emit(SessionEvent.Completion(ts = TS_D, payload = CompletionPayload(done = true)))
        upstream.emit(SessionEvent.StreamEnd)
        advanceUntilIdle()

        // Speech on a phone is something the user asked for by speaking. The reply is on screen -
        // proof the turn ran, so the silence is the gate rather than a turn that never happened.
        assertTrue("a handheld spoke a typed reply: ${synth.spoken}", synth.spoken.isEmpty())
        assertEquals(
            listOf("The film starts at eight."),
            vm.state.value.items.filterIsInstance<ChatItem.AssistantMessage>().map { it.text },
        )
    }

    // ---- fixtures ----

    private data class Frame(val phase: RunPhase, val items: List<ChatItem>)

    /**
     * Records every distinct [ChatUiState] the surface would render. An INDEPENDENT scope on the
     * test's own scheduler, per the house idiom: `runTest`'s leak check ignores it (a `StateFlow`
     * collect never returns), while `advanceUntilIdle()` still drives it. Unconfined so the collector
     * resumes inline with each `_state.update`, instead of conflating a split emission back into the
     * single one this test exists to distinguish it from.
     */
    private fun TestScope.observe(vm: ChatViewModel, into: MutableList<Frame>): CoroutineScope {
        val scope = CoroutineScope(UnconfinedTestDispatcher(testScheduler) + SupervisorJob())
        scope.launch { vm.state.collect { into += Frame(it.runPhase, it.items) } }
        return scope
    }

    private fun pendingFlags(vm: ChatViewModel, text: String): List<Boolean> =
        vm.state.value.items.filterIsInstance<ChatItem.UserBubble>().filter { it.text == text }.map { it.pending }

    /** Registers the live stream for [sessionId] and hands back the upstream the test writes to. */
    private fun upstream(sessionId: String): MutableSharedFlow<SessionEvent> {
        val flow = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 16)
        `when`(streamClient.stream(sessionId)).thenReturn(flow)
        return flow
    }

    private fun conflict(): Response<SessionQueryResponseDto> = Response.error(
        409,
        """{"message": "Session is already running."}"""
            .toResponseBody("application/json".toMediaType()),
    )

    private fun TestScope.viewModel(
        transcriber: Transcriber = SilentTranscriber(),
        synthesizer: Synthesizer = SilentSynthesizer(),
        deviceShape: DeviceShape = DeviceShape.Handheld,
    ): ChatViewModel = ChatViewModel(
        sessionRepository = SessionRepository(api, json),
        runRepository = RunRepository(
            api = api,
            streamClient = streamClient,
            deviceToolCatalog = catalog(),
            deviceControlSession = grant,
            deviceToolDispatch = DeviceToolDispatch { _, _ -> },
            json = json,
            runNotifications = RunNotifications { _, _, _ -> },
            scope = shareScope(),
        ),
        synthesizer = synthesizer,
        transcriber = transcriber,
        haptics = NoOpAuraHaptics,
        modelRepository = ModelRepository(api),
        settingsStore = settingsStore(),
        sessionScopeRepository = SessionScopeRepository(api, catalog(), grant),
        attachmentRepository = mock(AttachmentRepository::class.java),
        deviceControlSession = grant,
        deviceShape = deviceShape,
    )

    /** Shizuku absent, matching `RunRepositoryTest`: no run here takes or needs a real grant. */
    private fun catalog() = DeviceToolCatalog(
        DevicePermissionChecker { true },
        DeviceToolGate { emptySet() },
        DeviceControlGate { false },
    )

    /** `shareIn`'s scope, per the house idiom — driven by `advanceUntilIdle()`, ignored by the leak
     * check, which a sharing coroutine (it never completes on its own) needs. */
    private fun TestScope.shareScope(): CoroutineScope =
        CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob())

    /** DataStore-backed, so it cannot be constructed here; only the five flows `init` collects
     * matter, and each completes so no collector outlives the test. */
    private fun settingsStore(): SettingsStore = mock(SettingsStore::class.java).also {
        `when`(it.baseUrl).thenReturn(flowOf("http://backend.example.com"))
        `when`(it.displayName).thenReturn(flowOf(""))
        `when`(it.selectedModel).thenReturn(flowOf(""))
        `when`(it.streamlitWidgetsEnabled).thenReturn(flowOf(true))
        `when`(it.selectedProject).thenReturn(flowOf(""))
        `when`(it.speakResponses).thenReturn(flowOf(true))
    }

    /**
     * Backend-shaped `user` frame. Built as raw JSON and decoded through the REAL
     * [SessionEvent.decode] the live wire uses (`SessionRepository.fetchHistory`), so a typo'd type
     * or `@SerialName` degrades to `Unknown` and fails an assertion rather than passing a fixture
     * against itself. The numeric `+00:00` offset is what the backend actually sends — a bare `Z`
     * would mask the parser's real path.
     */
    private fun userEvent(text: String, ts: String): JsonElement = buildJsonObject {
        put("type", "user")
        put("ts", ts)
        putJsonObject("payload") { put("text", text) }
    }

    /**
     * [AuraApi] has ~40 endpoints and this suite scripts six of them, so the rest are delegated to a
     * mock: an unscripted call then fails loudly instead of needing forty hand-written stubs.
     */
    private class FakeAuraApi(
        private val order: MutableList<String>,
        private val delegate: AuraApi = mock(AuraApi::class.java),
    ) : AuraApi by delegate {

        private val histories = mutableMapOf<String, SessionEventsResponseDto>()
        private val stalled = mutableSetOf<String>()
        private val waiting = mutableMapOf<String, Continuation<Unit>>()

        var createdSessionId: String = "s-created"
        var queryResponse: Response<SessionQueryResponseDto> =
            Response.success(202, SessionQueryResponseDto(sessionId = "s-created", accepted = true))
        var sendMessageResponse: Response<SendMessageResponseDto> =
            Response.success(202, SendMessageResponseDto(sessionId = "s-created", enqueued = true))
        var sendMessageFailure: Throwable? = null
        var interruptFailure: Throwable? = null

        fun history(
            sessionId: String,
            running: Boolean,
            title: String? = null,
            events: List<JsonElement> = emptyList(),
        ) {
            histories[sessionId] =
                SessionEventsResponseDto(sessionId = sessionId, events = events, running = running, title = title)
        }

        /** The NEXT history fetch for [sessionId] suspends until [release]. */
        fun stall(sessionId: String) {
            stalled += sessionId
        }

        fun release(sessionId: String) {
            waiting.remove(sessionId)?.resume(Unit)
        }

        override suspend fun getEvents(sessionId: String, after: String?): SessionEventsResponseDto {
            if (stalled.remove(sessionId)) {
                // Non-cancellable on purpose — see the rebinding test's own KDoc.
                suspendCoroutine { continuation -> waiting[sessionId] = continuation }
            }
            return histories[sessionId] ?: SessionEventsResponseDto(sessionId = sessionId)
        }

        override suspend fun createSession(request: SessionCreateRequest): SessionCreateResponseDto =
            SessionCreateResponseDto(sessionId = createdSessionId)

        override suspend fun query(sessionId: String, request: SessionQueryRequest): Response<SessionQueryResponseDto> {
            order += "query $sessionId"
            return queryResponse
        }

        override suspend fun sendMessage(sessionId: String, request: SendMessageRequest): Response<SendMessageResponseDto> {
            order += "message $sessionId"
            sendMessageFailure?.let { throw it }
            return sendMessageResponse
        }

        override suspend fun interruptSession(sessionId: String): Response<SessionInterruptResponseDto> {
            order += "interrupt $sessionId"
            interruptFailure?.let { throw it }
            return Response.success(202, SessionInterruptResponseDto(sessionId = sessionId, interrupted = true))
        }

        /** The catalogs degrade to "couldn't load" rather than being scripted — no assertion here
         * reads them, and a silent failure is exactly what the production path does with them. */
        override suspend fun getModels(): Nothing = throw IllegalStateException("no model catalog in this test")

        override suspend fun getProjects(): Nothing = throw IllegalStateException("no project catalog in this test")

        override suspend fun getTools(project: String?): Nothing = throw IllegalStateException("no tool catalog in this test")
    }

    private class SilentSynthesizer : Synthesizer {
        override val isAvailable = MutableStateFlow(true)

        override fun speak(utteranceId: String, text: String) = Unit

        override fun stop() = Unit

        override fun events(): Flow<SynthEvent> = emptyFlow()
    }

    /** Records what actually reached the synthesizer - the only evidence that a reply was READ
     * ALOUD rather than merely rendered. */
    private class RecordingSynthesizer : Synthesizer {
        val spoken = mutableListOf<String>()
        override val isAvailable = MutableStateFlow(true)

        override fun speak(utteranceId: String, text: String) {
            spoken += text
        }

        override fun stop() = Unit

        override fun events(): Flow<SynthEvent> = emptyFlow()
    }

    private class SilentTranscriber : Transcriber {
        override fun listen(): Flow<TranscriberEvent> = emptyFlow()
    }

    /** Ends a capture the way a server-backed engine does when the gateway refuses: a Ready, then
     * an error, and no transcript. */
    private class FailingTranscriber(private val code: TranscriberError) : Transcriber {
        override fun listen(): Flow<TranscriberEvent> =
            flowOf(TranscriberEvent.Ready, TranscriberEvent.Error(code))
    }

    private companion object {
        const val RELEASED_GRANT = "released device-control grant"

        /** Backend timestamps carry a NUMERIC offset, never a bare `Z` (`test/CLAUDE.md`). */
        val TS_A: String = stamp(-40)
        val TS_B: String = stamp(-30)
        val TS_C: String = stamp(-20)
        val TS_D: String = stamp(-10)

        private fun stamp(secondsAgo: Long): String =
            Instant.now().plusSeconds(secondsAgo).toString().removeSuffix("Z") + "+00:00"
    }
}
