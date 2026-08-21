package com.mewbo.aura.voice

/**
 * What the server-backed speech engines need from the network, and nothing else.
 *
 * Declared HERE, in the consumer's own package, with the implementation
 * ([com.mewbo.aura.data.repo.SpeechRepository]) bound in `di/` — the seam law
 * ([`di/CLAUDE.md`](../di/CLAUDE.md)) that `DeviceToolDispatch` and `RunNotifications` already
 * follow. Two things fall out of it, and both are the point:
 *
 * - **`voice/` never sees a DTO or a Retrofit type**, so the unreconciled `/api/speech` wire shapes
 *   ([com.mewbo.aura.data.api.SpeechModelsResponseDto]) stay behind one door.
 * - **[RemoteSynthesizer] and [RemoteTranscriber] are plain-JVM testable** against a hand-rolled
 *   fake, with no OkHttp and no `SettingsStore` (which transitively needs the Android Keystore).
 *   Same motivation as `DeviceToolGate` being a lambda rather than a `SettingsStore` injection.
 */
interface SpeechGateway {
    /**
     * Synthesizes [text] with server model [modelId], returning the encoded audio bytes.
     *
     * The container is WAV or FLAC and is NOT declared by any header worth reading (see
     * [com.mewbo.aura.data.api.AuraApi.synthesizeSpeech]) — callers hand the bytes to a decoder
     * that sniffs them.
     *
     * Throws on any transport or server failure; [RemoteSynthesizer] turns that into a
     * [SynthEvent.Error] for the utterance rather than letting it escape.
     */
    suspend fun synthesize(modelId: String, text: String): ByteArray

    /**
     * Transcribes one complete captured utterance ([audio], 16-bit PCM wrapped as a WAV file by
     * the caller) with server model [modelId].
     *
     * Request/response, never streaming: this client has no WebSocket anywhere and deliberately
     * does not add one. That single fact is why [RemoteTranscriber] can produce no
     * [TranscriberEvent.Partial] — there is nothing to stream partials over.
     */
    suspend fun transcribe(modelId: String, audio: ByteArray): String
}

/**
 * The server is already serving its maximum concurrent speech calls and asked us to wait
 * [retryAfterSeconds] before trying again.
 *
 * A distinct type rather than a status code, so `voice/` learns "this one is worth retrying"
 * without importing Retrofit or knowing what 503 means — the same reason the gateway interface
 * exists at all.
 *
 * **The distinction is load-bearing: 503 covers TWO different answers.**
 * `speech_capacity_exhausted` is transient and carries a `Retry-After`; `speech_unavailable` means
 * the deployment is not configured for speech and carries none. Retrying the second one would
 * stall a read for no reason. The presence of the header is what separates them, which is also why
 * this carries the delay rather than a boolean — the wait is the server's to choose, not ours to
 * guess.
 */
class SpeechCapacityExhausted(val retryAfterSeconds: Long) : Exception(
    "The speech service is at capacity; it asked to be retried in ${retryAfterSeconds}s.",
)
