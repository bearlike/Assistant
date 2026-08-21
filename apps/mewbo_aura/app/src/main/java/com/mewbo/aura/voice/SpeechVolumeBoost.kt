package com.mewbo.aura.voice

import com.mewbo.aura.di.ApplicationScope
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/**
 * Amplification of spoken replies ABOVE the device's own maximum volume, owned in one place and
 * consumed by both synthesizers.
 *
 * ## Why this class exists at all — neither playback API can do it
 *
 * A television frequently has no volume rocker, and a remote's volume keys usually drive the panel
 * or an AVR rather than Android's media stream, so at the top of the device's range the assistant
 * can still be too quiet to hear. Neither of this app's two playback APIs can help, and both
 * failures are the same shape — a scalar that only ever attenuates:
 *
 * - `TextToSpeech.Engine.KEY_PARAM_VOLUME` is documented as *"a float ranging from 0 to 1 where 0
 *   is silence, and 1 is the maximum volume (the default behavior)"*. The framework carries it as
 *   `TextToSpeechService.AudioOutputParams.mVolume` (*"in the range [0.0f, 1.0f]"*) to
 *   `AudioTrack.setVolume`, which hard-clamps to its own `GAIN_MAX = 1.0f`.
 * - `MediaPlayer.setVolume` reaches that same clamped gain.
 *
 * The platform's supported route above unity is `android.media.audiofx.LoudnessEnhancer` —
 * *"an audio effect for increasing audio loudness … signals amplified outside of the sample range
 * supported by the platform are compressed"* — attached to an audio SESSION and parametrized in
 * millibels. That is what [AudioBoostPlatform] wraps, and routing audio through a session id is the
 * only thing either synthesizer has to do.
 *
 * ## What it does NOT claim
 *
 * A successful attach means the EFFECT exists on the session. On the `TextToSpeech` leg the audio
 * only passes through that session if the engine honours
 * [android.speech.tts.TextToSpeech.Engine.KEY_PARAM_SESSION_ID] — which AOSP's own
 * `BlockingAudioTrack` does, but an engine that plays its own audio out of band never sees the
 * bundle at all. Nothing on the device can report which kind of engine is installed, so
 * [SpeechBoostState.Applied] deliberately renders as NO status claim in Settings; only a REFUSED
 * attach is surfaced, and only after one was genuinely attempted. Guessing "supported" here would
 * be the wrong-green `ui/settings/CLAUDE.md` exists to prevent.
 *
 * ## Shape
 *
 * State (the gain in force, the live attachment) and behaviour (attach, release) sit together;
 * the Android effect arrives as an injected [AudioBoostPlatform] and the user's choice as a
 * [SpeechVolumeBoostGate] flow — the same narrow-seam treatment [SpeechEngineGate] and
 * [SpeechRecognitionAvailability] get, and for the same reason: the rules worth testing (clamping,
 * dB→mB, off-attaches-nothing, the refusal latch) must not need an `AudioManager` in the test.
 *
 * [sessionId] and [release] are `@Synchronized` for the reason [PlatformSynthesizer]'s own `pending`
 * is: `speak` and a barge-in `stop` arrive on different threads.
 */
