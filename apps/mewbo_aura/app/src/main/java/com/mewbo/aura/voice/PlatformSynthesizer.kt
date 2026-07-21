package com.mewbo.aura.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.Locale
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Wraps [TextToSpeech], initialized lazily on the first [speak]. Engine/voice-data absence never
 * surfaces to the UI as an error — [isAvailable] just flips to false and callers degrade
 * silently (speaker toggle disabled). Because init is async, utterances issued before it resolves
 * are held in [pending] and flushed in order once [onTtsInit] fires — resolved-unavailable
 * instead emits [SynthEvent.Error] for each of them so callers aren't left guessing. [speak],
 * [stop], and the init callback all touch [pending] and can run on different threads (TTS init
 * callbacks arrive off the caller's thread), so they're `@Synchronized`.
 */
@Singleton
class PlatformSynthesizer @Inject constructor(
    @ApplicationContext private val context: Context,
) : Synthesizer {

    private val _isAvailable = MutableStateFlow(false)
    override val isAvailable: StateFlow<Boolean> = _isAvailable.asStateFlow()

    private val _events = MutableSharedFlow<SynthEvent>(extraBufferCapacity = 16)
    override fun events(): Flow<SynthEvent> = _events.asSharedFlow()

    private var tts: TextToSpeech? = null
    private var initResolved = false
    private val pending = mutableListOf<PendingUtterance>()
    private var focusRequest: AudioFocusRequest? = null
    private val audioManager by lazy { context.getSystemService(Context.AUDIO_SERVICE) as AudioManager }

    @Synchronized
    override fun speak(utteranceId: String, text: String) {
        val engine = ensureInitialized()
        when {
            !initResolved -> pending += PendingUtterance(utteranceId, text)
            _isAvailable.value -> {
                requestAudioFocus()
                engine.speak(text, TextToSpeech.QUEUE_ADD, null, utteranceId)
            }
            else -> _events.tryEmit(SynthEvent.Error(utteranceId))
        }
    }

    @Synchronized
    override fun stop() {
        pending.clear()
        tts?.stop()
        abandonAudioFocus()
    }

    private fun ensureInitialized(): TextToSpeech =
        tts ?: TextToSpeech(context, ::onTtsInit).also { tts = it }

    @Synchronized
    private fun onTtsInit(status: Int) {
        val engine = tts ?: return
        val langOk = status == TextToSpeech.SUCCESS &&
            engine.isLanguageAvailable(Locale.getDefault()) >= TextToSpeech.LANG_AVAILABLE
        if (langOk) {
            engine.language = Locale.getDefault()
            engine.setOnUtteranceProgressListener(progressListener)
        }
        initResolved = true
        _isAvailable.value = langOk

        val queued = pending.toList()
        pending.clear()
        if (langOk) {
            requestAudioFocus()
            queued.forEach { engine.speak(it.text, TextToSpeech.QUEUE_ADD, null, it.id) }
        } else {
            queued.forEach { _events.tryEmit(SynthEvent.Error(it.id)) }
        }
    }

    private data class PendingUtterance(val id: String, val text: String)

    private fun requestAudioFocus() {
        if (focusRequest != null) return
        val attributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attributes)
            .build()
        audioManager.requestAudioFocus(request)
        focusRequest = request
    }

    private fun abandonAudioFocus() {
        focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        focusRequest = null
    }

    private val progressListener = object : UtteranceProgressListener() {
        override fun onStart(utteranceId: String) {
            _events.tryEmit(SynthEvent.Started(utteranceId))
        }

        override fun onDone(utteranceId: String) {
            _events.tryEmit(SynthEvent.Done(utteranceId))
            if (tts?.isSpeaking != true) abandonAudioFocus()
        }

        // onDone abandons focus when nothing else is speaking, but
        // an error on the LAST queued utterance of a turn used to leave focus held (ducking other
        // apps) until some unrelated later speak()/stop() call - voice/CLAUDE.md's own "abandon on
        // stop/done" rule didn't cover the error path. Same guard as onDone, since an error means
        // nothing more is coming for THIS utterance either.
        @Suppress("OVERRIDE_DEPRECATION")
        override fun onError(utteranceId: String) {
            _events.tryEmit(SynthEvent.Error(utteranceId))
            if (tts?.isSpeaking != true) abandonAudioFocus()
        }

        override fun onError(utteranceId: String, errorCode: Int) {
            _events.tryEmit(SynthEvent.Error(utteranceId))
            if (tts?.isSpeaking != true) abandonAudioFocus()
        }
    }
}
