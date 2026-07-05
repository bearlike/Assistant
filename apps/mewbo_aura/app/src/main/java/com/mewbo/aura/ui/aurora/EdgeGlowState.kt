package com.mewbo.aura.ui.aurora

/**
 * Closed union of [AuroraEdgeGlow] visual states (§6.10/§7.0/M1/M2/M3, geometry per Rev E §E-1).
 * [Igniting] carries the one-shot growth's own progress (0f = a single point at bottom-center,
 * 1f = the bloom has reached its resting extent, per the §7.0 timeline's 450ms budget); every other
 * state renders the bloom at full extent and differs only in intensity/hue-drift behavior.
 * Exhaustive `when` everywhere, no `else` branch.
 */
sealed interface EdgeGlowState {
    data object Hidden : EdgeGlowState
    data class Igniting(val progress: Float) : EdgeGlowState
    data class Listening(val rmsDb: Float) : EdgeGlowState
    data object Thinking : EdgeGlowState
}
