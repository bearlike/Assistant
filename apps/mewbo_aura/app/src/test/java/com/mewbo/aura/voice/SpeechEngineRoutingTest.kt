package com.mewbo.aura.voice

import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The routing contract: which implementation a speech call actually reaches, given what the user
 * picked in Settings.
 *
 * Plain JVM, no Robolectric, because both legs of each router are plain interfaces behind
 * qualifiers and the selection arrives through [SpeechEngineGate] — the same shape (and the same
 * reason) as `DeviceToolGate`. Naming `RemoteSynthesizer` concretely in the router would have
 * dragged `Context`/`AudioManager`/`MediaPlayer` into every test here to assert a branch that
 * touches none of them.
 *
 * These cover the feature's two load-bearing claims — the default is on-device, and a selection
 * reaches the engine it names — neither of which is observable from the settings screen.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SpeechEngineRoutingTest {

    // ---- Transcriber routing ----

    @Test
    fun `an unset selection transcribes on device`() = runTest {
        val onDevice = RecordingTranscriber(TranscriberEvent.Final("local"))
        val remote = RecordingTranscriber(TranscriberEvent.Final("remote"))
        val router = SelectedTranscriber(onDevice, remote, FakeGate(stt = SpeechCatalog.ON_DEVICE), available())

        assertEquals(listOf(TranscriberEvent.Final("local")), router.listen().toList())
        assertEquals(1, onDevice.listenCalls)
        assertEquals("the remote engine must not be started at all", 0, remote.listenCalls)
    }

    @Test
    fun `a blank selection means on device, not a server engine with a blank id`() {
        // The whole default rests on this: DataStore returns "" for a key never written, and that
        // has to mean the platform recognizer rather than a lookup for a blank-id engine.
        assertTrue(SpeechCatalog.isOnDevice(""))
        assertTrue(SpeechCatalog.isOnDevice("   "))
        assertFalse(SpeechCatalog.isOnDevice("nova-3"))
    }

    @Test
    fun `a selected server engine transcribes remotely, never on device`() = runTest {
        val onDevice = RecordingTranscriber(TranscriberEvent.Final("local"))
        val remote = RecordingTranscriber(TranscriberEvent.Final("remote"))
        val router = SelectedTranscriber(onDevice, remote, FakeGate(stt = "nova-3"), available())

        assertEquals(listOf(TranscriberEvent.Final("remote")), router.listen().toList())
        assertEquals("the microphone must not be opened locally", 0, onDevice.listenCalls)
    }

    @Test
    fun `a selected server engine transcribes remotely even when on-device recognition IS available`() = runTest {
        // Availability is only consulted for an on-device SELECTION — an explicit server choice
        // must never fall back to on-device just because the platform recognizer happens to work.
        val onDevice = RecordingTranscriber(TranscriberEvent.Final("local"))
        val remote = RecordingTranscriber(TranscriberEvent.Final("remote"))
        val router = SelectedTranscriber(onDevice, remote, FakeGate(stt = "nova-3"), available(true))

        assertEquals(listOf(TranscriberEvent.Final("remote")), router.listen().toList())
        assertEquals(0, onDevice.listenCalls)
    }

    @Test
    fun `on-device selected but recognition unavailable falls back to the server engine, not a fake`() = runTest {
        // The Fire TV defect this guards: no RecognitionService on the device, so the platform
        // recognizer can never fire. Before this fallback, the debug on-device leg silently
        // substituted a scripted FakeTranscriber here instead of routing to a real engine.
        val onDevice = RecordingTranscriber(TranscriberEvent.Final("local"))
        val remote = RecordingTranscriber(TranscriberEvent.Final("remote"))
        val router = SelectedTranscriber(onDevice, remote, FakeGate(stt = SpeechCatalog.ON_DEVICE), available(false))

        assertEquals(listOf(TranscriberEvent.Final("remote")), router.listen().toList())
        assertEquals("the unavailable on-device leg must never be started", 0, onDevice.listenCalls)
        assertEquals(1, remote.listenCalls)
    }

    @Test
    fun `the selection is re-read per capture, so a change needs no restart`() = runTest {
        val onDevice = RecordingTranscriber(TranscriberEvent.Final("local"))
        val remote = RecordingTranscriber(TranscriberEvent.Final("remote"))
        val gate = FakeGate(stt = SpeechCatalog.ON_DEVICE)
        val router = SelectedTranscriber(onDevice, remote, gate, available())

        assertEquals(listOf(TranscriberEvent.Final("local")), router.listen().toList())
        gate.sttSelection.value = "nova-3"
        assertEquals(listOf(TranscriberEvent.Final("remote")), router.listen().toList())

        assertEquals(1, onDevice.listenCalls)
        assertEquals(1, remote.listenCalls)
    }

    // ---- Synthesizer routing ----

    @Test
    fun `an unset selection speaks on device`() = runTest {
        val onDevice = RecordingSynthesizer()
        val remote = RecordingSynthesizer()
        val router = synthesizer(onDevice, remote, FakeGate(tts = SpeechCatalog.ON_DEVICE))
        advanceUntilIdle()

        router.speak("m1:0", "hello")

        assertEquals(listOf("m1:0" to "hello"), onDevice.spoken)
        assertTrue(remote.spoken.isEmpty())
    }

    @Test
    fun `a selected server engine speaks remotely`() = runTest {
        val onDevice = RecordingSynthesizer()
        val remote = RecordingSynthesizer()
        val router = synthesizer(onDevice, remote, FakeGate(tts = "supertonic-3"))
        advanceUntilIdle()

        router.speak("m1:0", "hello")

        assertEquals(listOf("m1:0" to "hello"), remote.spoken)
        assertTrue(onDevice.spoken.isEmpty())
    }

    @Test
    fun `one message never splits across two engines mid-reply`() = runTest {
        // A reply is enqueued sentence by sentence. Resolving the engine per call would let a
        // selection changed mid-stream leave half a message on each engine — and the two queues
        // know nothing about each other, so they would overlap rather than take turns.
        val onDevice = RecordingSynthesizer()
        val remote = RecordingSynthesizer()
        val gate = FakeGate(tts = SpeechCatalog.ON_DEVICE)
        val router = synthesizer(onDevice, remote, gate)
        advanceUntilIdle()

        router.speak("m1:0", "first sentence")
        gate.ttsSelection.value = "supertonic-3"
        advanceUntilIdle()
        router.speak("m1:1", "second sentence")

        assertEquals(listOf("m1:0" to "first sentence", "m1:1" to "second sentence"), onDevice.spoken)
        assertTrue("the run was latched to on-device before the change", remote.spoken.isEmpty())
    }

    @Test
    fun `stop releases the latch, so the next run honours the new selection`() = runTest {
        val onDevice = RecordingSynthesizer()
        val remote = RecordingSynthesizer()
        val gate = FakeGate(tts = SpeechCatalog.ON_DEVICE)
        val router = synthesizer(onDevice, remote, gate)
        advanceUntilIdle()

        router.speak("m1:0", "first")
        gate.ttsSelection.value = "supertonic-3"
        advanceUntilIdle()
        router.stop() // barge-in: this run is over
        router.speak("m2:0", "second")

        assertEquals(listOf("m1:0" to "first"), onDevice.spoken)
        assertEquals(listOf("m2:0" to "second"), remote.spoken)
    }

    @Test
    fun `stop reaches BOTH engines, so a switch cannot leave one still talking`() = runTest {
        val onDevice = RecordingSynthesizer()
        val remote = RecordingSynthesizer()
        val router = synthesizer(onDevice, remote, FakeGate(tts = "supertonic-3"))
        advanceUntilIdle()

        router.stop()

        assertEquals(1, onDevice.stopCalls)
        assertEquals(1, remote.stopCalls)
    }

    @Test
    fun `availability follows the selected engine`() = runTest {
        // A device with no TTS voice data reports the platform engine unavailable, which disables
        // read-aloud. Picking a server engine must re-enable it.
        val onDevice = RecordingSynthesizer(available = false)
        val remote = RecordingSynthesizer(available = true)
        val gate = FakeGate(tts = SpeechCatalog.ON_DEVICE)
        val router = synthesizer(onDevice, remote, gate)
        advanceUntilIdle()

        assertFalse(router.isAvailable.value)
        gate.ttsSelection.value = "supertonic-3"
        advanceUntilIdle()
        assertTrue(router.isAvailable.value)
    }

    @Test
    fun `events from both engines reach one collector`() = runTest {
        // SpeechController subscribes ONCE at construction and never re-subscribes, so an event
        // from whichever engine is active has to arrive on that same stream — otherwise a switch
        // would leave the speaking indicator lit forever.
        val onDevice = RecordingSynthesizer()
        val remote = RecordingSynthesizer()
        val router = synthesizer(onDevice, remote, FakeGate(tts = SpeechCatalog.ON_DEVICE))
        advanceUntilIdle()

        val seen = mutableListOf<SynthEvent>()
        val collector = launch { router.events().collect { seen += it } }
        advanceUntilIdle()

        onDevice.emit(SynthEvent.Done("m1:0"))
        remote.emit(SynthEvent.Done("m2:0"))
        advanceUntilIdle()
        collector.cancel()

        assertEquals(listOf(SynthEvent.Done("m1:0"), SynthEvent.Done("m2:0")), seen)
    }

    // ---- fixtures ----

    /**
     * The `machineScope()` idiom from this module's test CLAUDE.md: an INDEPENDENT scope on the
     * SAME scheduler, so `advanceUntilIdle()` drives the router's eager `stateIn` collectors while
     * `runTest`'s leak check ignores them — they never complete, by design.
     */
    private fun TestScope.synthesizer(
        onDevice: Synthesizer,
        remote: Synthesizer,
        gate: SpeechEngineGate,
    ): SelectedSynthesizer = SelectedSynthesizer(
        onDevice = onDevice,
        remote = remote,
        engineGate = gate,
        scope = CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob()),
    )

    /** Defaults to "the platform recognizer works" — most routing tests don't care about the
     * fallback and would otherwise all have to say so. */
    private fun available(isAvailable: Boolean = true): SpeechRecognitionAvailability =
        SpeechRecognitionAvailability { isAvailable }

    private class FakeGate(
        stt: String = SpeechCatalog.ON_DEVICE,
        tts: String = SpeechCatalog.ON_DEVICE,
    ) : SpeechEngineGate {
        val sttSelection = MutableStateFlow(stt)
        val ttsSelection = MutableStateFlow(tts)

        override fun selection(direction: SpeechDirection): Flow<String> = when (direction) {
            SpeechDirection.SpeechToText -> sttSelection
            SpeechDirection.TextToSpeech -> ttsSelection
        }
    }

    /** One script per `listen()` CALL is not needed here — no test drives two captures of the same
     * delegate — but the call COUNT is asserted, which is what distinguishes "routed here" from
     * "routed here and also started the other one". */
    private class RecordingTranscriber(private vararg val events: TranscriberEvent) : Transcriber {
        var listenCalls = 0
            private set

        override fun listen(): Flow<TranscriberEvent> {
            listenCalls++
            return flowOf(*events)
        }
    }

    private class RecordingSynthesizer(available: Boolean = true) : Synthesizer {
        val spoken = mutableListOf<Pair<String, String>>()
        var stopCalls = 0
            private set

        override val isAvailable: StateFlow<Boolean> = MutableStateFlow(available)

        // A SharedFlow, not a StateFlow: a StateFlow conflates and replays its last value, so a
        // collector attaching late would see a stale event and two identical events in a row would
        // collapse into one — both of which would make this fixture lie about the merge.
        private val _events = MutableSharedFlow<SynthEvent>(extraBufferCapacity = 8)
        override fun events(): Flow<SynthEvent> = _events.asSharedFlow()

        override fun speak(utteranceId: String, text: String) {
            spoken += utteranceId to text
        }

        override fun stop() {
            stopCalls++
        }

        fun emit(event: SynthEvent) {
            _events.tryEmit(event)
        }
    }
}
