package com.mewbo.aura.ui.orb

import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableFloatState
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.getValue
import androidx.compose.animation.core.withInfiniteAnimationFrameMillis
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.mewbo.aura.ui.theme.AuraMotion
import kotlin.math.PI
import kotlin.math.sin
import kotlinx.coroutines.isActive

/**
 * Lifecycle-gated, draw-phase-only time source shared by every AGSL-shader composable in the app
 * (orb, spark, aurora primitives): pauses the frame loop while the host lifecycle drops below
 * STARTED (backgrounded activity, dismissed overlay), and hands time back as a
 * [MutableFloatState] callers read only inside `onDrawBehind`/`drawWithCache` — so the continuous
 * per-frame tick never forces the calling composable itself to recompose. Extracted from orb v2
 * unchanged; every shader consumer previously hand-rolled this same lifecycle/frame-loop wiring.
 */
@Composable
internal fun rememberShaderTimeSeconds(): MutableFloatState {
    val timeSeconds = remember { mutableFloatStateOf(0f) }

    val lifecycle = LocalLifecycleOwner.current.lifecycle
    var isVisible by remember { mutableStateOf(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) }
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_START -> isVisible = true
                Lifecycle.Event.ON_STOP -> isVisible = false
                else -> Unit
            }
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }

    LaunchedEffect(isVisible) {
        if (!isVisible) return@LaunchedEffect
        while (isActive) {
            withInfiniteAnimationFrameMillis { frameMs -> timeSeconds.floatValue = frameMs / 1000f }
        }
    }

    return timeSeconds
}

/**
 * M8 reduced-motion "opacity breathe" (Gitea #181 fix wave, finding 4): a smooth sine envelope over
 * [rememberShaderTimeSeconds]'s own lifecycle-gated clock, replacing a second, ungated
 * `rememberInfiniteTransition()` per reduced-motion fallback ([com.mewbo.aura.ui.orb.Orb]'s
 * `ReducedMotionOrb`, `AuraSpark`'s `ReducedMotionSpark`) - the old fallback kept animating (and,
 * destructured with `by` at composable scope, recomposing its whole host composable every frame)
 * even while the host dropped below `STARTED`, missing this module's "frame loop pauses when not
 * STARTED" rule on exactly the accessibility path. Meant to be read only where every other shader
 * uniform in this family is read - `graphicsLayer{}`/`onDrawBehind{}`, never composable scope.
 * [min] is the trough (each brand mark measured its own; Orb and Spark differ); the peak is always
 * `1f`, matching the pre-#181 fallback's own range for each.
 */
internal fun reducedMotionBreatheAlpha(timeSeconds: Float, min: Float): Float {
    val periodSeconds = 2f * AuraMotion.reducedMotionBreatheHalfPeriodMs / 1000f
    val envelope = 0.5f + 0.5f * sin(2f * PI.toFloat() * timeSeconds / periodSeconds)
    return min + (1f - min) * envelope
}

/** Draw-phase-only rotation accumulator: integrates a (possibly crossfaded) angular speed every
 * frame so a state-driven speed change eases angular velocity instead of snapping it. Written and
 * read exclusively inside `onDrawBehind` by [Orb] and `AuraSpark` — never touches recomposition. */
internal class ShaderRotationAccumulator {
    var angleRadians = 0f
    var lastTimeSeconds = Float.NaN

    /** Advances [angleRadians] by `revsPerSec * 2*PI * dt` for the elapsed time since the last call
     * (clamped to 0.25s so a dropped-frame gap can't wind the angle forward in one jump), and
     * returns the updated angle. */
    fun advance(timeSeconds: Float, revsPerSec: Float): Float {
        val dt = if (lastTimeSeconds.isNaN()) 0f else (timeSeconds - lastTimeSeconds).coerceIn(0f, 0.25f)
        lastTimeSeconds = timeSeconds
        angleRadians = (angleRadians + revsPerSec * TWO_PI_RADIANS * dt) % TWO_PI_RADIANS
        return angleRadians
    }

    private companion object {
        const val TWO_PI_RADIANS = (2.0 * Math.PI).toFloat()
    }
}
