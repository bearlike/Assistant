package com.mewbo.aura.voice

import kotlinx.coroutines.flow.Flow

/**
 * STT seam. Cold flow: collecting [listen] starts recognition, cancelling the collector stops
 * it — this maps directly onto `callbackFlow`'s own start/`awaitClose` lifecycle in
 * [SpeechRecognizerTranscriber] and onto a scripted timer in the debug fake.
 */
interface Transcriber {
    fun listen(): Flow<TranscriberEvent>
}

sealed interface TranscriberEvent {
    data object Ready : TranscriberEvent
    data class Rms(val db: Float) : TranscriberEvent
    data class Partial(val text: String) : TranscriberEvent
    data class Final(val text: String) : TranscriberEvent
    data class Error(val code: TranscriberError) : TranscriberEvent
}

/**
 * Maps [android.speech.SpeechRecognizer] error codes, plus the one failure only a REMOTE engine
 * can have. NoMatch/Timeout are quiet-cancels; [ServiceFailed] deliberately is not.
 */
enum class TranscriberError {
    NoMatch,
    Timeout,
    Unavailable,
    Other,

    /**
     * A server-backed engine was selected and the server refused or could not be reached — the
     * audio was captured and then thrown away.
     *
     * **Separate from [Other] because it is the one error that must NOT be silent.** Every other
     * code here describes something the user can see for themselves: they said nothing, they
     * paused too long, the device has no recognizer. This one describes a recording they DID make
     * being lost to a failure with no visible cause, and the remedy is a setting they chose — so a
     * quiet revert to idle reads as the microphone button simply not working. Only
     * [RemoteTranscriber] ever emits it; the platform recognizer has no notion of a service.
     */
    ServiceFailed,
}
