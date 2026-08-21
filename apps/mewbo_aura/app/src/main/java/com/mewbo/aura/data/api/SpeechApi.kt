package com.mewbo.aura.data.api

import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part

/**
 * The `/api/speech` surface, split out of [AuraApi] for ONE reason: **its calls need a different
 * timeout regime, and a timeout is a property of the client, not of the method.**
 *
 * The shared [okhttp3.OkHttpClient] carries a 30s `callTimeout`, which is right for every other
 * route and wrong for these. The server's own deadlines are 30s for transcription and 60s for
 * synthesis, so a 30s client cap TIES the first and UNDERCUTS the second — the client's own
 * `SocketTimeoutException` wins the race and replaces a diagnosable `502 speech_gateway_timeout`
 * with a generic failure. That defeats the whole point of the server publishing distinct error
 * codes. OkHttp's `callTimeout` cannot be varied per call from an interceptor, so the only way to
 * give these routes a longer leash without slackening every unrelated request is a derived client
 * — and a client is reached through a Retrofit instance, which is reached through an interface.
 * Hence this file. See [com.mewbo.aura.di.SpeechModule] for the derivation and the chosen value.
 *
 * **The whole namespace is OPTIONAL server-side.** It mounts only when the `mewbo-speech` package
 * is installed, so a deployment without it answers 404 to all three routes. That is a normal
 * state, not a fault: [com.mewbo.aura.data.repo.SpeechRepository] degrades it to "no server
 * engines" and the pickers still offer On device.
 *
 * Verified field-for-field against `mewbo_api/speech/routes.py`; shapes in [SpeechDtos.kt].
 */
interface SpeechApi {

    /**
     * What the deployment can do: per-direction availability, the models the gateway advertises
     * for each, and the operator's configured defaults.
     *
     * One call rather than two because the settings screen renders both selectors together.
     * **`available` is the gating signal, not the model list** — the route deliberately keeps
     * answering defaults and voices with an EMPTY list when the gateway is unreachable, so a
     * client can still render a picker. It is derived from mount state and configuration with NO
     * health probe, so it reads `true` for a gateway whose credential is dead: availability is
     * not reachability.
     *
     * **Cost: `O(collection)`** in the gateway's model count (tens). The server caches the
     * catalogue per process and remembers a failure for a minute, so this is one round trip on
     * the first call and free afterwards.
     */
    @GET("api/speech/capabilities")
    suspend fun getSpeechCapabilities(): SpeechCapabilitiesResponseDto

    /**
     * Synthesizes [request]'s text, returning RAW AUDIO BYTES.
     *
     * The GATEWAY's own `Content-Type` is always `audio/mpeg` and always wrong (the bytes are
     * WAV or FLAC); the server corrects it before answering, but nothing here depends on that —
     * `MediaPlayer` sniffs the container from the bytes and gets it right either way.
     * [ResponseBody] rather than a DTO for the same reason `postDeviceToolResult` uses one:
     * running audio through the JSON converter is a decode failure on the success path.
     *
     * **Cost: `O(one record)` in the text length, and it is SLOW.** Measured against the live
     * gateway: a COLD first call is ~7.9s for a 34-character sentence (connection setup plus a
     * library import server-side), ~2.4s warm. That is why
     * [com.mewbo.aura.voice.SentenceChunker] matters here rather than being an optimisation:
     * synthesizing sentence-by-sentence starts audio after the first sentence instead of after
     * the whole reply. Text is capped server-side at 2000 characters, which one sentence never
     * approaches. Server deadline 60s.
     *
     * **Concurrency: the server serves 4 speech calls at once across BOTH routes**, and the 5th
     * gets `503 speech_capacity_exhausted` with `Retry-After`. See
     * [com.mewbo.aura.voice.RemoteSynthesizer] for how that one status is handled differently
     * from every other failure.
     */
    @POST("api/speech/synthesize")
    suspend fun synthesizeSpeech(@Body request: SpeechSynthesizeRequest): ResponseBody

    /**
     * Transcribes one captured utterance. Multipart, mirroring `uploadAttachments`' shape — the
     * only file-upload precedent in this client.
     *
     * **The part MUST be named `file`.** The route reads `request.files.get("file")` and answers
     * a 400 naming the field for anything else; the name is not interchangeable with the `files`
     * the attachments route uses. **The FILENAME's extension is the gateway's format hint**,
     * falling back to the part's mimetype — so a `.wav` name over WAV bytes is doing real work
     * here, not decoration.
     *
     * **Cost: `O(one record)` in the audio duration**, bounded server-side by a 30s deadline and
     * a 10 MiB upload cap. [com.mewbo.aura.voice.RemoteTranscriber]'s own 20s capture cap keeps a
     * request an order of magnitude inside both. Measured: 0.24s for a WAV clip.
     */
    @Multipart
    @POST("api/speech/transcribe")
    suspend fun transcribeSpeech(
        @Part file: MultipartBody.Part,
        @Part("model") model: RequestBody,
    ): SpeechTranscribeResponseDto
}
