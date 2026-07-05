package com.mewbo.aura.voice

import android.util.Log
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Logs utterances instead of speaking; runs on redroid (no TTS engine) and powers spoken-output
 * assertions in tests. A single-consumer channel gives true QUEUE_ADD ordering; [stop] drains
 * the channel and restarts the consumer so it stays idempotent.
 */
@Singleton
class FakeSynthesizer @Inject constructor() : Synthesizer {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val queue = Channel<QueuedUtterance>(capacity = Channel.UNLIMITED)
    private val _events = MutableSharedFlow<SynthEvent>(extraBufferCapacity = 16)
    private var consumer: Job = startConsumer()

    override val isAvailable: StateFlow<Boolean> = MutableStateFlow(true).asStateFlow()

    override fun events(): Flow<SynthEvent> = _events.asSharedFlow()

    override fun speak(utteranceId: String, text: String) {
        queue.trySend(QueuedUtterance(utteranceId, text))
    }

    override fun stop() {
        while (queue.tryReceive().isSuccess) { /* drain any not-yet-spoken utterances */ }
        consumer.cancel()
        consumer = startConsumer()
    }

    private fun startConsumer(): Job = scope.launch {
        for (utterance in queue) {
            _events.tryEmit(SynthEvent.Started(utterance.id))
            Log.d(TAG, "speak(${utterance.id}): ${utterance.text}")
            delay(utterance.wordCount * MS_PER_WORD)
            _events.tryEmit(SynthEvent.Done(utterance.id))
        }
    }

    private data class QueuedUtterance(val id: String, val text: String) {
        val wordCount: Long = text.trim().split(Regex("\\s+")).count { it.isNotEmpty() }.toLong().coerceAtLeast(1)
    }

    companion object {
        private const val TAG = "FakeSynthesizer"
        private const val MS_PER_WORD = 40L
    }
}
