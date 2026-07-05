package com.mewbo.aura.voice

import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

/**
 * Scripted STT double for redroid (no on-device [android.speech.SpeechRecognizer]) and tests.
 * [script] is settable before [listen] is collected — the whole orb state machine runs on this.
 */
@Singleton
class FakeTranscriber @Inject constructor() : Transcriber {

    var script: List<ScriptStep> = DEFAULT_SCRIPT

    override fun listen(): Flow<TranscriberEvent> = flow {
        for (step in script) {
            delay(step.delayMs)
            emit(step.event)
        }
    }

    data class ScriptStep(val delayMs: Long, val event: TranscriberEvent)

    companion object {
        val DEFAULT_SCRIPT = listOf(
            ScriptStep(0, TranscriberEvent.Ready),
            ScriptStep(150, TranscriberEvent.Rms(-2f)),
            ScriptStep(150, TranscriberEvent.Rms(4f)),
            ScriptStep(150, TranscriberEvent.Rms(-1f)),
            ScriptStep(150, TranscriberEvent.Rms(6f)),
            ScriptStep(200, TranscriberEvent.Partial("Hey")),
            ScriptStep(400, TranscriberEvent.Partial("Hey Mewbo, what…")),
            ScriptStep(500, TranscriberEvent.Final("Hey Mewbo, what's on my calendar today?")),
        )
    }
}
