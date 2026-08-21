package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.SpeechApi
import com.mewbo.aura.data.api.SpeechSynthesizeRequest
import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.voice.SpeechCapacityExhausted
import com.mewbo.aura.voice.SpeechGateway
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.toRequestBody
import retrofit2.HttpException

/**
 * The ONE place `/api/speech` is spoken to.
 *
 * It wears two faces on purpose. [SpeechGateway] is the narrow half `voice/` sees — synthesize and
 * transcribe, no DTOs — and [catalog] is the settings screen's half, mirroring
 * [ModelRepository.catalog]'s shape so the two pickers behave identically (fetched fresh, `null` on
 * failure, never persisted; only the SELECTION persists). One class rather than two because both
 * halves hit the same namespace with the same auth and would otherwise drift.
 *
 * **Unlike [ModelRepository] this does NOT cache.** The chat model catalog is fetched once per
 * process because it is read on nearly every screen; this one is read only when a speech picker
 * opens, so a cache would buy nothing and would pin a stale engine list across a gateway config
 * change for the life of the process.
 */
@Singleton
class SpeechRepository @Inject constructor(
    private val api: SpeechApi,
) : SpeechGateway {

    /**
     * Both directions' engines in one round trip, or `null` when the fetch fails.
     *
     * `null` is "we do not know", NOT "there are none": the picker keeps offering On device and
     * the caller shows a notice, exactly as `loadModelsIfNeeded` already does. Degrading a failed
     * fetch to an empty list would instead claim the server offers no speech engines, which is a
     * statement we have no evidence for.
     *
     * **Cost: `O(collection)`** in the gateway's model count, one request.
     */
    suspend fun catalog(): SpeechCatalog? {
        val response = runCatching { api.getSpeechCapabilities() }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() ?: return null
        return response.toCatalog()
    }

    /**
     * **Cost: `O(one record)` in [text]'s length**, ~7.9s cold and ~2.4s warm for a short sentence,
     * measured against the live gateway; server deadline 60s.
     *
     * Throws on failure, and translates exactly ONE status on the way out:
     * [SpeechCapacityExhausted] for a 503 that carries a `Retry-After`. That is the server saying
     * "too many speech calls at once, come back in N seconds" — transient, and worth one retry.
     * A 503 WITHOUT the header is `speech_unavailable`: the deployment is not configured for
     * speech at all, and retrying would stall a read for nothing. Keying on the header rather than
     * parsing the error envelope keeps the two apart with no body read on the failure path.
     */
    override suspend fun synthesize(modelId: String, text: String): ByteArray =
        try {
            api.synthesizeSpeech(SpeechSynthesizeRequest(text = text, model = modelId)).bytes()
        } catch (e: HttpException) {
            throw capacityExhaustedOrNull(e) ?: e
        }

    /** `null` unless this is the retryable 503 — see [synthesize]. A `Retry-After` we cannot parse
     * is treated as absent rather than as zero, so a malformed header can never spin a retry
     * loop with no delay. */
    private fun capacityExhaustedOrNull(e: HttpException): SpeechCapacityExhausted? {
        if (e.code() != HTTP_UNAVAILABLE) return null
        val seconds = e.response()?.headers()?.get(RETRY_AFTER)?.trim()?.toLongOrNull() ?: return null
        return SpeechCapacityExhausted(seconds.coerceIn(0, MAX_RETRY_AFTER_SECONDS))
    }

    /** **Cost: `O(one record)` in the audio duration**, which the caller caps. Throws on failure;
     * [com.mewbo.aura.voice.RemoteTranscriber] catches and maps to a `TranscriberError`. */
    override suspend fun transcribe(modelId: String, audio: ByteArray): String {
        val part = MultipartBody.Part.createFormData(
            AUDIO_PART,
            AUDIO_FILENAME,
            audio.toRequestBody(WAV_MEDIA_TYPE.toMediaType()),
        )
        return api.transcribeSpeech(part, modelId.toRequestBody(TEXT_MEDIA_TYPE.toMediaType())).text
    }

    private companion object {
        const val HTTP_UNAVAILABLE = 503
        const val RETRY_AFTER = "Retry-After"

        /** A ceiling on what the server can ask us to wait. It sends 5; honouring an absurd value
         * verbatim would hang a read on a header we do not control. */
        const val MAX_RETRY_AFTER_SECONDS = 30L

        /** The route reads `request.files.get("file")` and 400s on any other part name. */
        const val AUDIO_PART = "file"

        /** The EXTENSION is the gateway's format hint, so this name is load-bearing: the server
         * reads it before falling back to the part's mimetype. */
        const val AUDIO_FILENAME = "audio.wav"
        const val WAV_MEDIA_TYPE = "audio/wav"
        const val TEXT_MEDIA_TYPE = "text/plain"
    }
}
