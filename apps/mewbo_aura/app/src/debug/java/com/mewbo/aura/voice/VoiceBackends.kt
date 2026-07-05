package com.mewbo.aura.voice

import android.content.Context
import android.speech.SpeechRecognizer
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking

private val Context.voiceSettingsDataStore: DataStore<Preferences> by preferencesDataStore(name = "voice_settings")

/**
 * Debug-only runtime switch between fake and platform speech backends. Defaults to fakes
 * whenever [SpeechRecognizer.isRecognitionAvailable] is false (redroid, automatically),
 * overridable via the "voice.useFakes" DataStore key. No settings UI yet (a later wave adds the
 * toggle) — this is just the seam. The decision is read once via `runBlocking` at first access
 * (a tiny local preferences read); acceptable for a debug-only convenience class.
 */
@Singleton
class VoiceBackends @Inject constructor(
    @ApplicationContext private val context: Context,
    private val speechRecognizerTranscriber: SpeechRecognizerTranscriber,
    private val platformSynthesizer: PlatformSynthesizer,
    private val fakeTranscriber: FakeTranscriber,
    private val fakeSynthesizer: FakeSynthesizer,
) {
    private val useFakes: Boolean by lazy {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            true
        } else {
            runBlocking { context.voiceSettingsDataStore.data.first()[USE_FAKES_KEY] ?: false }
        }
    }

    val transcriber: Transcriber get() = if (useFakes) fakeTranscriber else speechRecognizerTranscriber
    val synthesizer: Synthesizer get() = if (useFakes) fakeSynthesizer else platformSynthesizer

    companion object {
        val USE_FAKES_KEY = booleanPreferencesKey("voice.useFakes")
    }
}
