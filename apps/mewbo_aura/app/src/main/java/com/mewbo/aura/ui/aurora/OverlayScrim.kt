package com.mewbo.aura.ui.aurora

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import com.mewbo.aura.ui.theme.AuraColors

/**
 * Context-preserving scrim over the live app during overlay invocation [R4 2026-07-10] —
 * supersedes the flat 60%-black Rev E fill: the underlying app stays legible (8–10% dim over its
 * top 60%), with one soft bottom-concentrated ramp (55% at the bottom edge) guaranteeing contrast
 * for the pill, companion chips, and the resting glow over arbitrary bright content. Static and
 * state-free by design: the §7.0 timeline's 180ms fade belongs to the overlay host's own
 * `AnimatedVisibility`, not this primitive.
 *
 * Desaturating what's beneath (§3.3) remains intentionally NOT implemented: a
 * `VoiceInteractionSession` window has no compositor hook onto the already-composited frame
 * beneath it. Documented here rather than silently dropped.
 */
@Composable
fun OverlayScrim(modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(
                    colorStops = AuraColors.scrimOverlayGradient
                        .map { it.fraction to it.color }
                        .toTypedArray(),
                ),
            ),
    )
}
