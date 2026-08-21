package com.mewbo.aura.voice

import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.di.ApplicationScope
import com.mewbo.aura.di.OnDeviceSpeech
import com.mewbo.aura.di.ServerSpeech
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.merge
import kotlinx.coroutines.flow.stateIn

/**
 * The [Synthesizer] everything in the app actually gets: the user's Settings choice picks the
 * delegate, per speech RUN.
 *
 * The read-aloud button, the overlay speak-along and `SpeechController`'s sentence chunking all
 * hold a `Synthesizer` and none of them knows a choice exists — the selection lands at the DI seam,
 * so every speech path honours it by construction rather than by three call sites agreeing.
 *
 * ## Why a run-scoped latch rather than a per-utterance read
 *
 * [speak] is queue-append ([Synthesizer]'s QUEUE_ADD contract) and `SpeechController` calls it once
 * per SENTENCE. Resolving the engine per call would let a selection changed mid-reply finish a
 * message in a different voice than it started in — and worse, split one message's utterances
 * across two engines whose queues know nothing about each other, so they would overlap rather than
 * queue. The delegate is therefore chosen on the first [speak] after a [stop] and held until the
 * next [stop] — which is exactly barge-in, the boundary the rest of the speech stack already treats
 * as "this run is over".
 */
@Singleton
class SelectedSynthesizer @Inject constructor(
    @OnDeviceSpeech private val onDevice: Synthesizer,
    @ServerSpeech private val remote: Synthesizer,
    private val engineGate: SpeechEngineGate,
    @ApplicationScope private val scope: CoroutineScope,
) : Synthesizer {

    /**
     * The selection, sampled without suspending because [speak] cannot.
     *
     * `Eagerly` with an on-device seed rather than a blocking `first()` read: a `runBlocking` here
     * would sit on whichever thread called [speak], and that is routinely Main (a transcript fold
     * on `viewModelScope`). The seed is only ever observed in the window between process start and
     * DataStore's first emission, which no speech can occur in — speaking requires a rendered
     * screen and either a tap or a streamed reply, both of which are many frames away. Erring
     * toward on-device in that window is also the conservative direction: the local engine, no
     * audio leaving the device.
     */
    private val selection: StateFlow<String> = engineGate.selection(SpeechDirection.TextToSpeech)
        .stateIn(scope, SharingStarted.Eagerly, SpeechCatalog.ON_DEVICE)

    /** The delegate serving the CURRENT run; `null` between runs. Guarded by `this` — [speak] and
     * [stop] race whenever a barge-in lands while a reply is still folding in. */
    private var active: Synthesizer? = null

    /**
     * Both delegates' events, merged.
     *
     * Safe to merge unconditionally because only one engine is ever mid-run (the latch), and
     * `utteranceId` is `{messageId}:{sentenceIndex}` — unique per message — so a straggling event
     * from a barged-in run cannot be mistaken for the new one's. `SpeechController` additionally
     * ignores any id that is not its own `lastEnqueuedId`.
     */
    override fun events(): Flow<SynthEvent> = merge(onDevice.events(), remote.events())

    /**
     * Whether the SELECTED engine can speak. Follows the selection, so switching to a server
     * engine on a device with no TTS voice data re-enables the read-aloud control that the
     * platform engine's absence had disabled.
     */
    @OptIn(ExperimentalCoroutinesApi::class)
    override val isAvailable: StateFlow<Boolean> = selection
        .flatMapLatest { stored ->
            if (SpeechCatalog.isOnDevice(stored)) onDevice.isAvailable else remote.isAvailable
        }
        .stateIn(scope, SharingStarted.Eagerly, false)

    @Synchronized
    override fun speak(utteranceId: String, text: String) {
        val delegate = active ?: currentDelegate().also { active = it }
        delegate.speak(utteranceId, text)
    }

    /**
     * Stops BOTH delegates, not just the active one, and clears the latch.
     *
     * [Synthesizer.stop] is specified idempotent and both implementations are no-ops when idle, so
     * the extra call is free — and it is the only thing that makes a selection change SAFE while
     * audio is playing. Stopping just the latched delegate would leave the other one's queue
     * intact if the engine changed between a `speak` and this `stop`, which is a voice that keeps
     * talking after the user pressed stop.
     */
    @Synchronized
    override fun stop() {
        active = null
        onDevice.stop()
        remote.stop()
    }

    private fun currentDelegate(): Synthesizer =
        if (SpeechCatalog.isOnDevice(selection.value)) onDevice else remote
}
