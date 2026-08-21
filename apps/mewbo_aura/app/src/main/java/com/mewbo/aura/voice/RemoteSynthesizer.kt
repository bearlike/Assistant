package com.mewbo.aura.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.di.ApplicationScope
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * The server-backed [Synthesizer]: text out to `/api/speech/synthesis`, audio bytes back, played
 * through [MediaPlayer].
 *
 * The THIRD implementation of this seam, behind the same binding [PlatformSynthesizer] uses — a
 * peer, not a parallel path, so [SpeechController]'s sentence chunking, the read-aloud button and
 * the overlay speak-along all work unchanged the moment [SelectedSynthesizer] routes here.
 *
 * Three facts about the gateway shape everything below:
 *
 * - **Synthesis is slow — ~0.5s a sentence, ~4.0s a paragraph, measured.** That is why the queue is
 *   serial-but-pipelined-by-sentence rather than one call per message: [SpeechController] already
 *   hands us one sentence at a time, so audio starts after the first ~0.5s instead of after the
 *   whole reply. The wait is not silent to the user either — `SpeechController` sets its
 *   `speakingKey` at ENQUEUE time, so the read-aloud glyph lights the instant it is tapped and
 *   stays lit across the round trip.
 * - **One sentence is synthesized WHILE the previous one plays** ([pump]). Synthesis costs roughly
 *   a THIRD of the playback it feeds (measured: 240 chars → 3.98s synth / 12.40s playback; 600
 *   chars → 10.47s / 31.74s), so awaiting it only after playback ended put the whole of that third
 *   into the silence before every sentence. Running it under the previous sentence's audio hides
 *   it entirely at steady state. **Depth is exactly one, and never two calls at once:** the next
 *   synthesis starts only once the current sentence's audio is in hand, so this client occupies
 *   ONE of the backend's two shared TTS slots no matter how long the reply is.
 * - **The `Content-Type` is always `audio/mpeg` and always wrong** (the bytes are WAV or FLAC), so
 *   nothing here reads it. `MediaPlayer` sniffs the container itself, which is also why it is the
 *   right decoder rather than Media3 — that would be a whole new dependency for one short clip.
 * - **A failure must not wedge the caller.** Every failure path emits [SynthEvent.Error] for the
 *   utterance, which is what `SpeechController` needs to clear its speaking state; an exception
 *   escaping the pump would leave the glyph lit forever.
 */
