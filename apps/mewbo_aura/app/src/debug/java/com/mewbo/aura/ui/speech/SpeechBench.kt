package com.mewbo.aura.ui.speech

import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.voice.SentenceChunker
import com.mewbo.aura.voice.SpeechGateway
import com.mewbo.aura.voice.SynthEvent
import com.mewbo.aura.voice.Synthesizer
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * The debug TTS bench: type text, pick ANY engine the deployment offers plus the on-device one,
 * speak it, and read what actually happened.
 *
 * **It bypasses the SETTING, never the implementations.**
 * [com.mewbo.aura.voice.SelectedSynthesizer] and [com.mewbo.aura.voice.RemoteSynthesizer] are used
 * verbatim, wired against a [com.mewbo.aura.voice.SpeechEngineGate] backed by [selection] instead
 * of `SettingsStore` — so the queueing, the audio focus, the one retry and the playback are the
 * production path, and only the source of the model id differs. Nothing in this file synthesizes
 * anything; if it ever grows an HTTP call or a `MediaPlayer`, it has stopped testing the thing it
 * claims to test.
 *
 * **A bench-owned gate is the only way in, because the model id is read TWICE and the second read
 * is not the router's.** `SelectedSynthesizer` picks a delegate from the gate, and then
 * `RemoteSynthesizer.pump` reads the gate AGAIN for the model id it puts on the wire. Handing the
 * app-wide singleton a different selection would therefore route to the server leg and synthesize
 * with whatever Settings happens to hold — a bench reporting one engine's name over another
 * engine's latency.
 *
 * The one thing the [Synthesizer] seam cannot report is WHY a call failed: [SynthEvent.Error]
 * carries an id and nothing else, which is right for a read-aloud button and useless here.
 * [RecordingSpeechGateway] closes that by recording the call as it passes, rather than by widening
 * a production event.
 */
