package com.mewbo.aura.ui.composer

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.common.TypingIndicator
import com.mewbo.aura.ui.theme.AuraColors
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
 *
 * [R5 2026-07-11] Fluid-synthesizer redesign: the old single `animateFloatAsState` envelope scaled
 * one fixed taper shape uniformly ("a triangle moving," per user feedback on 0.0.30-debug). Each
 * bar is now its own critically-damped spring chasing a per-bar, time-flowing pseudo-spectrum
 * target ([RmsWaveformMath.barTarget]), with neighbor coupling so energy visibly travels across
 * the row ([RmsWaveformMath.stepBars]). Rendering is ONE [Canvas] driven by a `withFrameNanos`
 * loop that mutates plain arrays and bumps a draw-phase-only tick - zero per-frame recomposition
 * (house draw-phase law, `ui/CLAUDE.md`).
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

    val rms by rememberUpdatedState(rmsDb.coerceIn(0f, 1f))
    val heights = remember { FloatArray(RmsWaveformMath.BarCount) { RmsWaveformMath.MinBarFraction } }
    val velocities = remember { FloatArray(RmsWaveformMath.BarCount) }
    // Draw-phase invalidation tick (the aurora/orb pattern): the frame loop mutates the arrays
    // and bumps this; only the Canvas draw lambda reads it, so nothing recomposes per frame.
    val frameTick = remember { mutableLongStateOf(0L) }
    LaunchedEffect(Unit) {
        var lastNanos = 0L
        var firstNanos = 0L
        while (true) {
            withFrameNanos { nanos ->
                val dt = if (lastNanos == 0L) 0f else (nanos - lastNanos) / 1_000_000_000f
                if (firstNanos == 0L) firstNanos = nanos
                lastNanos = nanos
                // Anchor to the first observed frame, not absolute Choreographer uptime: at
                // device-uptime magnitudes (days), Float32's ulp exceeds one frame's worth of
                // seconds (ulp(604800s) = 0.0625s at 7 days uptime), so t and t+16.7ms round to
                // the same Float and the flow field silently freezes/steps instead of flowing.
                RmsWaveformMath.stepBars(heights, velocities, rms, (nanos - firstNanos) / 1_000_000_000f, dt)
                frameTick.longValue++
            }
        }
    }

    val rowWidth = RmsWaveformMath.BarWidth * RmsWaveformMath.BarCount +
        RmsWaveformMath.BarGap * (RmsWaveformMath.BarCount - 1)
    val barColor = AuraColors.textPrimary
    Canvas(modifier = modifier.width(rowWidth).height(RmsWaveformMath.MaxBarHeight)) {
        frameTick.longValue // draw-phase read: invalidates this draw scope each simulation step
        val barWidthPx = RmsWaveformMath.BarWidth.toPx()
        val gapPx = RmsWaveformMath.BarGap.toPx()
        val maxHeightPx = RmsWaveformMath.MaxBarHeight.toPx()
        val radius = CornerRadius(barWidthPx / 2f, barWidthPx / 2f)
        for (i in 0 until RmsWaveformMath.BarCount) {
            val h = maxHeightPx * heights[i]
            drawRoundRect(
                color = barColor,
                topLeft = Offset(i * (barWidthPx + gapPx), (maxHeightPx - h) / 2f),
                size = Size(barWidthPx, h),
                cornerRadius = radius,
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
     * height is derived from the two size tokens themselves, not a fresh literal. Public: the
     * spring simulation's initial/rest-floor value ([RmsWaveform], tests). */
    val MinBarFraction: Float = BarWidth.value / MaxBarHeight.value

    // [R5 2026-07-11] Fluid-synthesizer dynamics - behavioral tuning constants (rates/ratios of
    // the simulation, the EdgeGlowUniformMath provenance convention), never design tokens.
    private const val FLOW_HZ = 0.9f // pseudo-spectrum travel rate across the row
    private const val PER_BAR_PHASE = 0.37f // per-bar noise phase offset (gives travel a direction)
    private const val SPRING_HZ = 6f // critically-damped follower response
    private const val COUPLING_PER_SECOND = 12f // neighbor energy diffusion
    private const val NOISE_FLOOR = 0.55f // target = rms * (floor + (1-floor)*noise) * shape
    private const val MAX_STEP_SECONDS = 1f / 20f

    // [R5 2026-07-11] The critically-damped spring (SPRING_HZ) is numerically stiff: a single
    // semi-implicit-Euler step at MAX_STEP_SECONDS overshoots the target and clamps every bar to
    // the ceiling instead of springing smoothly (verified: dt=1/20 from rest toward a mid target
    // overshoots to 1.59 before clamping) - the dt clamp alone does not make integration stable,
    // it only bounds how much SIMULATED time one call advances. SUB_STEP_SECONDS keeps each
    // internal integration step small enough to stay stable regardless of how large (post-clamp)
    // dtSeconds is, so a slow/janky frame (redroid's software renderer; a real dropped frame or GC
    // pause) still springs fluidly instead of slamming to max height uniformly across every bar.
    private const val SUB_STEP_SECONDS = 1f / 60f

    /** Deterministic 1-D value noise in [0,1] - pure Kotlin, JVM-testable, no Android deps. */
    fun valueNoise1d(x: Float): Float {
        val i = kotlin.math.floor(x)
        val f = x - i
        val u = f * f * (3f - 2f * f)
        return hash(i) * (1f - u) + hash(i + 1f) * u
    }

    private fun hash(n: Float): Float {
        val s = kotlin.math.sin(n * 127.1f) * 43758.5453f
        return s - kotlin.math.floor(s)
    }

    /**
     * Constant per-bar taper (index 0 = leftmost): tallest at center, shorter toward the edges.
     * This is the "waveform" shape itself; [barTarget] scales it by the live signal.
     */
    fun shape(index: Int, barCount: Int = BarCount): Float {
        val center = (barCount - 1) / 2f
        val distance = if (center == 0f) 0f else kotlin.math.abs(index - center) / center
        return 1f - 0.6f * distance
    }

    /** [R5] Per-bar target at [timeSeconds]: a slowly FLOWING pseudo-spectrum (only RMS exists -
     * no FFT), tapered by [shape]. At rms 0 this is exactly [MinBarFraction] for every bar (the
     * Rev E dot-row law, preserved). */
    fun barTarget(index: Int, rms: Float, timeSeconds: Float, barCount: Int = BarCount): Float {
        val clamped = rms.coerceIn(0f, 1f)
        val noise = valueNoise1d(index * PER_BAR_PHASE + timeSeconds * FLOW_HZ)
        val level = clamped * (NOISE_FLOOR + (1f - NOISE_FLOOR) * noise) * shape(index, barCount)
        return (MinBarFraction + (1f - MinBarFraction) * level).coerceIn(0f, 1f)
    }

    /** [R5] One simulation step, mutating [heights]/[velocities] in place: critically-damped
     * spring toward [barTarget] per bar, then neighbor coupling so energy visibly travels.
     * [dtSeconds] is clamped to [MAX_STEP_SECONDS] so a dropped frame can't explode the
     * integration, then internally sub-stepped at [SUB_STEP_SECONDS] so the stiff spring stays
     * numerically stable (see that constant's KDoc) regardless of how large a single caller-side
     * frame gap is; velocity is zeroed at the dot floor so bars settle instead of sticking. */
    fun stepBars(heights: FloatArray, velocities: FloatArray, rms: Float, timeSeconds: Float, dtSeconds: Float) {
        var remaining = dtSeconds.coerceIn(0f, MAX_STEP_SECONDS)
        var t = timeSeconds
        while (remaining > 0f) {
            val sub = minOf(remaining, SUB_STEP_SECONDS)
            stepBarsOnce(heights, velocities, rms, t, sub)
            t += sub
            remaining -= sub
        }
    }

    private fun stepBarsOnce(heights: FloatArray, velocities: FloatArray, rms: Float, timeSeconds: Float, dt: Float) {
        val omega = 2f * kotlin.math.PI.toFloat() * SPRING_HZ
        val prev = heights.copyOf()
        for (i in heights.indices) {
            val target = barTarget(i, rms, timeSeconds, heights.size)
            velocities[i] += (omega * omega * (target - heights[i]) - 2f * omega * velocities[i]) * dt
            val left = prev.getOrElse(i - 1) { prev[i] }
            val right = prev.getOrElse(i + 1) { prev[i] }
            velocities[i] += COUPLING_PER_SECOND * (left + right - 2f * prev[i]) * dt
            heights[i] = (heights[i] + velocities[i] * dt).coerceIn(MinBarFraction, 1f)
            if (heights[i] <= MinBarFraction && velocities[i] < 0f) velocities[i] = 0f
        }
    }
}