@Singleton
class RemoteSynthesizer @Inject constructor(
    @ApplicationContext private val context: Context,
    private val gateway: SpeechGateway,
    private val engineGate: SpeechEngineGate,
    private val boost: SpeechVolumeBoost,
    @ApplicationScope private val scope: CoroutineScope,
) : Synthesizer {

    /**
     * Always `true`, and that is the honest answer rather than a lazy one.
     *
     * Whether a remote engine can synthesize is not knowable without asking it, and a `false` here
     * DISABLES the read-aloud control — so a single transient failure would remove the only
     * affordance that could retry, with no path back. Per-utterance [SynthEvent.Error] is where a
     * real failure surfaces, which is exactly the granularity at which it happened.
     */
    override val isAvailable: StateFlow<Boolean> = MutableStateFlow(true).asStateFlow()

    private val _events = MutableSharedFlow<SynthEvent>(extraBufferCapacity = EVENT_BUFFER)
    override fun events(): Flow<SynthEvent> = _events.asSharedFlow()

    /** Enqueued-but-unspoken utterances. Guarded by `this`, like [PlatformSynthesizer]'s own
     * `pending` — [speak], [stop] and the pump all touch it from different threads. */
    private val queue = ArrayDeque<PendingUtterance>()
    private var pumpJob: Job? = null

    /** `true` from the moment a pump is launched until it retires under the lock in
     * [pollNextOrRetire]. Distinct from `pumpJob.isActive` because the pump outlives an empty
     * queue — it is still playing the clip it dequeued — and a [speak] arriving in that window
     * must NOT start a second pump, which would play two clips at once. */
    private var pumping = false
    private var player: MediaPlayer? = null
    private var focusRequest: AudioFocusRequest? = null
    private val audioManager by lazy { context.getSystemService(Context.AUDIO_SERVICE) as AudioManager }

    /**
     * `true` once this run has failed and been stopped. Further utterances are refused rather
     * than queued — a still-streaming reply keeps calling [speak] after the failure, and without
     * the latch each one would restart the pump and re-fail against the same dead service, which
     * is the swiss-cheese read this whole mechanism exists to prevent. Cleared by [stop], because
     * a barge-in IS the next run beginning.
     */
    private var runFailed = false

    /** QUEUE_ADD semantics, matching [Synthesizer]'s contract: appends, never interrupts. */
    @Synchronized
    override fun speak(utteranceId: String, text: String) {
        if (runFailed) {
            // Refused, but still ANSWERED. `SpeechController` clears its speaking state only on
            // an event for its own `lastEnqueuedId`, so dropping an utterance silently would
            // leave the read-aloud glyph lit forever on whatever sentence happened to be last.
            _events.tryEmit(SynthEvent.Error(utteranceId))
            return
        }
        queue += PendingUtterance(utteranceId, text)
        // `pumping`, not `pumpJob.isActive`: the pump now holds a clip it has already dequeued
        // while that clip plays, so "is the job running" and "is anything still owed audio" stopped
        // being the same question. Retirement is [pollNextOrRetire]'s job, under this same lock.
        if (!pumping) {
            pumping = true
            pumpJob = scope.launch { pump() }
        }
    }

    /**
     * Idempotent flush — the barge-in path, and it must be fast (`voice/CLAUDE.md`: ≤200ms).
     *
     * Cancelling [pumpJob] alone is not enough: [MediaPlayer] keeps playing on its own thread, so
     * it is stopped explicitly. Nothing is emitted for the dropped utterances — a barge-in is the
     * caller discarding them, and `SpeechController.bargeIn` has already cleared its own state.
     */
    @Synchronized
    override fun stop() {
        queue.clear()
        pumpJob?.cancel()
        pumpJob = null
        pumping = false
        runFailed = false
        releasePlayer()
        abandonAudioFocus()
        boost.release()
    }

    /**
     * Dequeue for the READ-AHEAD, without retiring on empty.
     *
     * An empty queue here means nothing yet — a streaming reply routinely has not produced its next
     * sentence while the current one is playing. Retiring on it would let a sentence arriving
     * mid-playback start a SECOND pump alongside the clip this one is still playing, which is two
     * voices at once.
     */
    @Synchronized
    private fun pollNext(): PendingUtterance? = queue.removeFirstOrNull()

    /**
     * Dequeue, or `null` having retired this pump — one atomic step, under the lock.
     *
     * Only ever called with nothing playing and nothing held, so an empty queue genuinely means
     * this pump has no work left. Deciding and retiring TOGETHER is what makes it safe: two
     * separate calls (`isEmpty()` then `return`) would leave a window where [speak] sees
     * `pumping == true` and appends, while the pump has already decided to exit — that utterance
     * is then never spoken and never answered with an event, which strands the read-aloud glyph
     * lit.
     */
    @Synchronized
    private fun pollNextOrRetire(): PendingUtterance? {
        val next = queue.removeFirstOrNull()
        if (next == null) pumping = false
        return next
    }

    /**
     * Playback is strictly serial; synthesis runs ONE clip ahead of it.
     *
     * The gap this closes: awaiting a sentence's synthesis only after the previous sentence's
     * audio had finished put the whole round trip into the silence between them, every time.
     * Fetching sentence N+1 while N plays spends that time against audio the listener is already
     * hearing.
     *
     * **Read-ahead depth is exactly one, structurally.** The queue is polled once per iteration, so
     * at most one [synthesize] is ever in flight — this client holds ONE of the gateway's two
     * shared TTS slots however long the reply runs. That bound is not just politeness: the backend
     * serves 2 in parallel and a third concurrent request measurably queues, so a deeper pipeline
     * would slow the very gap it was meant to close, for every caller at once. And exactly one clip
     * is ever PLAYING: `async` fetches bytes and nothing else, while [play] is only ever reached
     * from this one sequential loop.
     *
     * `coroutineScope` is what makes barge-in still correct: the read-ahead is a CHILD of this
     * coroutine, so `stop()` cancelling [pumpJob] cancels a synthesis in flight with it. A
     * prefetched clip cannot outlive the run that asked for it.
     */
    private suspend fun pump() {
        // The model id is read ONCE per pump run, not per utterance: a selection change landing
        // between two sentences of one reply would otherwise finish the message in a different
        // voice than it started in.
        val modelId = engineGate.selection(SpeechDirection.TextToSpeech).first()
        coroutineScope {
            fun fetch(utterance: PendingUtterance) = utterance to async { synthesize(modelId, utterance) }

            var current = pollNextOrRetire()?.let(::fetch) ?: return@coroutineScope
            while (true) {
                val (utterance, clipAhead) = current
                val clip = clipAhead.await()
                if (clip == null) {
                    endRun(utterance, readAhead = null)
                    return@coroutineScope
                }
                // Started BEFORE this clip plays — the whole point of the read-ahead. Empty here
                // means "not written yet" on a streaming reply, NOT "nothing more is coming", so
                // the queue is asked again after playback.
                val next = pollNext()?.let(::fetch)
                // Focus is held while a read-ahead exists, or a multi-sentence reply would duck and
                // unduck other apps' audio between every sentence. With none, playback is the last
                // thing keeping this run alive, so focus is abandoned as it ends.
                if (!playClip(utterance, clip, lastOfRun = next == null)) {
                    endRun(utterance, readAhead = next)
                    return@coroutineScope
                }
                // Retire only HERE, with nothing playing and nothing held — the one moment an empty
                // queue really does mean this pump is done.
                current = next ?: pollNextOrRetire()?.let(::fetch) ?: return@coroutineScope
            }
        }
    }

    /**
     * Fetch one clip, or `null` when this run must not continue.
     *
     * The retry loop is bounded by [SpeechQueueOutcome], which owns the whole "is this worth
     * trying again" rule; this method only carries it out. `attempt` is 1-based so the decision
     * reads the way it is stated: only the FIRST failure can earn a retry. A retry's wait now
     * happens under the previous sentence's playback too, so a single capacity refusal need not be
     * audible at all.
     */
    private suspend fun synthesize(modelId: String, utterance: PendingUtterance): File? {
        var attempt = 1
        while (true) {
            try {
                val clip = writeClip(gateway.synthesize(modelId, utterance.text))
                // A barge-in landing between the write and the pump consuming this clip leaves a
                // file nothing will ever play — the old shape's `finally` deleted it, and losing
                // that on the read-ahead path would leak one clip per barge-in into the cache dir.
                try {
                    currentCoroutineContext().ensureActive()
                } catch (e: CancellationException) {
                    clip.delete()
                    throw e
                }
                return clip
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                when (val outcome = SpeechQueueOutcome.forFailure(e, attempt)) {
                    is SpeechQueueOutcome.RetryAfter -> {
                        delay(outcome.delayMillis)
                        attempt++
                    }
                    SpeechQueueOutcome.StopRun -> return null
                }
            }
        }
    }

    /**
     * Play one already-fetched clip. `false` when this run must not continue.
     *
     * Catches rather than throws for the same reason [synthesize] returns `null`: an undecodable
     * clip is this utterance's failure, and an exception escaping the pump would end the run with
     * no [SynthEvent] at all — which is precisely the silent drain that strands the read-aloud
     * glyph lit. It does NOT emit that [SynthEvent.Error] itself — [endRun] is the single owner of
     * answering utterances that will never be spoken, so a failure here cannot double-emit for the
     * same id.
     */
    private suspend fun playClip(utterance: PendingUtterance, clip: File, lastOfRun: Boolean): Boolean {
        try {
            requestAudioFocus()
            _events.tryEmit(SynthEvent.Started(utterance.id))
            play(clip)
            _events.tryEmit(SynthEvent.Done(utterance.id))
            return true
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            return false
        } finally {
            clip.delete()
            if (lastOfRun) abandonAudioFocus()
        }
    }

    /**
     * End the run, answering EVERY utterance that will now never be spoken.
     *
     * [readAhead] is the one the pump had already dequeued for prefetching, so [failRun]'s own
     * drain cannot see it — and an utterance dropped without a [SynthEvent.Error] is exactly what
     * leaves `SpeechController`'s speaking state stuck. Ordered failed-then-read-ahead-then-queue,
     * matching the order they were enqueued in.
     *
     * Its clip is deleted whichever way the fetch landed: one already written is deleted here, and
     * one still in flight deletes its own on [synthesize]'s cancellation check. Nothing else will
     * ever consume it, and a temp file per failed run is a leak that only shows up as a full cache.
     */
    @OptIn(ExperimentalCoroutinesApi::class)
    private fun endRun(failed: PendingUtterance, readAhead: Pair<PendingUtterance, Deferred<File?>>?) {
        _events.tryEmit(SynthEvent.Error(failed.id))
        readAhead?.let { (queued, clipAhead) ->
            if (clipAhead.isCompleted && !clipAhead.isCancelled) clipAhead.getCompleted()?.delete()
            clipAhead.cancel()
            _events.tryEmit(SynthEvent.Error(queued.id))
        }
        failRun()
    }

    /**
     * End the run cleanly rather than skipping the failed sentence and carrying on.
     *
     * **A gap is worse than a stop.** Continuing past a failure means the listener hears a
     * sentence disappear from the middle of a reply with nothing to indicate it happened — they
     * are simply told something untrue by omission. Stopping is at least legible: the audio ends,
     * and the text is on screen to read.
     *
     * Every dropped utterance is answered with an [SynthEvent.Error] rather than discarded.
     * `SpeechController` clears its speaking state only on an event matching its own
     * `lastEnqueuedId`, so a silent drain would strand the read-aloud glyph lit on a sentence
     * that is never coming.
     */
    @Synchronized
    private fun failRun() {
        runFailed = true
        // The pump is about to return. Leaving this set would refuse to start a new one after the
        // `runFailed` latch clears on the next `stop()`, silencing every later read.
        pumping = false
        while (true) {
            val dropped = queue.removeFirstOrNull() ?: break
            _events.tryEmit(SynthEvent.Error(dropped.id))
        }
        releasePlayer()
        abandonAudioFocus()
        boost.release()
    }

    /** [MediaPlayer] reads a file or a descriptor, never a byte array — so the clip lands in the
     * cache dir for the length of one utterance and is deleted in [speakOne]'s `finally`. */
    private fun writeClip(audio: ByteArray): File =
        File.createTempFile(CLIP_PREFIX, CLIP_SUFFIX, context.cacheDir).apply { writeBytes(audio) }

    private suspend fun play(clip: File) = suspendCancellableCoroutine { continuation ->
        val media = MediaPlayer().apply {
            // BEFORE `setDataSource` — `setAudioSessionId` throws once a source is set. Routing
            // every clip of a reply through the ONE id [SpeechVolumeBoost] holds is what lets a
            // single effect span the whole answer instead of being rebuilt per sentence.
            // `setVolume` is deliberately NOT the mechanism: it reaches the same `AudioTrack` gain
            // that clamps at 1.0f, so it can only ever attenuate.
            boost.sessionId()?.let { setAudioSessionId(it) }
            setAudioAttributes(speechAttributes())
            setDataSource(clip.absolutePath)
            setOnCompletionListener { if (continuation.isActive) continuation.resume(Unit) }
            setOnErrorListener { _, _, _ ->
                // Handled by resuming rather than throwing: a clip that will not decode is this
                // utterance's failure, and `speakOne` turns a plain return into a Done. Reported
                // as an error instead would be a lie about a clip that may simply have ended.
                if (continuation.isActive) continuation.resume(Unit)
                true
            }
            prepare()
            start()
        }
        synchronized(this@RemoteSynthesizer) { player = media }
        continuation.invokeOnCancellation { releasePlayer() }
    }

    @Synchronized
    private fun releasePlayer() {
        val media = player ?: return
        player = null
        // Separate blocks: `stop()` throws on a player in an unexpected state, and folding both
        // into one would skip `release()` and leak the codec + audio track.
        runCatching { media.stop() }
        runCatching { media.release() }
    }

    private fun speechAttributes(): AudioAttributes = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANT)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build()

    /** `TRANSIENT_MAY_DUCK` for the duration of speech, abandoned on stop/drain — the same
     * etiquette [PlatformSynthesizer] follows, so which engine is selected is inaudible to
     * whatever else is playing. */
    @Synchronized
    private fun requestAudioFocus() {
        if (focusRequest != null) return
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(speechAttributes())
            .build()
        audioManager.requestAudioFocus(request)
        focusRequest = request
    }

    @Synchronized
    private fun abandonAudioFocus() {
        focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        focusRequest = null
    }

    private data class PendingUtterance(val id: String, val text: String)

    private companion object {
        const val EVENT_BUFFER = 16
        const val MILLIS_PER_SECOND = 1_000L
        const val CLIP_PREFIX = "mewbo-speech"
        const val CLIP_SUFFIX = ".audio"
    }
}
