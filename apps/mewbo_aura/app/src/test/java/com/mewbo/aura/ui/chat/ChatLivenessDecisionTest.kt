package com.mewbo.aura.ui.chat

import com.mewbo.aura.ui.aurora.EdgeGlowState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The two pure liveness decisions the chat transcript / screen make each frame:
 * - [shouldShowDisclaimer] —: the bottom disclaimer must appear only once a reply has
 *   settled AND no run is live (never under a fresh user bubble on a follow-up turn).
 * - [chatGlowState] —: the bottom aurora glow re-arms its bounded ambient breathe on
 *   every run completion, not just on a fresh empty screen.
 * Both are extracted from their composables precisely so this regression-prone area (disclaimer
 * placement has shipped wrong three times, DESIGN.md §7 registry #7) has a contract test.
 */
class ChatLivenessDecisionTest {

    // ---- disclaimer gate ----

    @Test
    fun `disclaimer hidden until a reply settles`() {
        assertFalse(shouldShowDisclaimer(hasSettledReply = false, isRunLive = false))
    }

    @Test
    fun `disclaimer hidden while a run is live even after a prior reply settled`() {
        // The bug: on a follow-up turn a prior reply is settled (hasSettledReply=true) but the
        // new turn is live — the caption must NOT render under the fresh user bubble ahead of the
        // new response.
        assertFalse(shouldShowDisclaimer(hasSettledReply = true, isRunLive = true))
    }

    @Test
    fun `disclaimer shows once settled and not live`() {
        assertTrue(shouldShowDisclaimer(hasSettledReply = true, isRunLive = false))
    }

    // ---- glow-state completion re-arm ----

    @Test
    fun `a live run shows Thinking regardless of the linger flags`() {
        assertEquals(
            EdgeGlowState.Thinking,
            chatGlowState(
                runPhase = RunPhase.Streaming,
                freshInvocationActive = true,
                completionLingerActive = true,
                transcriptEmpty = true,
                loadingHistory = false,
            ),
        )
        assertEquals(
            EdgeGlowState.Thinking,
            chatGlowState(
                runPhase = RunPhase.Sending,
                freshInvocationActive = false,
                completionLingerActive = false,
                transcriptEmpty = false,
                loadingHistory = false,
            ),
        )
    }

    @Test
    fun `fresh invocation breathes only on an empty settled transcript`() {
        assertEquals(
            EdgeGlowState.Listening(0f),
            chatGlowState(
                runPhase = RunPhase.Idle,
                freshInvocationActive = true,
                completionLingerActive = false,
                transcriptEmpty = true,
                loadingHistory = false,
            ),
        )
        // A resumed session with history does NOT re-breathe on entry (transcript non-empty).
        assertEquals(
            EdgeGlowState.Hidden,
            chatGlowState(
                runPhase = RunPhase.Idle,
                freshInvocationActive = true,
                completionLingerActive = false,
                transcriptEmpty = false,
                loadingHistory = false,
            ),
        )
        // Still loading history: no breathe yet even though the transcript is momentarily empty.
        assertEquals(
            EdgeGlowState.Hidden,
            chatGlowState(
                runPhase = RunPhase.Idle,
                freshInvocationActive = true,
                completionLingerActive = false,
                transcriptEmpty = true,
                loadingHistory = true,
            ),
        )
    }

    @Test
    fun `completion linger breathes on a non-empty transcript`() {
        // The re-arm: after a response lands (Done, non-empty transcript, fresh window already
        // elapsed), the armed completion linger still shows the ambient breathe — the empty-transcript
        // gate that scopes the FRESH-invocation breathe must not suppress THIS one.
        assertEquals(
            EdgeGlowState.Listening(0f),
            chatGlowState(
                runPhase = RunPhase.Done,
                freshInvocationActive = false,
                completionLingerActive = true,
                transcriptEmpty = false,
                loadingHistory = false,
            ),
        )
    }

    @Test
    fun `rest is solid when nothing is armed`() {
        assertEquals(
            EdgeGlowState.Hidden,
            chatGlowState(
                runPhase = RunPhase.Done,
                freshInvocationActive = false,
                completionLingerActive = false,
                transcriptEmpty = false,
                loadingHistory = false,
            ),
        )
    }
}
