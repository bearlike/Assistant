package com.mewbo.aura.ui.theme

import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Design-v2 shape tokens (spec §5) — pill geometry everywhere, no sharp corners anywhere in the
 * product (spec §1.4). Exactly the three radii the spec names; if a surface needs a shape this
 * set doesn't cover, that's a signal to report, not to add a literal at the call site.
 */
object AuraShape {
    /**
     * Stadium/fully-rounded — composer, chips, toasts, drawer selected pill, top-bar icon scrims.
     * [CircleShape] is `RoundedCornerShape(50%)`: on a wide element it renders a true stadium
     * (both ends fully round regardless of height), which is what "999dp" is normally faking in
     * systems without percent-based corners — Compose doesn't need the magic number.
     */
    val radiusPill: Shape = CircleShape

    /** 28dp — user message bubble. */
    val radiusBubble: Dp = 28.dp

    /** 16dp — small square icon tiles (waveform status tile). */
    val radiusThumb: Dp = 16.dp

    /** 20dp — the assist overlay's Streaming-state response card (Gitea #181 P3, measured capture).
     * Distinct from [radiusBubble]'s 28dp: the card is a smaller, tighter-cornered floating surface,
     * not a message bubble. */
    val radiusCard: Dp = 20.dp

    /**
     * M3 [Shapes] bridge for pre-migration call sites outside `ui/theme/` that still read
     * `MaterialTheme.shapes.*` (`ui/chat`, `ui/sessions` — outside this task's file ownership).
     * `extraSmall`/`small` keep the pre-design-v2 8dp value: the spec's 3-token set has no "small"
     * analog, and inventing one isn't this task's call (globals.md: report a gap, don't invent a
     * value). Simplify once those surfaces read [radiusPill]/[radiusBubble]/[radiusThumb] directly.
     */
    private val legacySmallRadius = 8.dp

    val Shapes = Shapes(
        extraSmall = RoundedCornerShape(legacySmallRadius),
        small = RoundedCornerShape(legacySmallRadius),
        medium = RoundedCornerShape(radiusThumb),
        large = RoundedCornerShape(radiusBubble),
        extraLarge = RoundedCornerShape(radiusBubble),
    )
}
