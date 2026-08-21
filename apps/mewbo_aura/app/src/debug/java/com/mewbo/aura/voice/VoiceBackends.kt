package com.mewbo.aura.voice

import android.content.Context
import android.os.Build
import android.speech.SpeechRecognizer
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking

/**
 * Whether the user asked for the fake pipeline, as a flow.
 *
 * **A seam rather than a `SettingsStore` injection, and rather than this file opening the
 * preferences file itself.** Opening it here is what crashed the app: a `preferencesDataStore`
 * delegate CONSTRUCTS a `DataStore`, and DataStore refuses two live instances over one file —
 * `IllegalStateException: There are multiple DataStores active for the same file`. Naming the same
 * file as `SettingsStore` therefore did not share its store, it created a second one. The gate is
 * bound from the single `SettingsStore` instance in `di/VoiceModule`, which makes a second owner
 * impossible rather than merely absent.
 */
fun interface VoiceFakesGate {
    fun enabled(): Flow<Boolean>
}

/**
 * Debug-only runtime switch between fake and platform speech backends. Defaults to fakes
 * whenever [SpeechRecognizer.isRecognitionAvailable] is false (redroid, automatically),
 * overridable from Settings' own "Use fake voice pipeline" switch, which writes the same key
 * this reads. The decision is read once via `runBlocking` at first access
 * (a tiny local preferences read); acceptable for a debug-only convenience class.
 */
@Singleton
class VoiceBackends @Inject constructor(
    @ApplicationContext private val context: Context,
    private val fakesGate: VoiceFakesGate,
    private val speechRecognizerTranscriber: SpeechRecognizerTranscriber,
    private val platformSynthesizer: PlatformSynthesizer,
    private val fakeTranscriber: FakeTranscriber,
    private val fakeSynthesizer: FakeSynthesizer,
) {
    private val useFakes: Boolean by lazy {
        if (isEmulator && !SpeechRecognizer.isRecognitionAvailable(context)) {
            true
        } else {
            runBlocking { fakesGate.enabled().first() }
        }
    }

    /**
     * **"No recognizer" is NOT sufficient to substitute a fake, and treating it as such shipped a
     * lie to a real device.** The auto-substitution exists for redroid, which is AOSP and has no
     * recognizer — but a Fire TV has no recognizer either, so a user holding real hardware with
     * real microphone permission got scripted, fabricated transcripts presented as their own
     * speech. Nothing reported it, because a fake transcriber succeeds.
     *
     * Requiring an emulator narrows the substitution back to the case it was written for. A real
     * device with no recognizer now falls through to the engine the user actually selected, and
     * `SelectedTranscriber` routes it to the server leg when on-device cannot serve — a real
     * transcription rather than a fabricated one.
     *
     * Fingerprint matching is the ordinary way to ask this; `ro.kernel.qemu` is gone on modern
     * images and there is no first-party API. A false negative here is the safe direction — it
     * costs a dev one Settings toggle, where a false positive costs a user their trust in the
     * transcript.
     */
    private val isEmulator: Boolean
        get() = Build.FINGERPRINT.startsWith("generic") ||
            Build.FINGERPRINT.contains("emulator", ignoreCase = true) ||
            Build.FINGERPRINT.contains("redroid", ignoreCase = true) ||
            Build.MODEL.contains("Emulator", ignoreCase = true) ||
            Build.MODEL.contains("Android SDK built for", ignoreCase = true) ||
            Build.PRODUCT.contains("redroid", ignoreCase = true) ||
            Build.HARDWARE.contains("goldfish", ignoreCase = true) ||
            Build.HARDWARE.contains("ranchu", ignoreCase = true) ||
            Build.HARDWARE.contains("redroid", ignoreCase = true)

    val transcriber: Transcriber get() = if (useFakes) fakeTranscriber else speechRecognizerTranscriber
    val synthesizer: Synthesizer get() = if (useFakes) fakeSynthesizer else platformSynthesizer

}
