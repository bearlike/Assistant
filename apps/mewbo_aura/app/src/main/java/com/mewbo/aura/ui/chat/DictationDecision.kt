package com.mewbo.aura.ui.chat

import com.mewbo.aura.voice.TranscriberEvent

/**
 * Pure `TranscriberEvent` -> [DictationState] transitions [ChatViewModel.startDictation] applies to
 * every event its collected `Transcriber` flow emits - extracted the same way [SendDecision] was
 * (`ChatViewModel` itself can't be constructed in a plain JVM test, no Robolectric in this module).
 */
internal object DictationDecision {
    /**
     * [current] only matters for [TranscriberEvent.Rms]/[TranscriberEvent.Partial], which each
     * carry just ONE half of [DictationState.Listening]'s payload and must preserve the other - an
     * amplitude tick arriving mid-partial-transcript must not blank the words already shown, and a
     * newly-finalizing word must not reset the amplitude back to silence. [TranscriberEvent.Final]'s
     * text surfaces via [DictationState.Final] for `ChatSurface`'s one-shot draft-fill;
     * [TranscriberEvent.Error] always reverts to [DictationState.Idle] regardless of code -
     * `NoMatch`/`Timeout` per the P0 reference-capture ruling (silent revert, zero error UI) and
     * `Unavailable`/`Other` the same way from THIS state machine's point of view (the mic-disable
     * side effect specific to `Unavailable` is a separate, stateful concern handled at the call
     * site - [ChatViewModel.startDictation] - not this pure mapping).
     */
    fun next(current: DictationState, event: TranscriberEvent): DictationState = when (event) {
        TranscriberEvent.Ready -> DictationState.Listening()
        is TranscriberEvent.Rms -> listening(current).copy(rmsDb = normalizeRms(event.db))
        is TranscriberEvent.Partial -> listening(current).copy(partial = event.text)
        is TranscriberEvent.Final -> DictationState.Final(event.text)
        is TranscriberEvent.Error -> DictationState.Idle
    }

    private fun listening(current: DictationState): DictationState.Listening =
        current as? DictationState.Listening ?: DictationState.Listening()

    /** `SpeechRecognizer#onRmsChanged`'s dBFS-ish range (empirically ~[-2, 10] - matching
     * `FakeTranscriber`'s own script) linearly mapped onto `RmsWaveform`'s expected 0f..1f
     * amplitude - the "voice/-layer concern" its own KDoc calls out as deliberately out of
     * ui/composer's job (Gitea #180 P2). */
    private const val MinRmsDb = -2f
    private const val MaxRmsDb = 10f
    private fun normalizeRms(db: Float): Float = ((db - MinRmsDb) / (MaxRmsDb - MinRmsDb)).coerceIn(0f, 1f)
}
