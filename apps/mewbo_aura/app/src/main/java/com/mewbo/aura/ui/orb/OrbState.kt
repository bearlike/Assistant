package com.mewbo.aura.ui.orb

/** Closed union of orb visual states (§7). Exhaustive `when` everywhere — no `else` branch. */
sealed interface OrbState {
    data object Idle : OrbState
    data class Listening(val rmsDb: Float) : OrbState
    data object Thinking : OrbState
    data object Error : OrbState
}
