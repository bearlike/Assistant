package com.mewbo.aura.voice

import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.di.OnDeviceSpeech
import com.mewbo.aura.di.ServerSpeech
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emitAll
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flow

/**
 * The [Transcriber] everything in the app actually gets: the user's Settings choice picks the
 * delegate, per capture.
 *
 * **This is why no consumer changed.** `AssistTurnMachine.startListening` and
 * `ChatViewModel.startDictation` both hold a `Transcriber` and neither knows a choice exists; the
 * selection lands at the DI seam instead of at two call sites that would then have to be kept in
 * step. A third consumer added later inherits it for free.
 *
 * The [OnDeviceSpeech] delegate is whatever the build type says on-device means — the platform
 * recognizer in release, `VoiceBackends`' fake/platform runtime switch in debug — so redroid's
 * scripted fake still stands in for on-device here exactly as it did before.
 *
 * ## On-device selected, on-device impossible: fall back to the server leg
 *
 * A stored selection of [SpeechCatalog.ON_DEVICE] is not, by itself, a promise this device can
 * keep — some televisions (Fire TV among them) ship no `RecognitionService` at all, so the
 * platform recognizer never fires. Before this fallback existed, the debug build's own
 * `VoiceBackends` auto-detected exactly that unavailability and silently substituted
 * `FakeTranscriber` — a scripted, canned transcript — because that switch was written for the
 * redroid dev container, where the identical unavailability signal means "no hardware, use the
 * dev double". On a real, unavailable device it meant "return fabricated text as if it were a
 * real transcription", which is worse than either working or failing honestly.
 *
 * [SpeechCatalog.ON_DEVICE] is stored as blank, and blank is DOUBLY overloaded: it is both "never
 * touched this setting" and "explicitly chose on-device" — see `data/settings/SettingsStore`'s
 * own KDoc on [com.mewbo.aura.voice.SpeechEngineGate]. Nothing in the current schema can tell
 * those two apart, so this router cannot honour "the user explicitly chose on-device, so never
 * let audio reach a server" any more strictly than "on-device is merely the untouched default".
 * Recording that gap rather than inventing a migration to close it: distinguishing the two would
 * need a new stored tri-state, which is out of scope here. Given the choice between the two
 * readings, falling back to the real server engine is still the better of the two outcomes
 * available today — the alternative is not silence, it is a scripted lie.
 */
@Singleton
class SelectedTranscriber @Inject constructor(
    @OnDeviceSpeech private val onDevice: Transcriber,
    @ServerSpeech private val remote: Transcriber,
    private val engineGate: SpeechEngineGate,
    private val recognitionAvailability: SpeechRecognitionAvailability,
) : Transcriber {

    /**
     * The choice is read INSIDE the cold flow, so it is sampled when a capture starts rather than
     * when the singleton is built. Changing the engine in Settings therefore takes effect on the
     * next tap of the mic — no restart, and no way for a mid-capture change to swap engines under
     * a recording that is already running.
     */
    override fun listen(): Flow<TranscriberEvent> = flow {
        val stored = engineGate.selection(SpeechDirection.SpeechToText).first()
        val delegate = when {
            !SpeechCatalog.isOnDevice(stored) -> remote
            recognitionAvailability.isAvailable() -> onDevice
            else -> remote
        }
        emitAll(delegate.listen())
    }
}
