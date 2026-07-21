package com.mewbo.aura.ui.aurora

/**
 * Closed union of [AuroraWashTop] visual states (§3.3/M3/M4; [Resting] added).
 * `Hidden` alone renders nothing; the three active states ([Resting]/[Thinking]/[Streaming]) share
 * the same target intensity ramp and differ only in hue-drift rate (see `AuroraWashUniformMath`) —
 * exhaustive `when` everywhere, no `else` branch. [Resting] is the landing-page/idle-composer wash
 * (real captures show it visibly present even with nothing generating — supersedes the old
 * "wash only during runs, [AuroraGlowBottom] disc at rest" pairing, which had it backwards).
 */
sealed interface AuroraState {
    data object Hidden : AuroraState
    data object Resting : AuroraState
    data object Thinking : AuroraState
    data object Streaming : AuroraState
}
