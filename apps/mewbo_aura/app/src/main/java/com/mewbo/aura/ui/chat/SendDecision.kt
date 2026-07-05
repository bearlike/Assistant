package com.mewbo.aura.ui.chat

import com.mewbo.aura.voice.InputModality

/**
 * Pure decisions [ChatViewModel.send] needs about its OWN in-flight send, before the network
 * round trip resolves anything - extracted the same way [SessionBinding] was (ChatViewModel's
 * other constructor deps can't be constructed in a plain JVM test, no Robolectric in this module,
 * so this is the one piece of `send()`'s logic that could get a real regression test).
 */
internal object SendDecision {
    /** `true` when this send is steering into an already-Sending-or-Streaming run (spec §6.2 C4's
     * 202 enqueue) - drives BOTH the optimistic bubble's `pending = true` echo and [phaseForSend]. */
    fun isSteering(currentPhase: RunPhase): Boolean =
        currentPhase == RunPhase.Sending || currentPhase == RunPhase.Streaming

    /**
     * The [RunPhase] to apply IMMEDIATELY on send, before the network call resolves RunStarted vs.
     * Enqueued. [hasActiveStream] = is a live SSE collection genuinely running right now (checked
     * via `streamJob?.isActive`, not the phase value alone).
     *
     * The bug this replaces (fix-round-3 Important #3): `send()` used to set `RunPhase.Sending`
     * unconditionally on every call. A steer into an ALREADY-streaming run stomped that live
     * `Streaming` phase - and since `Enqueued`'s own handling only resubscribes "if `streamJob`
     * isn't already active", nothing downstream ever restored it either. The thinking spark
     * (`showThinking = runPhase == Sending || ...`) and the still-live streamed reply then rendered
     * at the same time for the rest of the steered turn. A steer while a send is in flight but
     * hasn't started streaming yet ([hasActiveStream] false, phase `Sending`) has no live stream to
     * preserve, so it collapses to the same case as a fresh send.
     */
    fun phaseForSend(currentPhase: RunPhase, hasActiveStream: Boolean): RunPhase =
        if (hasActiveStream && currentPhase == RunPhase.Streaming) RunPhase.Streaming else RunPhase.Sending

    /**
     * The [InputModality] that should tag [ChatUiState.activeTurnModality] after this send
     * (Gitea #180 P1): a fresh (non-steering) send always (re)tags the turn it's opening with
     * [requested] - including the finalized [current] modality being replaced once a prior turn is
     * genuinely done, so a Voice tag never lingers past its own turn. A steer into an
     * already-Sending/Streaming run leaves [current] untouched - the steer's own modality (always
     * [InputModality.Text] today; the composer has no voice-steer path) was never what originated
     * the run it's steering into, so [current] stays readable unchanged for the rest of that turn.
     */
    fun modalityForSend(current: InputModality, requested: InputModality, isSteering: Boolean): InputModality =
        if (isSteering) current else requested

    /**
     * Gitea #180 P3: whether tapping the sticky stop-speaking control should latch the per-
     * conversation mute (#175 §7.1) - `true` only while the tap actually interrupts THIS turn's own
     * live voice-modality speech-along, i.e. a voice-tagged turn still genuinely Sending/Streaming.
     * A stop tapped once that turn has already gone Idle/Done/Error (e.g. stopping an unrelated
     * manual read-aloud of an older message afterward) must not mute a turn that isn't live
     * anymore - [ChatViewModel.send]'s next voice-initiated call is what resets the flag back.
     */
    fun shouldMuteOnStopSpeaking(activeTurnModality: InputModality, runPhase: RunPhase): Boolean =
        activeTurnModality == InputModality.Voice && (runPhase == RunPhase.Sending || runPhase == RunPhase.Streaming)
}
