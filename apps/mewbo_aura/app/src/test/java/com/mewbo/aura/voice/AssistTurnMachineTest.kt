package com.mewbo.aura.voice

import com.mewbo.aura.data.model.AgentMessageDeltaPayload
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.CompletionPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.data.model.TextPayload
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit-tests [AssistTurnMachine] against scripted [Transcriber]/[Synthesizer]/`liveEvents` test
 * doubles and plain-lambda repo/handoff stubs (the constructor's narrow call-shape seam - see the
 * class KDoc for why that shape exists instead of the concrete `SessionRepository`/`RunRepository`
 * types). Virtual time via `runTest`/`advanceTimeBy` makes the 8s silence timeout AND the scripted
 * live-event timelines deterministic and fast.
 *
 * v5 contract: the FIRST turn of an invocation (voice or text) streams its response
 * in-overlay ([AssistUiState.Streaming], folding `liveEvents` through [com.mewbo.aura.data.model.TranscriptReducer])
 * - no handoff fires until either a SECOND turn is sent or [AssistTurnMachine.expand] is tapped.
 * [dismiss] resets the machine's own turn/session bookkeeping so a REUSED instance (the real
 * `VoiceInteractionSession` survives hide->show - voice/CLAUDE.md) starts its next invocation clean.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class AssistTurnMachineTest {

    /** The machine's scope for every test: an INDEPENDENT scope on the test's own scheduler.
     * `backgroundScope` is the obvious candidate but empirically does NOT execute its queued
     * coroutines under `advanceUntilIdle()` in this project's kotlinx-coroutines-test version
     * (probed directly - a bare `backgroundScope.launch { }` never runs), which starves every
     * machine job. Plain `this` (the TestScope) runs fine but trips `UncompletedCoroutinesError`
     * at cleanup: the machine's `init` block launches an infinite `speakingKey` collector that
     * outlives every test body by design (production parity - `AuraSession`'s scope lives for the
     * session). An own-`SupervisorJob` scope on the SHARED `testScheduler` gets both halves right:
     * `advanceUntilIdle()` drives it (same scheduler), and `runTest`'s leak check ignores it (not a
     * child of the test job). Per-test instances never cross-talk; the JVM tears them down. */
    private fun TestScope.machineScope(): CoroutineScope =
        CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob())

    /** Emits one script per `listen()` call on a `delay`-gated cold flow - the same shape as the
     * debug `FakeTranscriber`, kept local to the test so each scenario controls its own exact
     * timeline. ONE SCRIPT = ONE CAPTURE SESSION, mirroring the real `SpeechRecognizer`: its event
     * stream ENDS after a final result, and the machine deliberately leaves a finalized collector
     * draining rather than cancelling it - so a shared replay-from-zero script (the original shape)
     * had its "second turn" events silently swallowed by the FIRST turn's still-draining collector,
     * and a shared consumption cursor (the first fix attempt) had them consumed-and-discarded the
     * same way. Per-call scripts make each `startListening()` a genuinely fresh recognizer session. */
    private class ScriptedTranscriber(private vararg val scripts: List<Pair<Long, TranscriberEvent>>) : Transcriber {
        private var nextScript = 0
        override fun listen(): Flow<TranscriberEvent> = flow {
            val script = scripts.getOrNull(nextScript++) ?: return@flow
            for ((delayMs, event) in script) {
                delay(delayMs)
                emit(event)
            }
        }
    }

    /** [AssistTurnMachine.subscribeLive]'s analog of [ScriptedTranscriber] - a delay-gated
     * `liveEvents` stub letting a test pause virtual time MID-STREAM (e.g. to call
     * [AssistTurnMachine.stopStreaming]/[AssistTurnMachine.dismiss] before a later scripted event
     * "arrives") and assert it never folds in once the collecting job is genuinely cancelled. */
    private fun delayedEvents(vararg script: Pair<Long, SessionEvent>): (String) -> Flow<SessionEvent> =
        { _ ->
            flow {
                for ((delayMs, event) in script) {
                    delay(delayMs)
                    emit(event)
                }
            }
        }

    private class RecordingSynthesizer : Synthesizer {
        var stopCount = 0

        /** Utterance TEXT (post markdown-strip/chunk) of every [speak] call, in order - the
         * speak-along tests' evidence that [SpeechController] actually enqueued something (or
         * didn't), independent of [SentenceChunker]'s own internal id scheme. */
        val spoken = mutableListOf<String>()
        override val isAvailable = MutableStateFlow(true)
        override fun speak(utteranceId: String, text: String) {
            spoken += text
        }
        override fun stop() {
            stopCount++
        }
        override fun events(): Flow<SynthEvent> = flow {}
    }

    /** Records calls by name instead of touching `android.os.VibrationEffect`/`Vibrator` - those
     * are real Android SDK stubs that throw "not mocked" outside Robolectric, so
     * [VibratorAuraHaptics] itself isn't unit-testable here; this fake is what the §6.13 map's
     * call SITES (inside [AssistTurnMachine]) are actually asserted against. Deliberately does NOT
     * record [invocation] calls - the machine never calls it (§6.13: fired by the host at
     * `onShow()`, outside the machine entirely) - so a regression that wrongly wires it into the
     * machine would show up as an unexpected extra call in these tests. */
    private class RecordingHaptics : AuraHaptics {
        val calls = mutableListOf<String>()
        override fun invocation() { calls += "invocation" }
        override fun transcriptAccepted() { calls += "transcriptAccepted" }
        override fun listeningEnded() { calls += "listeningEnded" }
        override fun settle() { calls += "settle" }
        override fun error() { calls += "error" }
    }

    private fun machine(
        transcriber: Transcriber,
        synthesizer: Synthesizer,
        sessions: List<SessionSummary> = emptyList(),
        scope: kotlinx.coroutines.CoroutineScope,
        refreshSessions: suspend () -> Unit = {},
        haptics: AuraHaptics = NoOpAuraHaptics,
        createSession: suspend () -> String = { "session-1" },
        sendQuery: suspend (String, String) -> Unit = { _, _ -> },
        liveEvents: (String) -> Flow<SessionEvent> = { emptyFlow() },
        onHandoff: (String, InputModality) -> Unit = { _, _ -> },
        onPullUp: (String?, String?, InputModality) -> Unit = { _, _, _ -> },
    ) = AssistTurnMachine(
        transcriber = transcriber,
        synthesizer = synthesizer,
        sessions = MutableStateFlow(sessions),
        createSession = createSession,
        sendQuery = sendQuery,
        liveEvents = liveEvents,
        onHandoff = onHandoff,
        scope = scope,
        refreshSessions = refreshSessions,
        haptics = haptics,
        onPullUp = onPullUp,
    )

    private fun recentSession(sessionId: String = "old-session", title: String = "Yesterday's chat"): SessionSummary {
        // Regression fixture: the real backend emits a numeric offset
        // (`+00:00`), never bare `Z` - `java.time.Instant.now().toString()` produces `Z` and would
        // mask a bare-`Instant.parse` regression in `recentSessionOrNull`, since that form happens
        // to parse fine. This must use the same shape the live device actually sends.
        val recentIso = java.time.Instant.now().toString().removeSuffix("Z") + "+00:00"
        return SessionSummary(
            sessionId = sessionId,
            title = title,
            status = "done",
            running = false,
            doneReason = null,
            origin = null,
            recoverable = false,
            createdAt = recentIso,
            updatedAt = recentIso,
        )
    }

    /** Coarse state-kind label, ignoring within-kind payload changes (rms/partial ticks, streamed
     * item deltas) - lets a test assert the visited KIND sequence without being coupled to exactly
     * how many payload-only emissions happen along the way. */
    private fun AssistUiState.kindName(): String = when (this) {
        AssistUiState.Idle -> "Idle"
        is AssistUiState.Ready -> "Ready"
        is AssistUiState.Listening -> "Listening"
        AssistUiState.Sending -> "Sending"
        is AssistUiState.Streaming -> "Streaming"
        is AssistUiState.Error -> "Error"
    }

    @Test
    fun `show enters READY and populates lastSessionTitle from a recent session`() = runTest {
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            sessions = listOf(recentSession()),
            scope = machineScope(),
        )

        machine.show()

        val ready = machine.state.value
        assertTrue(ready is AssistUiState.Ready)
        assertEquals("Yesterday's chat", (ready as AssistUiState.Ready).lastSessionTitle)
    }

    // ---- auto-listen is the CALLER's (AuraSession's) decision - the machine only
    // needs to behave correctly whichever way that decision goes. ----

    @Test
    fun `auto-listen path (RECORD_AUDIO granted) - show then startListening enters LISTENING`() = runTest {
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
        )

        machine.show()
        machine.startListening() // mirrors AuraSession.onShow()'s granted branch

        assertTrue(machine.state.value is AssistUiState.Listening)
    }

    @Test
    fun `auto-listen path (RECORD_AUDIO denied) - show alone stays READY, text-first`() = runTest {
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
        )

        machine.show() // mirrors AuraSession.onShow()'s denied branch: no startListening() follows

        assertTrue(machine.state.value is AssistUiState.Ready)
    }

    // ---- the FIRST turn of an invocation streams its response IN the overlay - no
    // handoff fires. ----

    @Test
    fun `first voice turn visits IDLE, LISTENING, SENDING, STREAMING - no handoff`() = runTest {
        val transcriber = ScriptedTranscriber(listOf(50L to TranscriberEvent.Ready, 50L to TranscriberEvent.Final("hello")))
        val calls = mutableListOf<String>()
        val machine = machine(
            transcriber = transcriber,
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { calls += "createSession"; "session-1" },
            sendQuery = { id, text -> calls += "sendQuery($id,$text)" },
            onHandoff = { _, _ -> calls += "onHandoff" },
        )

        val visitedKinds = mutableListOf<String>()
        // (flakiness guard, not a current failure): routed through
        // its own machineScope() instance instead of a bare `launch` (a direct structural child of
        // runTest's own TestScope job) - the latter shape is exactly what this file's own class doc
        // warns against for an "infinite background collector" (machine.state never completes on
        // its own), and cancel() alone doesn't guarantee the job has fully reached Completed before
        // the test body returns; a second, independent scope on the SAME testScheduler sidesteps
        // the leak check entirely rather than relying on one more scheduler drain after cancel().
        val collector = machineScope().launch {
            machine.state.collect { state ->
                val kind = state.kindName()
                if (visitedKinds.lastOrNull() != kind) visitedKinds += kind
            }
        }
        // Let the collector actually start and pick up the CURRENT (Idle) value before triggering
        // any transition - otherwise it's still just enqueued when startListening() flips the
        // state, and misses the initial emission entirely.
        runCurrent()

        machine.startListening()
        advanceUntilIdle()
        collector.cancel()

        assertEquals(listOf("Idle", "Listening", "Sending", "Streaming"), visitedKinds)
        assertEquals(listOf("createSession", "sendQuery(session-1,hello)"), calls) // no onHandoff yet
        val streaming = machine.state.value
        assertTrue(streaming is AssistUiState.Streaming)
        assertTrue((streaming as AssistUiState.Streaming).done) // the default empty liveEvents completes at once
    }

    @Test
    fun `first text turn dispatches createSession then sendQuery then streams - no handoff`() = runTest {
        val calls = mutableListOf<String>()
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { calls += "createSession"; "session-2" },
            sendQuery = { id, text -> calls += "sendQuery($id,$text)" },
            onHandoff = { _, _ -> calls += "onHandoff" },
        )

        machine.sendText("What's up?")
        advanceUntilIdle()

        assertEquals(listOf("createSession", "sendQuery(session-2,What's up?)"), calls)
        assertTrue(machine.state.value is AssistUiState.Streaming)
    }

    @Test
    fun `first turn folds liveEvents through TranscriptReducer - completion flips done true and fires settle`() = runTest {
        val haptics = RecordingHaptics()
        val events = flowOf(
            SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Hi", agentId = "root", depth = 0)),
            SessionEvent.AgentMessageDelta(ts = "t2", payload = AgentMessageDeltaPayload(text = " there", agentId = "root", depth = 0)),
            SessionEvent.Assistant(ts = "t3", payload = TextPayload(text = "Hi there")),
            SessionEvent.Completion(ts = "t4", payload = CompletionPayload()),
        )
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = { events },
            haptics = haptics,
        )

        machine.sendText("hello")
        advanceUntilIdle()

        val streaming = machine.state.value
        assertTrue(streaming is AssistUiState.Streaming)
        streaming as AssistUiState.Streaming
        assertTrue(streaming.done)
        assertFalse(streaming.speaking) // forced false once done, regardless of the synthesizer's own tail
        val assistantText = streaming.items.filterIsInstance<ChatItem.AssistantMessage>().single().text
        assertEquals("Hi there", assistantText)
        assertTrue(haptics.calls.contains("settle"))
    }

    // ---- `stream_end` is a genuine terminal frame `SessionStreamClient` really delivers (after
    // `trySend`, before its own termination flag flips - `data/sse/SessionStreamClient.kt`), and
    // `RunRepository.live`'s `shareIn(... WhileSubscribed ...)` SharedFlow never itself completes -
    // so a real collect loop sees `StreamEnd` as just another event, and the post-collect fallback
    // below never runs. Both scripted flows model that with `awaitCancellation()` after `StreamEnd`. ----

    @Test
    fun `stream_end after completion keeps the card done - terminal frame must not reset to streaming`() = runTest {
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = {
                flow {
                    emit(SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Hi there", agentId = "root", depth = 0)))
                    emit(SessionEvent.Assistant(ts = "t2", payload = TextPayload(text = "Hi there")))
                    emit(SessionEvent.Completion(ts = "t3", payload = CompletionPayload()))
                    emit(SessionEvent.StreamEnd) // the terminal frame SessionStreamClient really delivers
                    awaitCancellation() // shareIn's SharedFlow never completes - the fallback is dead
                }
            },
        )

        machine.sendText("hello")
        advanceUntilIdle()

        val streaming = machine.state.value
        assertTrue(streaming is AssistUiState.Streaming)
        assertTrue("stream_end after completion must not reset done to false", (streaming as AssistUiState.Streaming).done)
    }

    @Test
    fun `a materialized stream_error ends the turn as an error, never a wedged streaming card`() = runTest {
        // `RunRepository.live()` turns an upstream failure into a StreamError VALUE
        // (`.catch { emit(...) }`) so every follower can see it — so it never THROWS, the machine's
        // own `catch` cannot fire, and the SharedFlow never completes so the post-collect fallback
        // cannot either. Treated as non-terminal it left `done` false forever: the overlay composer
        // stayed disarmed and TalkBack went on announcing "Responding" with nothing ever arriving
        // to correct it. This asserts the ERROR state rather than merely `done` — a lost connection
        // is not a turn that finished, and settling it would say it was.
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = {
                flow {
                    emit(SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Par", agentId = "root", depth = 0)))
                    emit(SessionEvent.StreamError(message = "Lost connection to the run"))
                    awaitCancellation() // shareIn's SharedFlow never completes - the fallback is dead
                }
            },
            haptics = haptics,
        )

        machine.sendText("hello")
        advanceUntilIdle()

        val state = machine.state.value
        assertTrue("a materialized stream_error must leave the Streaming state", state is AssistUiState.Error)
        assertEquals("Lost connection to the run", (state as AssistUiState.Error).reason)
        assertTrue("a lost stream is an error, not a settle", haptics.calls.contains("error"))
    }

    @Test
    fun `a non-terminal event between completion and stream_end keeps done sticky - settle fires once`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = {
                flow {
                    emit(SessionEvent.Assistant(ts = "t1", payload = TextPayload(text = "Hi there")))
                    emit(SessionEvent.Completion(ts = "t2", payload = CompletionPayload()))
                    // A stray non-terminal delta arriving AFTER completion but BEFORE stream_end must
                    // NOT reset the just-finalized card back to streaming - done is sticky, so
                    // finishStreaming() (and its settle haptic) fires exactly once, not again on the
                    // trailing stream_end.
                    emit(SessionEvent.AgentMessageDelta(ts = "t3", payload = AgentMessageDeltaPayload(text = "!", agentId = "root", depth = 0)))
                    emit(SessionEvent.StreamEnd)
                    awaitCancellation() // shareIn's SharedFlow never completes - the post-collect fallback is dead
                }
            },
            haptics = haptics,
        )

        machine.sendText("hello")
        advanceUntilIdle()

        val streaming = machine.state.value
        assertTrue(streaming is AssistUiState.Streaming)
        assertTrue("a post-completion non-terminal event must not reset done to false", (streaming as AssistUiState.Streaming).done)
        assertEquals(1, haptics.calls.count { it == "settle" })
    }

    @Test
    fun `stream_end without completion also finalizes the turn`() = runTest {
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = {
                flow {
                    emit(SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Hi there", agentId = "root", depth = 0)))
                    emit(SessionEvent.Assistant(ts = "t2", payload = TextPayload(text = "Hi there")))
                    emit(SessionEvent.StreamEnd) // terminal control frame, no prior `completion` at all
                    awaitCancellation()
                }
            },
        )

        machine.sendText("hello")
        advanceUntilIdle()

        val streaming = machine.state.value
        assertTrue(streaming is AssistUiState.Streaming)
        assertTrue("stream_end with no prior completion must still finalize done=true", (streaming as AssistUiState.Streaming).done)
    }

    @Test
    fun `a lost live connection surfaces as ERROR with no retry text`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = { flow { throw RuntimeException("connection lost") } },
            haptics = haptics,
        )

        machine.sendText("hello")
        advanceUntilIdle()

        val error = machine.state.value
        assertTrue(error is AssistUiState.Error)
        assertEquals("", (error as AssistUiState.Error).retryText)
        assertTrue(haptics.calls.contains("error"))
    }

    // ---- the SECOND interaction of an invocation hands off to the app instead of
    // streaming again - the session is REUSED, never re-created. ----

    @Test
    fun `second text follow-up hands off - session reused, tagged with ITS OWN modality (Text)`() = runTest {
        val calls = mutableListOf<String>()
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("first"))), // voice-initiated FIRST turn
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { calls += "createSession"; "session-1" },
            sendQuery = { id, text -> calls += "sendQuery($id,$text)" },
            onHandoff = { id, modality -> calls += "onHandoff($id,$modality)" },
        )

        machine.startListening()
        advanceUntilIdle()
        assertTrue(machine.state.value is AssistUiState.Streaming)
        calls.clear()

        machine.sendText("second, typed")
        advanceUntilIdle()

        // No SECOND createSession - the SAME session from the first turn is reused; the handoff is
        // tagged with THIS turn's own modality (Text), independent of the first turn having been Voice.
        assertEquals(listOf("sendQuery(session-1,second, typed)", "onHandoff(session-1,Text)"), calls)
    }

    @Test
    fun `second voice follow-up hands off tagged Voice`() = runTest {
        val calls = mutableListOf<String>()
        val machine = machine(
            transcriber = ScriptedTranscriber(
                listOf(10L to TranscriberEvent.Final("first")), // capture session 1 (first turn)
                listOf(10L to TranscriberEvent.Final("second")), // capture session 2 (the follow-up)
            ),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { calls += "createSession"; "session-1" },
            sendQuery = { id, text -> calls += "sendQuery($id,$text)" },
            onHandoff = { id, modality -> calls += "onHandoff($id,$modality)" },
        )

        machine.startListening() // consumes the FIRST scripted Final, streams in-overlay
        advanceUntilIdle()
        assertTrue(machine.state.value is AssistUiState.Streaming)
        calls.clear()

        machine.startListening() // a fresh capture for the SECOND turn, consumes the second Final
        advanceUntilIdle()

        assertEquals(listOf("sendQuery(session-1,second)", "onHandoff(session-1,Voice)"), calls)
    }

    // ---- stopStreaming / expand / toggleSpeak (the card's own controls) ----

    @Test
    fun `stopStreaming cancels the live job and marks the card done - a later scripted event never folds in`() = runTest {
        val liveEvents = delayedEvents(
            10L to SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Hi", agentId = "root", depth = 0)),
            1_000L to SessionEvent.AgentMessageDelta(ts = "t2", payload = AgentMessageDeltaPayload(text = " there", agentId = "root", depth = 0)),
        )
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = liveEvents,
        )

        machine.sendText("hello")
        advanceTimeBy(50) // past the first delta (t=10), well before the second (t=1010)
        runCurrent()
        val beforeStop = machine.state.value as AssistUiState.Streaming
        assertFalse(beforeStop.done)

        machine.stopStreaming()
        val afterStop = machine.state.value as AssistUiState.Streaming
        assertTrue(afterStop.done)
        assertFalse(afterStop.speaking)

        advanceUntilIdle() // if the job weren't truly cancelled, the t=1010 delta would land here
        val finalState = machine.state.value as AssistUiState.Streaming
        assertEquals(afterStop.items, finalState.items) // unchanged - the stream job was genuinely cancelled
    }

    @Test
    fun `expand hands off on the current session while still streaming - no additional dispatch`() = runTest {
        val calls = mutableListOf<String>()
        var handedOff: Pair<String, InputModality>? = null
        val liveEvents = delayedEvents(1_000L to SessionEvent.Completion(ts = "t1", payload = CompletionPayload()))
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { calls += "createSession"; "session-1" },
            sendQuery = { id, text -> calls += "sendQuery($id,$text)" },
            onHandoff = { id, modality -> handedOff = id to modality },
            liveEvents = liveEvents,
        )

        machine.startListening()
        advanceTimeBy(30) // past Final -> beginTurn -> subscribeLive, well before the 1000ms completion
        runCurrent()
        assertTrue(machine.state.value is AssistUiState.Streaming)
        assertFalse((machine.state.value as AssistUiState.Streaming).done)
        calls.clear()

        machine.expand()

        assertEquals("session-1" to InputModality.Voice, handedOff)
        assertTrue(calls.isEmpty()) // expand never re-dispatches
    }

    @Test
    fun `expand is a no-op before the first turn's session id has resolved`() = runTest {
        var handoffCalled = false
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            onHandoff = { _, _ -> handoffCalled = true },
        )

        machine.show()
        machine.expand()

        assertFalse(handoffCalled)
    }

    // ---- Pull-up (swipe-up on the composer pill) handoff (user directive): the routing
    // DECISION seam - session present => route to it, absent => new chat; draft carried in both. ----

    @Test
    fun `pull-up with an active session routes to THAT session, carrying the draft plus a settle haptic`() = runTest {
        var pulled: Triple<String?, String?, InputModality>? = null
        val calls = RecordingHaptics()
        // A live-but-unfinished first turn so a session id has resolved and we're still Streaming.
        val liveEvents = delayedEvents(1_000L to SessionEvent.Completion(ts = "t1", payload = CompletionPayload()))
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = calls,
            createSession = { "session-1" },
            liveEvents = liveEvents,
            onPullUp = { id, draft, modality -> pulled = Triple(id, draft, modality) },
        )

        machine.startListening()
        advanceTimeBy(30) // Final -> beginTurn -> subscribeLive; session-1 resolved, still streaming
        runCurrent()
        assertTrue(machine.state.value is AssistUiState.Streaming)
        calls.calls.clear() // drop the voice-Final transcriptAccepted tick so `settle` below is unambiguous

        machine.pullUpToApp("half typed")

        // The FIRST turn was voice, so turnModality rode through to the handoff (same as expand).
        assertEquals(Triple<String?, String?, InputModality>("session-1", "half typed", InputModality.Voice), pulled)
        assertTrue(calls.calls.contains("settle"))
    }

    @Test
    fun `pull-up before any turn routes to a NEW chat (null session id), carrying the draft`() = runTest {
        var pulled: Triple<String?, String?, InputModality>? = null
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            onPullUp = { id, draft, modality -> pulled = Triple(id, draft, modality) },
        )

        machine.show() // Ready - no turn dispatched, so no session exists yet
        machine.pullUpToApp("draft for a new chat")

        // null session id = "open a fresh new chat"; no turn ran, so modality is the default Text.
        assertEquals(Triple<String?, String?, InputModality>(null, "draft for a new chat", InputModality.Text), pulled)
    }

    @Test
    fun `pull-up with a blank draft carries a null draft`() = runTest {
        var pulled: Triple<String?, String?, InputModality>? = null
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            onPullUp = { id, draft, modality -> pulled = Triple(id, draft, modality) },
        )

        machine.show()
        machine.pullUpToApp("   ") // whitespace-only draft collapses to null so the app never seeds it

        assertEquals(Triple<String?, String?, InputModality>(null, null, InputModality.Text), pulled)
    }

    // ---- speak-along (voice modality only), moved into AssistTurnMachine's own
    // SpeechController instance. ----

    @Test
    fun `a voice-modality first turn speaks the folded assistant text`() = runTest {
        val synthesizer = RecordingSynthesizer()
        val events = flowOf(
            SessionEvent.Assistant(ts = "t1", payload = TextPayload(text = "Hi there.")),
            SessionEvent.Completion(ts = "t2", payload = CompletionPayload()),
        )
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = synthesizer,
            scope = machineScope(),
            liveEvents = { events },
        )

        machine.startListening()
        advanceUntilIdle()

        assertTrue(synthesizer.spoken.isNotEmpty())
    }

    @Test
    fun `a text-modality first turn stays silent - nothing is ever enqueued`() = runTest {
        val synthesizer = RecordingSynthesizer()
        val events = flowOf(
            SessionEvent.Assistant(ts = "t1", payload = TextPayload(text = "Hi there.")),
            SessionEvent.Completion(ts = "t2", payload = CompletionPayload()),
        )
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = synthesizer,
            scope = machineScope(),
            liveEvents = { events },
        )

        machine.sendText("hello")
        advanceUntilIdle()

        assertTrue(synthesizer.spoken.isEmpty())
    }

    @Test
    fun `toggleSpeak mutes further speak-along for the rest of this turn`() = runTest {
        val synthesizer = RecordingSynthesizer()
        val liveEvents = delayedEvents(
            10L to SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Hi there.", agentId = "root", depth = 0)),
            1_000L to SessionEvent.Assistant(ts = "t2", payload = TextPayload(text = "Hi there. More to say.")),
        )
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = synthesizer,
            scope = machineScope(),
            liveEvents = liveEvents,
        )

        machine.startListening()
        advanceTimeBy(30) // past Final -> beginTurn -> subscribeLive -> the first (complete-sentence) delta
        runCurrent()
        val spokenBeforeMute = synthesizer.spoken.size
        assertTrue("the complete first sentence should already have been spoken", spokenBeforeMute > 0)

        machine.toggleSpeak() // mute
        advanceUntilIdle() // lets the second (finalizing) event land and fold, but speech is muted now

        assertEquals(spokenBeforeMute, synthesizer.spoken.size) // no NEW utterances after mute
    }

    // ---- dismiss resets turn/session bookkeeping - the real VoiceInteractionSession
    // (and this SAME machine instance) survives hide->show, so this is the regression test for
    // "a second invocation must not inherit the first invocation's turn count." ----

    @Test
    fun `dismiss resets turn bookkeeping - a fresh show+turn streams in-overlay again, not an immediate handoff`() = runTest {
        val calls = mutableListOf<String>()
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { calls += "createSession"; "session-A" },
            sendQuery = { id, text -> calls += "sendQuery($id,$text)" },
            onHandoff = { id, _ -> calls += "onHandoff($id)" },
        )

        // First invocation: one turn streams in-overlay.
        machine.show()
        machine.sendText("first invocation query")
        advanceUntilIdle()
        assertTrue(machine.state.value is AssistUiState.Streaming)
        assertEquals(listOf("createSession", "sendQuery(session-A,first invocation query)"), calls)

        // Mirrors AuraSession.onHide()/dismissSession() tearing the window down WITHOUT destroying
        // this AssistTurnMachine instance (the real VoiceInteractionSession survives hide->show).
        machine.dismiss()
        calls.clear()

        // A second, entirely separate assistant invocation reusing the SAME machine instance.
        machine.show()
        machine.sendText("second invocation query")
        advanceUntilIdle()

        // Must stream in-overlay again (a fresh "first turn"), NOT hand off immediately as if this
        // were a turnCount>=1 follow-up left over from the previous invocation.
        assertTrue(machine.state.value is AssistUiState.Streaming)
        assertEquals(listOf("createSession", "sendQuery(session-A,second invocation query)"), calls)
    }

    @Test
    fun `dismiss cancels an in-flight FIRST-turn stream - no further folds land after teardown`() = runTest {
        val liveEvents = delayedEvents(
            10L to SessionEvent.AgentMessageDelta(ts = "t1", payload = AgentMessageDeltaPayload(text = "Hi", agentId = "root", depth = 0)),
            1_000L to SessionEvent.AgentMessageDelta(ts = "t2", payload = AgentMessageDeltaPayload(text = " there", agentId = "root", depth = 0)),
        )
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            liveEvents = liveEvents,
        )

        machine.sendText("hello")
        advanceTimeBy(50)
        runCurrent()
        assertTrue(machine.state.value is AssistUiState.Streaming)

        machine.dismiss()
        assertTrue(machine.state.value is AssistUiState.Idle)

        advanceUntilIdle()
        assertTrue(machine.state.value is AssistUiState.Idle) // the t=1010 delta never resurrected Streaming
    }

    // ---- InputModality tagging - client-only, never dispatched to the backend;
    // threaded through onHandoff so the picked-up chat can tell a voice turn from a typed one. ----

    @Test
    fun `continueLastSession hands off directly with Text modality - no createSession or sendQuery call`() = runTest {
        val haptics = RecordingHaptics()
        val calls = mutableListOf<String>()
        var handedOff: String? = null
        var handedOffModality: InputModality? = null
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            sessions = listOf(recentSession()),
            scope = machineScope(),
            createSession = { calls += "createSession"; "should-not-be-created" },
            sendQuery = { _, _ -> calls += "sendQuery" },
            onHandoff = { id, modality -> handedOff = id; handedOffModality = modality },
            haptics = haptics,
        )

        machine.show()
        machine.continueLastSession()

        assertEquals("old-session", handedOff)
        // No query was ever dispatched, so there's no "originating" turn to tag - Text is the
        // neutral default.
        assertEquals(InputModality.Text, handedOffModality)
        assertTrue(calls.isEmpty())
        assertEquals(listOf("settle"), haptics.calls)
    }

    @Test
    fun `continueLastSession with no recent session is a no-op`() = runTest {
        var handoffCalled = false
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            sessions = emptyList(),
            scope = machineScope(),
            onHandoff = { _, _ -> handoffCalled = true },
        )

        machine.show()
        machine.continueLastSession()

        assertFalse(handoffCalled)
    }

    @Test
    fun `silence timeout returns quietly to READY (overlay stays open) when nothing is ever heard`() = runTest {
        val transcriber = ScriptedTranscriber(listOf(100L to TranscriberEvent.Ready)) // never emits Final
        val machine = machine(transcriber = transcriber, synthesizer = RecordingSynthesizer(), scope = machineScope())

        machine.startListening()
        assertTrue(machine.state.value is AssistUiState.Listening)

        advanceTimeBy(8_100)
        advanceUntilIdle()

        assertTrue(machine.state.value is AssistUiState.Ready)
    }

    @Test
    fun `continuous Rms churn with no speech evidence still times out to READY`() = runTest {
        // SpeechRecognizer#onRmsChanged fires continuously on real hardware regardless of whether
        // the user is actually speaking - Rms alone must NOT reset the silence timer, or it would
        // never fire in production. 20 Rms ticks 500ms apart (10s of continuous "audio energy"
        // churn) with no Partial/Final at all.
        val script = buildList {
            add(0L to TranscriberEvent.Ready)
            repeat(20) { add(500L to TranscriberEvent.Rms(3f)) }
        }
        val machine = machine(transcriber = ScriptedTranscriber(script), synthesizer = RecordingSynthesizer(), scope = machineScope())

        machine.startListening()
        assertTrue(machine.state.value is AssistUiState.Listening)

        advanceTimeBy(8_100)
        advanceUntilIdle()

        assertTrue(machine.state.value is AssistUiState.Ready)
    }

    // ---- Mic-tap permission gate - AuraSession is what actually holds a Context
    // and decides RECORD_AUDIO's grant state; the machine just needs a way to surface the result
    // as the SAME quiet notice a failed send already uses. ----

    @Test
    fun `microphonePermissionUnavailable enters ERROR with no retry text - nothing was ever queued to resend`() = runTest {
        val machine = machine(transcriber = ScriptedTranscriber(emptyList()), synthesizer = RecordingSynthesizer(), scope = machineScope())
        machine.show()

        machine.microphonePermissionUnavailable()

        val error = machine.state.value
        assertTrue(error is AssistUiState.Error)
        assertEquals("", (error as AssistUiState.Error).retryText)
        assertTrue(error.reason.isNotBlank())
    }

    @Test
    fun `cancelListening returns to READY - the overlay stays open`() = runTest {
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(100L to TranscriberEvent.Ready)),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
        )

        machine.startListening()
        assertTrue(machine.state.value is AssistUiState.Listening)

        machine.cancelListening()
        assertTrue(machine.state.value is AssistUiState.Ready)
    }

    @Test
    fun `barge-in stops the synthesizer on a fresh LISTENING turn and on dismiss`() = runTest {
        // Routed through AssistTurnMachine's own SpeechController.bargeIn(), which
        // itself calls synthesizer.stop() - the OBSERVABLE stop-count contract is unchanged.
        val synthesizer = RecordingSynthesizer()
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(100L to TranscriberEvent.Ready)),
            synthesizer = synthesizer,
            scope = machineScope(),
        )

        machine.startListening()
        assertEquals(1, synthesizer.stopCount)

        machine.dismiss()
        assertEquals(2, synthesizer.stopCount)
    }

    @Test
    fun `sessions refresh fires at most once across show and startListening, and a failing refresh never breaks a turn`() = runTest {
        var refreshCount = 0
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hi"))),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            refreshSessions = { refreshCount++; error("offline") },
        )

        machine.show()
        machine.startListening()
        advanceUntilIdle()

        assertEquals(1, refreshCount)
        assertTrue(machine.state.value is AssistUiState.Streaming) // the throwing refresh didn't derail the turn
    }

    @Test
    fun `voice-initiated first turn fires transcriptAccepted on Final, then settle once streaming completes`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        advanceUntilIdle() // the default empty liveEvents completes at once, triggering finishStreaming()

        assertEquals(listOf("transcriptAccepted", "settle"), haptics.calls)
    }

    @Test
    fun `text-initiated first turn never fires transcriptAccepted, but does fire settle once streaming completes`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.sendText("What's up?")
        advanceUntilIdle()

        assertEquals(listOf("settle"), haptics.calls)
    }

    // ---- [R4] listeningEnded haptic: an a11y-mandated non-visual "mic is off" cue for
    // every way capture can end WITHOUT an accepted transcript - distinct from transcriptAccepted's
    // accepted-Final path (the two moments must never both fire for the same capture). ----

    @Test
    fun `cancelListening fires the listeningEnded haptic`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(100L to TranscriberEvent.Ready)),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        machine.cancelListening()

        assertEquals(listOf("listeningEnded"), haptics.calls)
    }

    @Test
    fun `silence timeout fires the listeningEnded haptic`() = runTest {
        val haptics = RecordingHaptics()
        val transcriber = ScriptedTranscriber(listOf(100L to TranscriberEvent.Ready)) // never emits Final
        val machine = machine(
            transcriber = transcriber,
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        advanceTimeBy(8_100) // past SILENCE_TIMEOUT_MS (8_000ms), reset once by the scripted Ready at t=100
        advanceUntilIdle()

        assertEquals(listOf("listeningEnded"), haptics.calls)
    }

    @Test
    fun `a recognizer error fires the listeningEnded haptic`() = runTest {
        val haptics = RecordingHaptics()
        val transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Error(code = TranscriberError.Other)))
        val machine = machine(
            transcriber = transcriber,
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        advanceUntilIdle()

        assertEquals(listOf("listeningEnded"), haptics.calls)
        assertTrue(machine.state.value is AssistUiState.Ready)
    }

    @Test
    fun `a failed speech SERVICE surfaces an error instead of returning quietly`() = runTest {
        // The one error that is not a quiet-cancel. On this voice-first surface there is no
        // transcript to look at, so a silent return to Ready is indistinguishable from the
        // assistant having ignored a sentence the user just finished speaking — and the cause
        // (a server engine they selected in Settings) is invisible from here.
        val haptics = RecordingHaptics()
        val transcriber = ScriptedTranscriber(
            listOf(10L to TranscriberEvent.Error(code = TranscriberError.ServiceFailed)),
        )
        val machine = machine(
            transcriber = transcriber,
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        advanceUntilIdle()

        val state = machine.state.value
        assertTrue("expected an Error state, got $state", state is AssistUiState.Error)
        assertTrue((state as AssistUiState.Error).reason.contains("Settings"))
        // Nothing to resend — the audio is gone and this surface cannot re-submit a recording.
        assertEquals("", state.retryText)
        assertEquals(listOf("error"), haptics.calls)
    }

    @Test
    fun `every other recognizer error stays a quiet cancel`() = runTest {
        // Guards the split from the other side: widening the loud branch to any error would put a
        // red card in front of a user who simply said nothing.
        for (code in listOf(TranscriberError.NoMatch, TranscriberError.Timeout, TranscriberError.Unavailable, TranscriberError.Other)) {
            val haptics = RecordingHaptics()
            val machine = machine(
                transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Error(code = code))),
                synthesizer = RecordingSynthesizer(),
                scope = machineScope(),
                haptics = haptics,
            )

            machine.startListening()
            advanceUntilIdle()

            assertTrue("$code should return quietly", machine.state.value is AssistUiState.Ready)
            assertEquals("$code should not fire the error haptic", listOf("listeningEnded"), haptics.calls)
        }
    }

    @Test
    fun `an accepted final transcript does NOT fire listeningEnded`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        advanceUntilIdle() // Final -> transcriptAccepted -> beginTurn -> streaming completes -> settle

        assertEquals(listOf("transcriptAccepted", "settle"), haptics.calls)
        assertEquals(0, haptics.calls.count { it == "listeningEnded" })
    }

    @Test
    fun `cancelListening is a no-op once a Final has been accepted - no listeningEnded, state unchanged`() = runTest {
        val haptics = RecordingHaptics()
        val machine = machine(
            transcriber = ScriptedTranscriber(listOf(10L to TranscriberEvent.Final("hello"))),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            haptics = haptics,
        )

        machine.startListening()
        advanceUntilIdle() // Final accepted -> transcriptAccepted -> beginTurn -> streaming completes
        val stateAfterFinal = machine.state.value
        assertTrue(stateAfterFinal is AssistUiState.Streaming) // no longer Listening
        val hapticsAfterFinal = haptics.calls.toList()

        machine.cancelListening() // must early-return: the machine is not Listening anymore

        assertFalse(haptics.calls.contains("listeningEnded")) // no end-of-listening cue for a turn that already ended
        assertEquals(hapticsAfterFinal, haptics.calls) // no haptic fired at all
        assertEquals(stateAfterFinal, machine.state.value) // state untouched (still the done Streaming card)
    }

    @Test
    fun `SENDING is entered immediately, before createSession resolves`() = runTest {
        val hangingCreate: suspend () -> String = { awaitCancellation() }
        val machine = AssistTurnMachine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            sessions = MutableStateFlow(emptyList()),
            createSession = hangingCreate,
            sendQuery = { _, _ -> },
            liveEvents = { emptyFlow() },
            onHandoff = { _, _ -> },
            scope = machineScope(),
        )

        machine.sendText("What's the weather?")
        runCurrent()

        assertTrue(machine.state.value is AssistUiState.Sending)
    }

    @Test
    fun `session-create failure enters ERROR with the text preserved, no handoff`() = runTest {
        val haptics = RecordingHaptics()
        var handoffCalled = false
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            createSession = { error("offline") },
            onHandoff = { _, _ -> handoffCalled = true },
            haptics = haptics,
        )

        machine.sendText("hello there")
        advanceUntilIdle()

        val error = machine.state.value
        assertTrue(error is AssistUiState.Error)
        assertEquals("hello there", (error as AssistUiState.Error).retryText)
        assertEquals(listOf("error"), haptics.calls)
        assertFalse(handoffCalled)
    }

    @Test
    fun `dispatch failure (sendQuery throws) also enters ERROR, no handoff`() = runTest {
        val haptics = RecordingHaptics()
        var handoffCalled = false
        val machine = machine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            scope = machineScope(),
            sendQuery = { _, _ -> error("connection lost") },
            onHandoff = { _, _ -> handoffCalled = true },
            haptics = haptics,
        )

        machine.sendText("hello")
        advanceUntilIdle()

        assertTrue(machine.state.value is AssistUiState.Error)
        assertEquals(listOf("error"), haptics.calls)
        assertFalse(handoffCalled)
    }

    @Test
    fun `dismiss cancels an in-flight send - no onHandoff fires afterward`() = runTest {
        var handoffCalled = false
        val hangingCreate: suspend () -> String = { awaitCancellation() }
        val machine = AssistTurnMachine(
            transcriber = ScriptedTranscriber(emptyList()),
            synthesizer = RecordingSynthesizer(),
            sessions = MutableStateFlow(emptyList()),
            createSession = hangingCreate,
            sendQuery = { _, _ -> },
            liveEvents = { emptyFlow() },
            onHandoff = { _, _ -> handoffCalled = true },
            scope = machineScope(),
        )

        machine.sendText("hello")
        runCurrent()
        assertTrue(machine.state.value is AssistUiState.Sending)

        machine.dismiss()

        assertTrue(machine.state.value is AssistUiState.Idle)
        assertFalse(handoffCalled)
    }
}