@Singleton
class SpeechVolumeBoost @Inject constructor(
    private val platform: AudioBoostPlatform,
    gate: SpeechVolumeBoostGate,
    @ApplicationScope scope: CoroutineScope,
) {

    /**
     * The user's choice, clamped, sampled without suspending because [sessionId] cannot.
     *
     * `Eagerly` with an OFF seed for exactly [SelectedSynthesizer]'s reason: a blocking read would
     * sit on whichever thread called `speak`, routinely Main. The seed is only observable between
     * process start and DataStore's first emission, and erring toward OFF there is the
     * conservative direction — the untouched default, and no effect attached to anything.
     */
    private val requested: StateFlow<Int> = gate.boostDecibels()
        .map(::clampDecibels)
        .stateIn(scope, SharingStarted.Eagerly, OFF_DECIBELS)

    private val _state = MutableStateFlow<SpeechBoostState>(SpeechBoostState.Untested)

    /** What the last attach ATTEMPT established. Read by Settings; see the class KDoc for what
     * [SpeechBoostState.Applied] is careful not to assert. */
    val state: StateFlow<SpeechBoostState> = _state.asStateFlow()

    private var attached: Attachment? = null

    /**
     * The gain this device has already refused, so a failing attach is tried ONCE per level.
     *
     * `LoudnessEnhancer`'s constructor failing is a device fact, not a transient one — and
     * [PlatformSynthesizer] calls [sessionId] once per SENTENCE, so without the latch a reply
     * would re-instantiate and re-fail the effect for every sentence it speaks. Keyed by level
     * rather than a bare flag, so raising or lowering the boost still gets a fresh attempt.
     */
    private var refusedDecibels: Int? = null

    /**
     * The audio session both synthesizers should route this utterance through, or `null` when
     * there is nothing to boost.
     *
     * **`null` means "carry on exactly as before".** Off returns `null` having attached nothing,
     * and a refused attach returns `null` too — so a caller's untouched path (a `null` params
     * Bundle, a `MediaPlayer` with its own session) is always the honest fallback and there is no
     * half-attached state to reason about.
     *
     * Idempotent and cheap: an existing attachment at the same gain is returned as-is, so a
     * multi-sentence reply builds ONE effect rather than one per utterance.
     *
     * Cost: `O(1)` — a `StateFlow` read, and at most one effect construction per level change.
     */
    @Synchronized
    fun sessionId(): Int? {
        val decibels = requested.value
        if (decibels == OFF_DECIBELS) {
            // Off must mean the effect is not attached at all, not attached at zero gain.
            detach()
            _state.value = SpeechBoostState.Untested
            return null
        }
        attached?.let { live ->
            if (live.decibels == decibels) return live.sessionId
        }
        detach()
        if (refusedDecibels == decibels) return null

        val session = platform.newSessionId()
        // `generateAudioSessionId` answers AudioManager.ERROR (a non-positive value) rather than
        // throwing when the framework has none to give; a session id is otherwise always positive.
        val handle = if (session > INVALID_SESSION_ID) {
            platform.attachLoudness(session, millibelsFor(decibels))
        } else {
            null
        }
        if (handle == null) {
            refusedDecibels = decibels
            _state.value = SpeechBoostState.Refused(decibels)
            return null
        }
        attached = Attachment(session, decibels, handle)
        _state.value = SpeechBoostState.Applied(decibels)
        return session
    }

    /**
     * Drop the effect at the end of a speech run — the barge-in/stop boundary the rest of this
     * package already treats as "this run is over".
     *
     * Both synthesizers call it from their own `stop()`, and [SelectedSynthesizer.stop] reaches
     * both, so it is called more than once per barge-in by design. Idempotent for that reason.
     */
    @Synchronized
    fun release() {
        detach()
    }

    private fun detach() {
        attached?.handle?.release()
        attached = null
    }

    private data class Attachment(val sessionId: Int, val decibels: Int, val handle: BoostHandle)

    companion object {
        /** No boost, and the untouched default: a control nobody has opened changes nothing. */
        const val OFF_DECIBELS = 0

        /**
         * The ceiling offered, and it is a judgement rather than a platform limit.
         *
         * `LoudnessEnhancer` documents no maximum, but it compresses whatever it pushes outside the
         * sample range — so past roughly this much gain a quiet television gains loudness by losing
         * dynamic range and intelligibility, which is the opposite of the point. It is also the
         * clamp a stored value from any future writer is held to.
         */
        const val MAX_DECIBELS = 20

        /** The levels the picker offers, coarse on purpose: this is a control someone operates
         * from across a room with a remote, not a mixing desk. [OFF_DECIBELS] leads it. */
        val LEVELS_DECIBELS: List<Int> = listOf(OFF_DECIBELS, 3, 6, 10, 15, MAX_DECIBELS)

        private const val MILLIBELS_PER_DECIBEL = 100

        /** A session id is always positive; `AudioManager.ERROR` is -1. */
        private const val INVALID_SESSION_ID = 0

        /**
         * A stored level held to the offered range.
         *
         * Below zero would be ATTENUATION — a boost control that quietens the assistant is not a
         * setting anyone asked for, and DataStore will hand back whatever was written — and above
         * the ceiling is the compression the class KDoc describes. Pure, so the rule is testable
         * without an `AudioManager`.
         */
        fun clampDecibels(raw: Int): Int = raw.coerceIn(OFF_DECIBELS, MAX_DECIBELS)

        /** dB → millibels, the unit `LoudnessEnhancer.setTargetGain` takes (100 mB = 1 dB), via
         * the same clamp so no caller can route around it. */
        fun millibelsFor(decibels: Int): Int = clampDecibels(decibels) * MILLIBELS_PER_DECIBEL
    }
}

