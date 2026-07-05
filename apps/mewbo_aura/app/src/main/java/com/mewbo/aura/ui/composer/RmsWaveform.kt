package com.mewbo.aura.ui.composer

import androidx.compose.animation.core.TweenSpec
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.common.TypingIndicator
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.LocalAssistantExtras

/**
 * Composer C3's live dictation meter (spec §6.2: "~28 vertical bars, 2dp wide, 3dp gap, heights
 * RMS-driven"). [rmsDb] is a pre-normalized 0f..1f amplitude - despite the name (kept to match the
 * brief's API sketch / `ComposerState.Dictation.rmsDb`), turning a real dBFS reading into this
 * range is a `voice/`-layer concern, out of this component's job (it only paints what it's given).
 *
 * Reused as-is by the assist overlay (Rev D-1: "C3-in-overlay shows bars inside the pill - same
 * component"), so this stays independent of [ComposerState]/[AuraComposer] - a caller only needs
 * an amplitude.
 */
@Composable
fun RmsWaveform(rmsDb: Float, modifier: Modifier = Modifier) {
    val reducedMotion = LocalAssistantExtras.current.reducedMotion
    if (reducedMotion) {
        // M8: "waveform -> 3-dot pulse". TypingIndicator already *is* exactly that pulse (ui/common)
        // - reusing it here is the DRY choice over hand-rolling a second one.
        TypingIndicator(modifier = modifier)
        return
    }

    val envelope by animateFloatAsState(
        targetValue = rmsDb.coerceIn(0f, 1f),
        animationSpec = RmsWaveformMath.envelopeSpec(),
        label = "rms-envelope",
    )

    Row(
        modifier = modifier,
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(RmsWaveformMath.BarGap),
    ) {
        repeat(RmsWaveformMath.BarCount) { index ->
            val fraction = RmsWaveformMath.heightFraction(index = index, smoothedRms = envelope)
            Box(
                modifier = Modifier
                    .width(RmsWaveformMath.BarWidth)
                    .height(RmsWaveformMath.MaxBarHeight * fraction)
                    .background(color = AuraColors.textPrimary, shape = RoundedCornerShape(RmsWaveformMath.BarWidth / 2)),
            )
        }
    }
}

/**
 * Pure bar geometry + timing, split out of [RmsWaveform] so the shape math is unit-testable
 * without Compose (module CLAUDE.md atomic-class paradigm).
 */
object RmsWaveformMath {
    const val BarCount: Int = 28
    val BarWidth: Dp = AuraSpacing.Composer.waveformBarWidth
    val BarGap: Dp = AuraSpacing.Composer.waveformBarGap
    val MaxBarHeight: Dp = AuraSpacing.Composer.waveformMaxBarHeight

    /** Rev E §E-3 (frame F6): "at rest/quiet the waveform renders as a row of small dots (bars
     * collapse to ~2dp dots)". A bar whose height equals its own width, fully corner-rounded (see
     * [RmsWaveform]'s `RoundedCornerShape(BarWidth / 2)`), draws as a perfect circle - so the rest
     * height is derived from the two size tokens themselves, not a fresh literal. */
    private val MinBarFraction = BarWidth.value / MaxBarHeight.value

    /**
     * Constant per-bar taper (index 0 = leftmost): tallest at center, shorter toward the edges.
     * This is the "waveform" shape itself; [heightFraction] scales it by the live signal.
     */
    fun shape(index: Int, barCount: Int = BarCount): Float {
        val center = (barCount - 1) / 2f
        val distance = if (center == 0f) 0f else kotlin.math.abs(index - center) / center
        return 1f - 0.6f * distance
    }

    /** Combines the constant per-bar [shape] with the live (already-[envelopeSpec]-decayed) RMS
     * value into a 0f..1f fraction of [MaxBarHeight]. */
    fun heightFraction(index: Int, smoothedRms: Float, barCount: Int = BarCount): Float {
        val clamped = smoothedRms.coerceIn(0f, 1f)
        return (MinBarFraction + (1f - MinBarFraction) * clamped * shape(index, barCount)).coerceIn(0f, 1f)
    }

    /** M2 (spec §7): "C3 waveform bar per-frame RMS decay" - [AuraMotion.rmsDecayMs] is the named
     * token; `tween` (not `spring`) because [AuraMotion.composerMorphSpring]-style constants are
     * Float-typed springs meant for discrete morphs, not a continuous per-frame envelope follower. */
    fun envelopeSpec(): TweenSpec<Float> = tween(durationMillis = AuraMotion.rmsDecayMs)
}
