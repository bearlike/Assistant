package com.mewbo.aura.voice

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.mewbo.aura.data.model.SpeechDirection
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.math.log10
import kotlin.math.sqrt
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn

/**
 * The server-backed [Transcriber]: capture with [AudioRecord], endpoint locally, upload the whole
 * utterance to `/api/speech/transcription`, emit one [TranscriberEvent.Final].
 *
 * ## ⚠️ UNVERIFIED AGAINST A LIVE GATEWAY
 *
 * **No call on this path has ever succeeded.** The gateway's transcription credential is invalid,
 * so every request fails in ~5s; the code below was written to a contract that is itself inferred
 * ([com.mewbo.aura.data.api.SpeechModelsResponseDto]). What HAS been reasoned through carefully is
 * the failure shape — a failed transcription emits [TranscriberError] and ends the flow, so the
 * caller returns to idle and the mic is never left open. On-device recognition is unaffected and
 * remains the default.
 *
 * ## Two behaviours that differ from [SpeechRecognizerTranscriber], both structural
 *
 * The transport is request/response — this client has no WebSocket and deliberately adds none — so
 * there is no channel over which interim results could arrive. Everything follows from that:
 *
 * - **No [TranscriberEvent.Partial], ever.** The composer shows an amplitude waveform while
 *   capturing and the text appears at the end, rather than building up word by word.
 * - **Stopping by hand keeps nothing.** `ChatViewModel.stopDictation` salvages the last partial;
 *   with no partials there is nothing to salvage, so a manual stop discards the utterance. Ending
 *   by falling silent is the path that produces a transcript.
 *
 * ## Why `AudioRecord` + WAV rather than `MediaRecorder` + AAC — decided, not defaulted
 *
 * `MediaRecorder` writing `.m4a` would be the obvious choice and produces a file about a quarter
 * the size. It is rejected because **this class needs the raw sample buffers for two separate
 * jobs, and `MediaRecorder` does not hand them over**: it exposes only a polled
 * `getMaxAmplitude()`. Those jobs are the `Rms` events that drive the orb's listening animation
 * and the composer waveform, and the silence endpointer below — which is not a nicety here but
 * the ONLY thing that decides an utterance is finished, since there is no recognizer to decide it
 * for us.
 *
 * The cost is real and bounded: uncompressed 16-bit PCM runs ~32 KB/s, so the 20s cap is ~640 KB
 * against a 10 MiB server limit — about 6%. Trading the endpointer to save ~480 KB on a
 * twenty-second clip is a bad trade, and the gateway transcribes WAV in 0.24s measured. **Anyone
 * revisiting this for bandwidth must replace the endpointer and the amplitude feed first**, not
 * merely swap the encoder.
 *
 * A third interaction is worth knowing and is NOT fixed here: `AssistTurnMachine` resets its 8s
 * silence timer only on `Ready` and on a CHANGED `Partial` (deliberately never on `Rms`, which
 * fires continuously on real hardware). With no partials nothing resets it, so on the OVERLAY path
 * an utterance running past ~8s from `Ready` is cut off before this endpointer finishes. Chat
 * dictation has no such timer and is unaffected. Widening that rule means editing a
 * contract-tested law in `AssistTurnMachine` on a path nobody has been able to exercise yet, which
 * is the wrong order — [MAX_CAPTURE_MS] instead keeps this side's own bound honest.
 */