/**
 * What the last attach attempt established — never more than that.
 *
 * [Untested] and [Applied] both render as no status claim in Settings, for the reason spelled out
 * in [SpeechVolumeBoost]'s KDoc: an attached effect is not proof the selected engine's audio passes
 * through it. Only [Refused] is surfaced, and only because it was measured.
 */
sealed interface SpeechBoostState {

    /**
     * Whether this state is a refusal of the level currently chosen.
     *
     * On the model rather than in the ViewModel because it is intrinsic to the state: a refusal of
     * +3 dB says nothing about the +15 dB the user has since picked, and a screen comparing the two
     * numbers itself would be a second place for that rule to drift.
     */
    fun refuses(decibels: Int): Boolean = this is Refused && this.decibels == decibels

    /** Nothing has been spoken since the level last changed, so nothing is known. */
    data object Untested : SpeechBoostState

    /** The effect was constructed on this run's session at [decibels]. */
    data class Applied(val decibels: Int) : SpeechBoostState

    /** The platform refused the effect at [decibels]. Measured, not assumed. */
    data class Refused(val decibels: Int) : SpeechBoostState
}

/**
 * How much amplification the user has asked for, in whole decibels; [SpeechVolumeBoost.OFF_DECIBELS]
 * for none.
 *
 * A `fun interface` bound in `di/` to `SettingsStore`, for exactly [SpeechEngineGate]'s reason:
 * injecting the store would drag the Android Keystore in through `KeystoreCipher` and force this
 * class's pure rules onto Robolectric. A **flow**, so a change in Settings reaches the next spoken
 * reply with no app restart.
 */
fun interface SpeechVolumeBoostGate {
    fun boostDecibels(): Flow<Int>
}

/**
 * The `android.media.audiofx` facts, behind a seam.
 *
 * Two methods rather than a `fun interface` because allocating the session and attaching the effect
 * to it are one indivisible capability — a caller that could do one without the other would be able
 * to route audio through a session carrying nothing. Bound as an anonymous object in
 * `di/SpeechModule`, the same place `SpeechRecognitionAvailability`'s real platform read lives.
 */
interface AudioBoostPlatform {

    /** A fresh audio session id, or a non-positive value when the framework has none to give. */
    fun newSessionId(): Int

    /** Attach a loudness effect at [gainMillibels], or `null` when this device refuses it. */
    fun attachLoudness(sessionId: Int, gainMillibels: Int): BoostHandle?
}

/** A live effect, releasable. Nothing else is ever asked of it: the gain is fixed at attach time,
 * because a level changed mid-reply would otherwise shift loudness between two sentences of one
 * answer — the same reason [SelectedSynthesizer] latches the engine for a whole run. */
fun interface BoostHandle {
    fun release()
}