class SpeechBench(
    private val synthesizer: Synthesizer,
    private val selection: MutableStateFlow<String>,
    private val probe: RecordingSpeechGateway,
    private val loadCatalog: suspend () -> SpeechCatalog?,
    onDeviceEngineName: String,
    private val scope: CoroutineScope,
) {

    private val _state = MutableStateFlow(
        SpeechBenchState(onDeviceEngineName = onDeviceEngineName).withPlanFor(SpeechBenchState().text),
    )
    val state: StateFlow<SpeechBenchState> = _state.asStateFlow()

    /** The utterance ids of the run in flight, in queue order. Events for any other id belong to a
     * read-aloud elsewhere in the app — the on-device leg is the app-wide singleton, so its event
     * stream is genuinely shared and an unfiltered collector would settle on someone else's
     * sentence. */
    private var pendingIds: List<String> = emptyList()
    private val settledIds = mutableSetOf<String>()
    private var spokenAtNanos = 0L
    private var runCount = 0

    init {
        scope.launch { synthesizer.events().collect(::onSynthEvent) }
        // Availability follows the SELECTED engine (SelectedSynthesizer flat-maps it), which is
        // exactly the fact this screen has to state honestly: on AOSP the on-device leg has no
        // engine, and the bench must say so rather than accept a press that goes nowhere.
        scope.launch { synthesizer.isAvailable.collect { available -> _state.update { it.copy(engineAvailable = available) } } }
        reloadCatalog()
    }

    /** Re-plans on every keystroke. The plan is a pure string scan over a short text, and showing
     * it live is the point — the boundaries are what the user is here to see. */
    fun setText(text: String) = _state.update { it.copy(text = text).withPlanFor(text) }

    /** Picks the engine for the NEXT speak. A run already playing keeps its own engine — the
     * latch-until-`stop` rule `SelectedSynthesizer` enforces, which [speak] releases. */
    fun selectEngine(storedId: String) {
        selection.value = storedId
        _state.update { it.copy(selectedEngine = storedId) }
    }

    /**
     * Types a gateway model id the catalog did not offer, and selects it.
     *
     * **Not a convenience — it is the only way to reach the gateway on a deployment whose
     * capability document enumerates no models.** `available: true` with an empty `models` list is
     * a real, currently-live state (the gateway's model-discovery call is refused for the runtime
     * credential), and synthesis works perfectly against a model id named directly. A bench that
     * could only offer what the catalog enumerates would report "no gateway engines" about a
     * gateway that is serving audio, which is the wrong answer to the question it exists to ask.
     *
     * Blank falls back to on-device, matching [SpeechCatalog.ON_DEVICE]'s blank-means-local rule
     * rather than inventing a second empty-id meaning.
     */
    fun setManualEngine(modelId: String) {
        _state.update { it.copy(manualEngine = modelId) }
        selectEngine(modelId.trim())
    }

    /** Fetches the deployment's engine list. Retryable on demand, because a bench launched while
     * the gateway was down is the normal case here rather than an edge one. */
    fun reloadCatalog() {
        _state.update { it.copy(catalogLoading = true) }
        scope.launch {
            val catalog = loadCatalog()
            _state.update {
                it.copy(catalog = catalog ?: it.catalog, catalogLoading = false, catalogFailed = catalog == null)
            }
        }
    }

    /**
     * Chunks the text exactly as production read-aloud does, then queues every chunk.
     *
     * **`SentenceChunker` is reused, not re-implemented, and the call shape mirrors
     * `SpeechController.speakFinalized` exactly** — one throwaway chunker, `push(text)` then
     * `flush()`. That is what makes the plan on screen the REAL plan: a bench that split the text
     * its own way would show boundaries the app never uses. `SpeechController` itself is not
     * reused, because it owns speaking-key state for a chat message that does not exist here.
     *
     * The leading [Synthesizer.stop] is load-bearing rather than defensive: `SelectedSynthesizer`
     * latches its delegate for a whole run and clears the latch only on `stop`, so without it a
     * second press after switching engines would still be served by the first — the bench would
     * report one engine's name over another's latency, the very confusion it exists to remove.
     */
    fun speak() {
        val text = _state.value.text.trim()
        if (text.isEmpty()) return
        synthesizer.stop()
        probe.clear()
        val runId = "$UTTERANCE_PREFIX${runCount++}"
        val chunks = planChunks(runId, text)
        if (chunks.isEmpty()) return
        pendingIds = chunks.map { it.id }
        settledIds.clear()
        spokenAtNanos = System.nanoTime()
        _state.update { it.copy(chunks = chunks, outcome = SpeechBenchOutcome.Speaking(firstAudioMillis = null)) }
        // Queued in one pass, exactly as `SpeechController.enqueue` does. Sequential by the
        // Synthesizer QUEUE_ADD contract, so the gateway's 2-parallel limit is never hit by one
        // run — a queued number here would be the backend busy with someone ELSE's call.
        chunks.forEach { synthesizer.speak(it.id, it.text) }
    }

    /** Barge-in. Nothing is reported for the dropped utterances, matching production — a barge-in
     * is the caller discarding them, not a failure. */
    fun stop() {
        pendingIds = emptyList()
        settledIds.clear()
        synthesizer.stop()
        _state.update { it.copy(outcome = SpeechBenchOutcome.Idle) }
    }

    private fun onSynthEvent(event: SynthEvent) {
        val id = when (event) {
            is SynthEvent.Started -> event.id
            is SynthEvent.Done -> event.id
            is SynthEvent.Error -> event.id
        }
        if (id !in pendingIds) return
        when (event) {
            // FIRST audio of the run, not of each chunk: what a listener waits through is the gap
            // before anything is heard, and for a server engine that gap IS chunk one's round trip.
            is SynthEvent.Started -> _state.update {
                val outcome = it.outcome
                if (outcome is SpeechBenchOutcome.Speaking && outcome.firstAudioMillis == null) {
                    it.copy(outcome = SpeechBenchOutcome.Speaking(firstAudioMillis = sinceSpoken()))
                } else {
                    it
                }
            }
            is SynthEvent.Done -> recordChunk(id, failed = false)
            is SynthEvent.Error -> recordChunk(id, failed = true)
        }
    }

    /**
     * Folds one chunk's result in, and settles the run once every chunk has answered.
     *
     * A FAILED chunk settles the whole run immediately, because `RemoteSynthesizer` ends a run on
     * a failure rather than skipping ahead — the queue behind it is already being drained with
     * `Error`s, and waiting for all of them would report the last dropped chunk's timing as the
     * run's, which is meaningless.
     */
    private fun recordChunk(id: String, failed: Boolean) {
        if (!settledIds.add(id)) return
        val call = probe.callFor(id.substringAfterLast(CHUNK_SEPARATOR).toIntOrNull() ?: settledIds.size - 1)
        _state.update { current ->
            val chunks = current.chunks.map { chunk ->
                if (chunk.id == id) chunk.copy(call = call, failed = failed) else chunk
            }
            val done = failed || settledIds.size == pendingIds.size
            val firstAudio = (current.outcome as? SpeechBenchOutcome.Speaking)?.firstAudioMillis
            current.copy(
                chunks = chunks,
                outcome = if (!done) {
                    current.outcome
                } else if (failed) {
                    SpeechBenchOutcome.Failed(totalMillis = sinceSpoken(), call = call)
                } else {
                    SpeechBenchOutcome.Spoke(firstAudioMillis = firstAudio, totalMillis = sinceSpoken(), call = call)
                },
            )
        }
        if (failed) pendingIds = emptyList()
    }

    private fun sinceSpoken(): Long = (System.nanoTime() - spokenAtNanos) / NANOS_PER_MILLI

    companion object {
        /** Namespaced so a stray event from the app's own read-aloud can never be mistaken for a
         * bench utterance — production ids are `{messageId}:{sentenceIndex}`. */
        private const val UTTERANCE_PREFIX = "bench-"
        private const val CHUNK_SEPARATOR = ":"
        private const val NANOS_PER_MILLI = 1_000_000L

        /**
         * The chunk plan for [text], through the SAME [SentenceChunker] production uses.
         *
         * Pure, so the screen can show the plan before anything is spoken — which is the whole
         * point of displaying it. `push` then `flush` is `speakFinalized`'s exact sequence: `push`
         * yields completed sentences, `flush` the trailing clause with no terminal punctuation.
         *
         * Note the chunker STRIPS markdown, so a chunk's character count is of the SPOKEN text and
         * will differ from the source — which is itself worth seeing, since the server's own
         * `max_text_chars` cap is measured on what is sent.
         */
        fun planChunks(runId: String, text: String): List<SpeechChunk> {
            val chunker = SentenceChunker(runId)
            val utterances = chunker.push(text) + listOfNotNull(chunker.flush())
            return utterances.mapIndexed { index, utterance ->
                SpeechChunk(id = utterance.id, index = index, text = utterance.text)
            }
        }
    }
}

