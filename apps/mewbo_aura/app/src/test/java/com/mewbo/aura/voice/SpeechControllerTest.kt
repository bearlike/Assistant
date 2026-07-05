package com.mewbo.aura.voice

import com.mewbo.aura.data.model.ChatItem
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit-tests [SpeechController] against a scripted [RecordingSynthesizer] test double, local to
 * this file rather than the debug-variant `FakeSynthesizer` (same convention as
 * [com.mewbo.aura.voice.AssistTurnMachineTest]'s own local fakes: the real `FakeSynthesizer` runs
 * genuine wall-clock `delay()`s on its own `CoroutineScope`, which doesn't integrate with
 * `runTest`'s virtual time / `backgroundScope`). A [Channel]-backed event stream (not a
 * `MutableSharedFlow`) guarantees delivery regardless of exactly when [SpeechController]'s own
 * background collector starts, mirroring the real [Synthesizer.speak] contract's own "true
 * QUEUE_ADD ordering" comment.
 *
 * Covers Gitea #180 P3 (speak-along wiring: modality/mute gating, reconciliation, the flicker guard
 * and its out-of-order mirror-image race, barge-in) and P4 (read-aloud markdown fidelity via the
 * shared [com.mewbo.aura.voice.SentenceChunker] pipeline).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SpeechControllerTest {

    private class RecordingSynthesizer : Synthesizer {
        val spoken = mutableListOf<Pair<String, String>>()
        var stopCount = 0
        override val isAvailable = MutableStateFlow(true)
        private val events = Channel<SynthEvent>(Channel.UNLIMITED)
        override fun speak(utteranceId: String, text: String) {
            spoken += utteranceId to text
        }
        override fun stop() {
            stopCount++
        }
        override fun events(): Flow<SynthEvent> = events.receiveAsFlow()
        fun emitDone(id: String) {
            check(events.trySend(SynthEvent.Done(id)).isSuccess)
        }
    }

    private fun message(key: String, text: String, isStreaming: Boolean) =
        ChatItem.AssistantMessage(text = text, isStreaming = isStreaming, ts = "t", key = key)

    // ---- onAssistantMessage: modality/mute gating (Gitea #180 P3: "text turns stay completely silent") ----

    @Test
    fun `a Text-modality turn never enqueues an utterance`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        controller.onAssistantMessage(message("assistant:1", "Hello there. ", isStreaming = true), InputModality.Text, muted = false)
        controller.onAssistantMessage(message("assistant:1", "Hello there.", isStreaming = false), InputModality.Text, muted = false)

        assertTrue(synth.spoken.isEmpty())
        assertNull(controller.speakingKey.value)
    }

    @Test
    fun `a muted conversation never enqueues an utterance even for a Voice turn`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        controller.onAssistantMessage(message("assistant:1", "Hello there.", isStreaming = false), InputModality.Voice, muted = true)

        assertTrue(synth.spoken.isEmpty())
    }

    // ---- onAssistantMessage: ordering + reconciliation ----

    @Test
    fun `voice-turn streaming deltas enqueue ordered stable-id utterances, reconciliation never re-speaks`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = true), InputModality.Voice, muted = false)
        // Reconciliation: a mid-word partial, then the same clause rewritten - must not re-speak
        // "Hello world." (position-based skip, not content-based - see SentenceChunkerTest).
        controller.onAssistantMessage(message("assistant:1", "Hello world. How are", isStreaming = true), InputModality.Voice, muted = false)
        controller.onAssistantMessage(message("assistant:1", "Hello world. How are you? ", isStreaming = true), InputModality.Voice, muted = false)
        controller.onAssistantMessage(
            message("assistant:1", "Hello world. How are you? Almost done", isStreaming = false),
            InputModality.Voice,
            muted = false,
        )

        assertEquals(
            listOf(
                "assistant:1:0" to "Hello world.",
                "assistant:1:1" to "How are you?",
                "assistant:1:2" to "Almost done",
            ),
            synth.spoken,
        )
    }

    @Test
    fun `the terminal fold flushes the trailing remainder exactly once`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        controller.onAssistantMessage(message("assistant:1", "One clause", isStreaming = false), InputModality.Voice, muted = false)

        assertEquals(listOf("assistant:1:0" to "One clause"), synth.spoken)
    }

    // ---- speakingKey lifecycle: the flicker guard and its out-of-order mirror-image race ----

    @Test
    fun `speakingKey stays set across a mid-turn pause between a sentence's Done and the next enqueue`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = true), InputModality.Voice, muted = false)
        assertEquals("assistant:1", controller.speakingKey.value)

        // Playback finishes the first sentence well before the LLM streams the next one - the turn
        // is still marked streaming (moreComing true), so this must NOT clear speakingKey (the
        // multi-sentence flicker this guards against).
        synth.emitDone("assistant:1:0")
        runCurrent()

        assertEquals("assistant:1", controller.speakingKey.value)
    }

    @Test
    fun `speakingKey clears once the terminal utterance's Done arrives AFTER the turn closes (in-order)`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        controller.onAssistantMessage(message("assistant:1", "One clause", isStreaming = false), InputModality.Voice, muted = false)
        assertEquals("assistant:1", controller.speakingKey.value)

        synth.emitDone("assistant:1:0")
        runCurrent()

        assertNull(controller.speakingKey.value)
    }

    @Test
    fun `speakingKey clears once the turn closes, even if the terminal Done already fired earlier (out-of-order race)`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = true), InputModality.Voice, muted = false)
        assertEquals("assistant:1", controller.speakingKey.value)

        // Done for the only utterance enqueued so far arrives WHILE the turn is still marked
        // streaming - both async sources (the SSE fold and the TTS Done callback) land on the same
        // dispatcher, and real-world timing can order either one first. Ignored for now (guard
        // above), same as the in-order case.
        synth.emitDone("assistant:1:0")
        runCurrent()
        assertEquals("assistant:1", controller.speakingKey.value)

        // The terminal fold has nothing NEW to say (the whole reply was already that one sentence)
        // - without latching that the Done already fired, speakingKey would stay stuck non-null
        // forever here, since no future SynthEvent will ever arrive for this id.
        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = false), InputModality.Voice, muted = false)

        assertNull(controller.speakingKey.value)
    }

    // ---- bargeIn ----

    @Test
    fun `bargeIn stops the synthesizer, clears speakingKey, and is idempotent`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = true), InputModality.Voice, muted = false)

        controller.bargeIn()
        assertNull(controller.speakingKey.value)
        assertEquals(1, synth.stopCount)

        controller.bargeIn()
        assertEquals(2, synth.stopCount)
        assertNull(controller.speakingKey.value)
    }

    @Test
    fun `a barged-in turn's later deltas for the SAME key never resume speech`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = true), InputModality.Voice, muted = false)
        controller.bargeIn()
        synth.spoken.clear()

        // A stale still-streaming fold for the SAME key lands after the barge-in (e.g. the SSE
        // collector hadn't cancelled yet) - closedKey must reject it, not resume speaking.
        controller.onAssistantMessage(message("assistant:1", "Hello world. More.", isStreaming = false), InputModality.Voice, muted = false)

        assertTrue(synth.spoken.isEmpty())
        assertNull(controller.speakingKey.value)
    }

    // ---- primeAlreadySpoken (Gitea #181 fix wave, finding 1: cross-instance handoff replay) ----

    @Test
    fun `priming a completed message closes its key - a later fold for the SAME key never re-speaks it`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        // Mirrors ChatViewModel.bind()'s history replay: a fresh instance's FIRST contact with
        // this message is the already-finished text some OTHER SpeechController (the overlay's)
        // already spoke, not a live fold.
        controller.primeAlreadySpoken(message("assistant:1", "Hello world. Already spoken.", isStreaming = false), InputModality.Voice)

        // A stale/defensive re-fold of the SAME finished text (history is never really re-folded in
        // production, but this locks the closedKey contract down regardless).
        controller.onAssistantMessage(message("assistant:1", "Hello world. Already spoken.", isStreaming = false), InputModality.Voice, muted = false)

        assertTrue(synth.spoken.isEmpty())
        assertNull(controller.speakingKey.value)
    }

    @Test
    fun `priming a still-streaming message leaves it open - the next live fold speaks only the new remainder`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        // Mid-turn Expand: the text present at bind time is still mid-sentence.
        controller.primeAlreadySpoken(message("assistant:1", "Hello world. How are", isStreaming = true), InputModality.Voice)
        assertTrue("priming must never itself speak", synth.spoken.isEmpty())

        // The live stream (subscribeLive, reattached after priming) resumes delivering deltas for
        // the SAME message key - only the genuinely new remainder should ever reach the synthesizer.
        controller.onAssistantMessage(message("assistant:1", "Hello world. How are you? ", isStreaming = true), InputModality.Voice, muted = false)
        controller.onAssistantMessage(
            message("assistant:1", "Hello world. How are you? Almost done", isStreaming = false),
            InputModality.Voice,
            muted = false,
        )

        // Utterance indices start at 1, not 0: priming's own (discarded) push over "Hello world. "
        // already consumed index 0 - SentenceChunker.nextIndex is a monotonic per-instance counter,
        // unaffected by whether an emitted utterance was actually enqueued or thrown away.
        assertEquals(
            listOf(
                "assistant:1:1" to "How are you?",
                "assistant:1:2" to "Almost done",
            ),
            synth.spoken,
        )
    }

    @Test
    fun `priming is a no-op for a Text-modality binding - matches onAssistantMessage's own gate`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        controller.primeAlreadySpoken(message("assistant:1", "Hello world.", isStreaming = false), InputModality.Text)
        // If priming had wrongly closed/opened anything, a genuine Voice fold afterward would
        // behave differently than a totally fresh controller's first fold.
        controller.onAssistantMessage(message("assistant:1", "Hello world.", isStreaming = false), InputModality.Voice, muted = false)

        assertEquals(listOf("assistant:1:0" to "Hello world."), synth.spoken)
    }

    @Test
    fun `priming a null item is a no-op`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)

        controller.primeAlreadySpoken(null, InputModality.Voice)

        assertTrue(synth.spoken.isEmpty())
        assertNull(controller.speakingKey.value)
    }

    // ---- speakFinalized (P4 read-aloud fidelity) ----

    @Test
    fun `speakFinalized speaks a markdown-heavy fixture cleanly - no headers, no code-fence syntax`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        val markdown = "# Heading\n\nHere is code:\n```\nval x = 1\nprintln(x)\n```\n"

        controller.speakFinalized("msg1", markdown)

        val spokenText = synth.spoken.joinToString(" ") { it.second }
        assertFalse(spokenText.contains("#"))
        assertFalse(spokenText.contains("`"))
        assertTrue(spokenText.contains("Code block omitted."))
        assertTrue(spokenText.contains("Heading"))
    }

    @Test
    fun `speakFinalized never contends with a live voice-turn's own chunker (caller bargeIns first)`() = runTest {
        val synth = RecordingSynthesizer()
        val controller = SpeechController(synth, backgroundScope)
        controller.onAssistantMessage(message("assistant:1", "Hello world. ", isStreaming = true), InputModality.Voice, muted = false)

        // ChatViewModel.toggleReadAloud always bargeIns before speakFinalized (a manual read-aloud
        // tap during a live voice turn IS a barge-in) - mirrors that call-site contract.
        controller.bargeIn()
        synth.spoken.clear()
        controller.speakFinalized("older-msg", "Older reply.")

        assertEquals(listOf("older-msg:0" to "Older reply."), synth.spoken)
        assertEquals("older-msg", controller.speakingKey.value)
    }
}
