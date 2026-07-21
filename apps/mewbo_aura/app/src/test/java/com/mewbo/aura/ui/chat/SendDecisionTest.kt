package com.mewbo.aura.ui.chat

import com.mewbo.aura.voice.InputModality
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression coverage for task fix-round-3 Important #3: `ChatViewModel.send()` used to set
 * `RunPhase.Sending` unconditionally, stomping a live `Streaming` phase on every steer into an
 * already-streaming run - and nothing downstream ever restored it, so the thinking spark and the
 * still-live streamed text rendered together for the rest of the steered turn.
 */
class SendDecisionTest {

    // ---- isSteering ----

    @Test
    fun `idle is not steering`() {
        assertFalse(SendDecision.isSteering(RunPhase.Idle))
    }

    @Test
    fun `done is not steering`() {
        assertFalse(SendDecision.isSteering(RunPhase.Done))
    }

    @Test
    fun `error is not steering`() {
        assertFalse(SendDecision.isSteering(RunPhase.Error))
    }

    @Test
    fun `sending is steering`() {
        assertTrue(SendDecision.isSteering(RunPhase.Sending))
    }

    @Test
    fun `streaming is steering`() {
        assertTrue(SendDecision.isSteering(RunPhase.Streaming))
    }

    // ---- phaseForSend - the exact matrix from the review ----

    @Test
    fun `fresh send (idle, no active stream) becomes Sending`() {
        assertEquals(RunPhase.Sending, SendDecision.phaseForSend(RunPhase.Idle, hasActiveStream = false))
    }

    @Test
    fun `steer with an active stream stays Streaming - the bug A regression`() {
        assertEquals(RunPhase.Streaming, SendDecision.phaseForSend(RunPhase.Streaming, hasActiveStream = true))
    }

    @Test
    fun `steer while the run is only Sending (no active stream yet) becomes Sending`() {
        assertEquals(RunPhase.Sending, SendDecision.phaseForSend(RunPhase.Sending, hasActiveStream = false))
    }

    @Test
    fun `phase Streaming but NO active stream job (stale phase) does not fake-preserve Streaming`() {
        // Defends the `hasActiveStream &&` half of the check specifically - a phase value alone
        // (e.g. left over from a cancelled/never-nulled streamJob) must never be trusted on its own.
        assertEquals(RunPhase.Sending, SendDecision.phaseForSend(RunPhase.Streaming, hasActiveStream = false))
    }

    @Test
    fun `send after Done or Error (retry) becomes Sending regardless of hasActiveStream`() {
        assertEquals(RunPhase.Sending, SendDecision.phaseForSend(RunPhase.Done, hasActiveStream = false))
        assertEquals(RunPhase.Sending, SendDecision.phaseForSend(RunPhase.Error, hasActiveStream = false))
    }

    // ---- modalityForSend ----

    @Test
    fun `a plain send() with no modality arg (its default, Text) tags a fresh turn Text`() {
        // Mirrors ChatViewModel.send(text) called with no explicit `modality` - the default
        // parameter value IS InputModality.Text, so this is what that call resolves to.
        assertEquals(
            InputModality.Text,
            SendDecision.modalityForSend(current = InputModality.Text, requested = InputModality.Text, isSteering = false),
        )
    }

    @Test
    fun `a fresh (non-steering) send always tags the turn with the requested modality`() {
        assertEquals(
            InputModality.Voice,
            SendDecision.modalityForSend(current = InputModality.Text, requested = InputModality.Voice, isSteering = false),
        )
    }

    @Test
    fun `a steer into an already-in-flight Voice turn leaves it readable as Voice through streaming`() {
        // The composer has no voice-steer path - `requested` here is always Text - but the steer
        // must never overwrite the ALREADY-Voice-tagged turn it's steering into.
        assertEquals(
            InputModality.Voice,
            SendDecision.modalityForSend(current = InputModality.Voice, requested = InputModality.Text, isSteering = true),
        )
    }

    @Test
    fun `the NEXT fresh send after a Voice turn finishes correctly re-tags Text - no stale leak`() {
        // "cleared/finalized correctly": once a Voice turn is Done/Error, isSteering is false again
        // (SendDecision.isSteering only holds for Sending/Streaming) - the next plain send must not
        // leave the prior turn's Voice tag stuck forever.
        assertEquals(
            InputModality.Text,
            SendDecision.modalityForSend(current = InputModality.Voice, requested = InputModality.Text, isSteering = false),
        )
    }

    // ---- shouldMuteOnStopSpeaking (§7.1) ----

    @Test
    fun `stopping a live Voice-turn speak-along (Sending) latches the mute`() {
        assertTrue(SendDecision.shouldMuteOnStopSpeaking(InputModality.Voice, RunPhase.Sending))
    }

    @Test
    fun `stopping a live Voice-turn speak-along (Streaming) latches the mute`() {
        assertTrue(SendDecision.shouldMuteOnStopSpeaking(InputModality.Voice, RunPhase.Streaming))
    }

    @Test
    fun `stopping a manual read-aloud of an OLDER message (turn already Done) does not mute`() {
        // The tap interrupted speech, but not THIS turn's own live voice speak-along - the turn
        // that originated activeTurnModality already finished.
        assertFalse(SendDecision.shouldMuteOnStopSpeaking(InputModality.Voice, RunPhase.Done))
        assertFalse(SendDecision.shouldMuteOnStopSpeaking(InputModality.Voice, RunPhase.Idle))
        assertFalse(SendDecision.shouldMuteOnStopSpeaking(InputModality.Voice, RunPhase.Error))
    }

    @Test
    fun `stopping speech during a Text-modality turn never mutes - nothing was ever speaking for it`() {
        assertFalse(SendDecision.shouldMuteOnStopSpeaking(InputModality.Text, RunPhase.Sending))
        assertFalse(SendDecision.shouldMuteOnStopSpeaking(InputModality.Text, RunPhase.Streaming))
    }
}
