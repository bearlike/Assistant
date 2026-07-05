package com.mewbo.aura.ui.orb

/**
 * Closed union of [AuraSpark] visual states (D-2). `Shimmer` is the greeting-screen loop; `Thinking`
 * replaces the Rev C `LoadingIndicator` in-chat (M3) — never both at once, so exactly two states.
 */
sealed interface SparkState {
    data object Shimmer : SparkState
    data object Thinking : SparkState
}
