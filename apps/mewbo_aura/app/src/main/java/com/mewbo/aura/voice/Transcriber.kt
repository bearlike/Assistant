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

/** Maps [android.speech.SpeechRecognizer] error codes. NoMatch/Timeout are quiet-cancels. */
enum class TranscriberError {
    NoMatch,
    Timeout,
    Unavailable,
    Other,
}
