package com.mewbo.aura.ui.aurora

/**
 * Closed union of [AuroraEdgeGlow] visual states (§6.10/§7.0/M1/M2/M3, geometry per Rev E §E-1).
 * [Igniting] carries the one-shot growth's own progress (0f = a single point at bottom-center,
 * 1f = the bloom has reached its resting extent, per the §7.0 timeline's 450ms budget); every other
 * state renders the bloom at full extent and differs only in intensity/hue-drift behavior. [Resting]
 * (OVERLAY-scoped, [R4 2026-07-10]) is the session-lifetime "assistant present" pool between an
 * active run and dismissal — see its own KDoc for the scope disclaimer. Exhaustive `when`
 * everywhere, no `else` branch.
 */
sealed interface EdgeGlowState {
    data object Hidden : EdgeGlowState
    data class Igniting(val progress: Float) : EdgeGlowState
    data class Listening(val rmsDb: Float) : EdgeGlowState
    data object Thinking : EdgeGlowState

    /** Session-lifetime resting pool [R4 2026-07-10, OVERLAY-scoped]: the first turn has settled
     * (`Streaming.done`) but the overlay is still on screen — a low, static bottom pool signals
     * "the assistant is present" without demanding attention. NEVER used by the chat surface
     * (DESIGN.md §5/§7.2 solid-resting law is untouched: `ChatScreen` never maps anything to this
     * state; an on-screen overlay is a live surface, the chat surface's rest is not). */
    data object Resting : EdgeGlowState
}