/**
 * Everything the bench screen renders.
 *
 * `catalog` survives a later FAILED refresh — dropping the engine list because the gateway blipped
 * would make the picker forget what the user was in the middle of testing, which is the same
 * "`null` means we do not know, not that there are none" posture `SpeechRepository.catalog` takes.
 */
data class SpeechBenchState(
    val text: String = DEFAULT_TEXT,
    val selectedEngine: String = SpeechCatalog.ON_DEVICE,
    val catalog: SpeechCatalog? = null,
    val catalogLoading: Boolean = false,
    val catalogFailed: Boolean = false,
    /** A hand-typed gateway model id, for the empty-catalog case — see
     * [SpeechBench.setManualEngine]. Held separately from [selectedEngine] so tapping a catalog
     * row does not erase what was typed. */
    val manualEngine: String = "",
    /** Which class the on-device leg actually resolved to in THIS build — `FakeSynthesizer` on a
     * device with no TTS engine. Named rather than described, because "on device" covering two
     * very different implementations is precisely what makes an AOSP result confusing. */
    val onDeviceEngineName: String = "",
    val engineAvailable: Boolean = false,
    /** The chunk plan for [text] — recomputed on every edit, then annotated with each chunk's own
     * gateway call as the run progresses. */
    val chunks: List<SpeechChunk> = emptyList(),
    val outcome: SpeechBenchOutcome = SpeechBenchOutcome.Idle,
) {
    val speaking: Boolean get() = outcome is SpeechBenchOutcome.Speaking

    /** Re-plans without speaking, so the boundaries are visible before the first press. */
    fun withPlanFor(source: String): SpeechBenchState =
        copy(chunks = SpeechBench.planChunks(PLAN_PREVIEW_ID, source.trim()))

    private companion object {
        /** Short on purpose: the gateway serves two synthesis calls in parallel and queues the
         * rest, so a long first probe reads as a broken deployment rather than a busy one. Two
         * sentences, so the chunk plan is never a single row that hides what chunking means. */
        const val DEFAULT_TEXT = "Testing one two three. This is the Mewbo speech bench."

        /** The preview plan is never spoken, so its ids never reach the synthesizer — a distinct
         * prefix keeps them from colliding with a real run's if that ever changed. */
        const val PLAN_PREVIEW_ID = "plan"
    }
}