@Singleton
class RemoteTranscriber @Inject constructor(
    private val gateway: SpeechGateway,
    private val engineGate: SpeechEngineGate,
) : Transcriber {

    /**
     * Cold, exactly like the platform impl: collecting starts capture, cancelling the collector
     * stops it and releases the microphone. `flowOn(IO)` because [AudioRecord.read] blocks — unlike
     * [SpeechRecognizerTranscriber], which must be driven from the MAIN thread, this one must NOT
     * be.
     */
    override fun listen(): Flow<TranscriberEvent> = flow {
        val modelId = engineGate.selection(SpeechDirection.SpeechToText).first()
        val recorder = createRecorder()
        if (recorder == null) {
            // Mirrors the platform impl's `isRecognitionAvailable` guard: a missing RECORD_AUDIO
            // grant or a mic held by another app closes the flow cleanly instead of throwing, and
            // both consumers already map Unavailable onto a disabled mic.
            emit(TranscriberEvent.Error(TranscriberError.Unavailable))
            return@flow
        }

        val captured = ByteArrayOutputStream()
        try {
            recorder.startRecording()
            emit(TranscriberEvent.Ready)

            val buffer = ShortArray(READ_SAMPLES)
            var heardSpeech = false
            var silentMs = 0L
            var elapsedMs = 0L

            while (elapsedMs < MAX_CAPTURE_MS) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read <= 0) break
                captured.write(buffer.toLittleEndianBytes(read))

                val amplitude = buffer.normalizedRms(read)
                emit(TranscriberEvent.Rms(amplitude.toRecognizerDb()))

                val chunkMs = read * MILLIS_PER_SECOND / SAMPLE_RATE
                elapsedMs += chunkMs
                if (amplitude >= SPEECH_AMPLITUDE) {
                    heardSpeech = true
                    silentMs = 0
                } else {
                    silentMs += chunkMs
                }
                // End of speech: quiet for long enough AFTER something was actually said. Before
                // any speech the silence budget is longer, so a slow start is not read as an
                // utterance that already ended.
                val budget = if (heardSpeech) TRAILING_SILENCE_MS else LEAD_IN_SILENCE_MS
                if (silentMs >= budget) break
            }

            if (!heardSpeech) {
                // The platform recognizer's own quiet-cancel: nothing was said, so there is
                // nothing to upload and nothing to charge the gateway for.
                emit(TranscriberEvent.Error(TranscriberError.NoMatch))
                return@flow
            }

            val transcript = runCatching { gateway.transcribe(modelId, captured.toByteArray().asWav()) }
            val text = transcript.getOrNull()
            when {
                // ServiceFailed, never Other: the user spoke, the recording was captured, and it
                // is now being thrown away for a reason nothing on screen would otherwise show.
                // Every other code here is a quiet-cancel because the user can see its cause for
                // themselves; this one they cannot, and the remedy is a setting they chose. This
                // is the ONLY path that reaches the gateway, so it is the only one that can fail
                // this way — and today it is the path every server-STT call takes, because the
                // gateway's transcription credential is dead.
                text == null -> emit(TranscriberEvent.Error(TranscriberError.ServiceFailed))
                text.isBlank() -> emit(TranscriberEvent.Error(TranscriberError.NoMatch))
                else -> emit(TranscriberEvent.Final(text))
            }
        } finally {
            // Two separate runCatching blocks, not one: `stop()` throws IllegalStateException on a
            // recorder that never started, and a single block would then skip `release()` and leak
            // the microphone for the life of the process.
            runCatching { recorder.stop() }
            runCatching { recorder.release() }
        }
    }.flowOn(Dispatchers.IO)

    /** `null` when the recorder cannot be built or initialised — a missing RECORD_AUDIO grant is
     * the common cause and shows up as `STATE_UNINITIALIZED`, not as an exception. */
    @SuppressLint("MissingPermission")
    private fun createRecorder(): AudioRecord? {
        val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuffer <= 0) return null
        val recorder = runCatching {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE,
                CHANNEL,
                ENCODING,
                minBuffer * BUFFER_FACTOR,
            )
        }.getOrNull() ?: return null
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            return null
        }
        return recorder
    }

    private companion object {
        /** What every speech gateway expects and what recognition sources are tuned for; also
         * keeps a 20s utterance at ~640KB, which is a reasonable thing to upload. */
        const val SAMPLE_RATE = 16_000
        const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
        const val BUFFER_FACTOR = 2
        const val MILLIS_PER_SECOND = 1_000

        /** ~64ms a read at 16kHz — frequent enough that the waveform animates smoothly and the
         * endpointer's resolution stays well under [TRAILING_SILENCE_MS]. */
        const val READ_SAMPLES = 1024

        /** Normalized RMS above which a chunk counts as speech. Empirical, not measured on a real
         * device (see the class KDoc) — deliberately low, because failing to notice speech ends
         * the capture early, which is the worse of the two errors. */
        const val SPEECH_AMPLITUDE = 0.02f

        /** Quiet-after-speech that ends the utterance. Long enough to survive the pause between
         * two sentences, short enough that the round trip still fits inside the overlay's own 8s
         * budget for a short utterance. */
        const val TRAILING_SILENCE_MS = 1_200L

        /** Quiet BEFORE any speech. Longer, so a user who takes a moment to start is not cut off. */
        const val LEAD_IN_SILENCE_MS = 6_000L

        /**
         * Hard cap: a bound on the upload, and the only thing standing between a stuck endpointer
         * and an unbounded recording.
         *
         * **This is what keeps the request inside the server's own 10 MiB limit, and the margin is
         * wide on purpose.** 20s × 16kHz × 2 bytes ≈ 640 KB, about 6% of the cap — so the DURATION
         * bound always binds first and the byte cap is never the thing that refuses a request.
         * That is why `capabilities.transcription.limits.max_audio_bytes` is not read here: a
         * value plumbed through but incapable of changing any decision reads as a live check while
         * being dead code. **Raise this constant and that stops being true** — 5 minutes of audio
         * is ~9.6 MB and lands right on the limit, so anything past ~3 minutes needs the server's
         * published cap consulted for real, or chunked uploads.
         *
         * **Splitting one utterance into several uploads buys nothing, measured.** Transcription
         * latency is flat in audio length — 48x the audio costs 3.5x the time (0.21s at 5s,
         * 0.73s at 240s), and the trial-to-trial spread is wider than that trend, because the
         * backend is batch ASR running far faster than realtime and the residual slope is upload
         * bytes rather than compute. So there is no wait to shorten, while N segments would spend
         * the server's concurrency budget N times for one dictation. The endpointer above has
         * also already cut on the only silence there was; a further cut lands mid-utterance,
         * where by construction no boundary signal exists.
         */
        const val MAX_CAPTURE_MS = 20_000L

        /**
         * [SpeechRecognizer][android.speech.SpeechRecognizer]'s `onRmsChanged` range, which
         * `ui/chat/DictationDecision` and the orb's listening animation are both calibrated
         * against. Restated here rather than shared because the alternative is a `voice/` → `ui/`
         * import, which the app's layering forbids outright; the two consumers of an `Rms` event
         * must agree on its units, so a remote engine emitting raw dBFS would render as a
         * permanently flat waveform.
         */
        const val RECOGNIZER_MIN_DB = -2f
        const val RECOGNIZER_MAX_DB = 10f

        /** dBFS window mapped onto the range above: about the quietest room tone up to a normal
         * speaking voice close to the mic. */
        const val QUIET_DBFS = -45f
        const val LOUD_DBFS = -5f

        const val PCM16_FULL_SCALE = 32_768f

        // RIFF/WAVE header fields. [CHANNELS] restates [CHANNEL] as the number the header wants;
        // the two must agree, and they are three lines apart so that they do.
        const val WAV_HEADER_BYTES = 44

        /** The RIFF chunk's declared size covers everything after its own size field: the 44-byte
         * header less `RIFF` and the 4 size bytes. */
        const val RIFF_CHUNK_OVERHEAD = 36
        const val PCM_SUBCHUNK_SIZE = 16
        const val PCM_FORMAT: Short = 1
        const val CHANNELS = 1
        const val BITS_PER_SAMPLE = 16
    }

    /** Root-mean-square of one read, as 0f..1f of full scale. */
    private fun ShortArray.normalizedRms(count: Int): Float {
        if (count <= 0) return 0f
        var sumOfSquares = 0.0
        for (i in 0 until count) {
            val sample = this[i].toDouble()
            sumOfSquares += sample * sample
        }
        return (sqrt(sumOfSquares / count) / PCM16_FULL_SCALE).toFloat().coerceIn(0f, 1f)
    }

    /** 0f..1f amplitude → the dB-ish number the rest of the app already understands. */
    private fun Float.toRecognizerDb(): Float {
        if (this <= 0f) return RECOGNIZER_MIN_DB
        val dbfs = (20f * log10(this)).coerceIn(QUIET_DBFS, LOUD_DBFS)
        val position = (dbfs - QUIET_DBFS) / (LOUD_DBFS - QUIET_DBFS)
        return RECOGNIZER_MIN_DB + position * (RECOGNIZER_MAX_DB - RECOGNIZER_MIN_DB)
    }

    private fun ShortArray.toLittleEndianBytes(count: Int): ByteArray =
        ByteBuffer.allocate(count * Short.SIZE_BYTES)
            .order(ByteOrder.LITTLE_ENDIAN)
            .apply { for (i in 0 until count) putShort(this@toLittleEndianBytes[i]) }
            .array()

    /**
     * Wraps raw PCM in a 44-byte RIFF/WAVE header.
     *
     * The gateway is handed a CONTAINER rather than bare samples because bare samples carry no
     * sample rate or channel count — a decoder that guesses wrong transcribes chipmunk audio and
     * returns plausible nonsense rather than an error, which is the failure mode that would take
     * longest to recognise.
     */
    private fun ByteArray.asWav(): ByteArray {
        val header = ByteBuffer.allocate(WAV_HEADER_BYTES).order(ByteOrder.LITTLE_ENDIAN)
        val byteRate = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE / Byte.SIZE_BITS
        header.put("RIFF".toByteArray(Charsets.US_ASCII))
        header.putInt(RIFF_CHUNK_OVERHEAD + size)
        header.put("WAVE".toByteArray(Charsets.US_ASCII))
        header.put("fmt ".toByteArray(Charsets.US_ASCII))
        header.putInt(PCM_SUBCHUNK_SIZE)
        header.putShort(PCM_FORMAT)
        header.putShort(CHANNELS.toShort())
        header.putInt(SAMPLE_RATE)
        header.putInt(byteRate)
        header.putShort((CHANNELS * BITS_PER_SAMPLE / Byte.SIZE_BITS).toShort())
        header.putShort(BITS_PER_SAMPLE.toShort())
        header.put("data".toByteArray(Charsets.US_ASCII))
        header.putInt(size)
        return header.array() + this
    }
}
