package com.mewbo.aura.ui.overlay

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.mewbo.aura.ui.aurora.EdgeGlowState
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.voice.AssistUiState
import kotlinx.coroutines.delay

/**
 * §7.0's bottom-anchored edge-glow choreography, decoupled from [AssistUiState] itself (this is
 * pure UI timing, screenshot-verified per the task brief, not something [AssistTurnMachine] should
 * model). Runs the 450ms ignition sweep exactly once, the first time [state] leaves [AssistUiState.Idle],
 * then tracks state continuously afterward.
 */
@Composable
internal fun rememberEdgeGlowState(state: AssistUiState, reducedMotion: Boolean): EdgeGlowState {
    var igniting by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf(1f) }
    val wasIdle = remember { mutableStateOf(true) }
    val isIdle = state is AssistUiState.Idle

    LaunchedEffect(isIdle) {
        if (wasIdle.value && !isIdle) {
            if (reducedMotion) {
                progress = 1f // M8: static bloom frame, no growth animation.
            } else {
                igniting = true
                val steps = 30
                repeat(steps + 1) { i ->
                    progress = i / steps.toFloat()
                    delay(AuraMotion.edgeSweepMs.toLong() / steps)
                }
                igniting = false
            }
        }
        wasIdle.value = isIdle
    }

    return when {
        isIdle -> EdgeGlowState.Hidden
        igniting -> EdgeGlowState.Igniting(progress)
        // READY is the resting "shown, nothing typed yet" state - reuses the same ambient
        // "edge alive" breathe (0 rms) Listening uses, since nothing is actually listening yet.
        state is AssistUiState.Ready -> EdgeGlowState.Listening(0f)
        state is AssistUiState.Listening -> EdgeGlowState.Listening(state.rmsDb)
        state is AssistUiState.Sending -> EdgeGlowState.Thinking
        // ACTIVE GENERATION stays in the CONTRACTED Thinking profile (bloom hugs the pill, no
        // corner reach) - mapping it to the WIDE ambient Listening breathe would invert the
        // listening-vs-generating relationship on screen. Settling to done RESTS instead of
        // hiding: a low, static bottom pool signals "the assistant is still present" for as long
        // as the overlay stays on screen, rather than going dark the instant the reply finishes.
        // Error stays Hidden (§6.12 "failure is quiet" - unchanged below).
        state is AssistUiState.Streaming && !state.done -> EdgeGlowState.Thinking
        state is AssistUiState.Streaming -> EdgeGlowState.Resting
        state is AssistUiState.Error -> EdgeGlowState.Hidden // §6.12: failure is quiet, no aurora treatment.
        else -> EdgeGlowState.Hidden // unreachable - every AssistUiState variant is covered above;
        // a boolean-condition `when` can't prove that itself the way `when(state)` could.
    }
}

/** Bloom envelope: snaps to 1 the moment the overlay leaves Idle (its alpha rides
 * [AuroraEdgeGlow]'s own visible ramp), holds through the 450ms ignite, then exhales to 0 over
 * [AuraMotion.bloomSettleMs] (FastOutSlowIn). Reduced motion never raises it; [AuroraEdgeGlow]
 * forces 0 too. The Idle reset is DELAYED by [OVERLAY_GLOW_DISMISS_MS] so a mid-bloom dismissal
 * fades WITH the glow's own hold-frame dismiss fade instead of snapping the perimeter to 0 a frame
 * early. A rapid re-invocation restarts this effect and cancels that pending reset harmlessly -
 * `wasIdle` is committed BEFORE the delay, so the ignition branch above still snaps the bloom back
 * to 1 on the next pass regardless. */
@Composable
internal fun rememberPerimeterBloom(state: AssistUiState, reducedMotion: Boolean): State<Float> {
    val bloom = remember { Animatable(0f) }
    val isIdle = state is AssistUiState.Idle
    val wasIdle = remember { mutableStateOf(true) }
    LaunchedEffect(isIdle) {
        if (wasIdle.value && !isIdle && !reducedMotion) {
            bloom.snapTo(1f)
            delay(AuraMotion.edgeSweepMs.toLong())
            bloom.animateTo(0f, tween(AuraMotion.bloomSettleMs, easing = FastOutSlowInEasing))
        }
        // Commit wasIdle BEFORE the suspending Idle reset below: if a rapid re-invocation restarts
        // this effect mid-delay, the pending snapTo(0) is cancelled, but wasIdle is already recorded,
        // so the ignition branch snaps the bloom to 1 on that next pass — the cancelled reset is a
        // no-op either way.
        wasIdle.value = isIdle
        if (isIdle) {
            delay(OVERLAY_GLOW_DISMISS_MS.toLong())
            bloom.snapTo(0f)
        }
    }
    return bloom.asState()
}
