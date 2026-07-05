package com.mewbo.aura.voice

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow

/** TTS seam. [speak] queues with QUEUE_ADD semantics; [stop] is an idempotent flush. */
interface Synthesizer {
    val isAvailable: StateFlow<Boolean>
    fun speak(utteranceId: String, text: String)
    fun stop()
    fun events(): Flow<SynthEvent>
}

sealed interface SynthEvent {
    data class Started(val id: String) : SynthEvent
    data class Done(val id: String) : SynthEvent
    data class Error(val id: String) : SynthEvent
}
