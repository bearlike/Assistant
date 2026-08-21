package com.mewbo.aura.voice

/**
 * Whether the platform's on-device speech recognizer can actually run on THIS device — the
 * predicate behind [SelectedTranscriber]'s on-device-vs-server fallback.
 *
 * A `fun interface` bound in `di/SpeechModule` over `android.speech.SpeechRecognizer
 * .isRecognitionAvailable`, for the same reason [SpeechEngineGate] and
 * [com.mewbo.aura.data.device.TelevisionChecker] are seams: it keeps [SelectedTranscriber]
 * constructible in a plain-JVM unit test with no `SpeechRecognizer`/`Context` in the test.
 */
fun interface SpeechRecognitionAvailability {
    fun isAvailable(): Boolean
}