/**
 * One chunk of the plan: what would be sent, and — once spoken — what came back for it.
 *
 * [text] is POST-markdown-strip, because that is what `SentenceChunker` hands the synthesizer and
 * therefore what is actually sent. Showing the source substring instead would misreport the length
 * the server's own cap is measured against.
 */
data class SpeechChunk(
    val id: String,
    val index: Int,
    val text: String,
    val call: SpeechCallRecord? = null,
    val failed: Boolean = false,
) {
    val charCount: Int get() = text.length
}

/** Where one bench run got to — a closed union, so a new state cannot be added without every
 * readout being asked what it should say about it. */
sealed interface SpeechBenchOutcome {

    data object Idle : SpeechBenchOutcome

    /** Enqueued. `firstAudioMillis` stays `null` until the engine reports it started; for a server
     * engine that gap IS the synthesis round trip. */
    data class Speaking(val firstAudioMillis: Long?) : SpeechBenchOutcome

    data class Spoke(
        val firstAudioMillis: Long?,
        val totalMillis: Long,
        val call: SpeechCallRecord?,
    ) : SpeechBenchOutcome

    data class Failed(val totalMillis: Long, val call: SpeechCallRecord?) : SpeechBenchOutcome
}

/**
 * One `/api/speech/synthesize` call as it passed through.
 *
 * The byte count is the only thing on this screen that distinguishes real audio from an empty 200,
 * which is why it is reported even on success. A null [failure] with zero bytes is a distinct,
 * legible result rather than an impossible one.
 *
 * **[networkMillis] is request-to-last-byte, and it CANNOT be split further from here.** The
 * gateway seam hands back a fully-read `ByteArray`, so connection setup, server-side synthesis and
 * body transfer are one number. Separating them would mean instrumenting OkHttp with an
 * `EventListener` on the shared client — production surface changed to serve a debug screen — so
 * the bench states the boundary it can see and labels what that number contains.
 */
data class SpeechCallRecord(
    val modelId: String,
    val networkMillis: Long,
    val audioBytes: Int,
    val failure: String?,
)

/**
 * A pass-through [SpeechGateway] that remembers the last call.
 *
 * It records; it never decides. `RemoteSynthesizer` still owns the request, the retry rule and the
 * playback — swapping this in changes nothing about what runs, which is the only basis on which it
 * is allowed to sit in the path at all.
 *
 * **The failure is the exception's own message, not its response body.** Reading a Retrofit error
 * body here would put a transport type in `ui/`, which the app's layering forbids; the message
 * already carries the status line, which is what separates a 400 from a 503 from a dead socket.
 */
class RecordingSpeechGateway(private val delegate: SpeechGateway) : SpeechGateway {

    /** Calls of the CURRENT run, in the order they were made. `RemoteSynthesizer`'s pump is
     * serial, so position N is chunk N — the only correspondence available, since the gateway
     * interface carries a model id and text but no utterance id. */
    private val calls = mutableListOf<SpeechCallRecord>()

    private val _lastCall = MutableStateFlow<SpeechCallRecord?>(null)
    val lastCall: StateFlow<SpeechCallRecord?> = _lastCall.asStateFlow()

    @Synchronized
    fun clear() {
        calls.clear()
        _lastCall.value = null
    }

    /** The call serving chunk [index], or `null` when the on-device leg served this run and no
     * gateway call was ever made. */
    @Synchronized
    fun callFor(index: Int): SpeechCallRecord? = calls.getOrNull(index)

    override suspend fun synthesize(modelId: String, text: String): ByteArray {
        val startedAt = System.nanoTime()
        try {
            val audio = delegate.synthesize(modelId, text)
            remember(record(modelId, startedAt, audio.size, failure = null))
            return audio
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            remember(record(modelId, startedAt, audioBytes = 0, failure = describe(e)))
            throw e
        }
    }

    override suspend fun transcribe(modelId: String, audio: ByteArray): String =
        delegate.transcribe(modelId, audio)

    @Synchronized
    private fun remember(call: SpeechCallRecord) {
        calls += call
        _lastCall.value = call
    }

    private fun record(modelId: String, startedAtNanos: Long, audioBytes: Int, failure: String?) = SpeechCallRecord(
        modelId = modelId,
        networkMillis = (System.nanoTime() - startedAtNanos) / NANOS_PER_MILLI,
        audioBytes = audioBytes,
        failure = failure,
    )

    private companion object {
        const val NANOS_PER_MILLI = 1_000_000L

        fun describe(e: Exception): String = "${e::class.simpleName}: ${e.message ?: "no message"}"
    }
}
