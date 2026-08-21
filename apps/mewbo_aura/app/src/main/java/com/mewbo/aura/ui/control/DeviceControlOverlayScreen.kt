package com.mewbo.aura.ui.control

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.wrapContentHeight
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.data.device.DeviceShape
import com.mewbo.aura.ui.aurora.AuroraEdgeGlow
import com.mewbo.aura.ui.aurora.EdgeGlowState
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlinx.coroutines.delay

/**
 * How long the decoration — the glow and the narration stack — takes to leave when the grant ends.
 * Its arrival rides the same [AuraMotion.deviceControlEaseMs] window, so the surface is symmetric.
 *
 * **This replaces an earlier quick 180ms exit, and that decision is resolved here rather than
 * dropped.** The argument for the short window was that a grant ending is deliberate and
 * user-initiated, so a glow still fading seconds after Stop reads as "it did not stop". That is
 * true of the AFFORDANCE, and the affordance is the Stop pill: it is the one thing on this surface
 * asserting the phone can still be taken over, so it leaves promptly on
 * [DEVICE_CONTROL_PILL_DISMISS_MS] and its window is REMOVED, which is the user's confirmation that
 * the tap landed. What is left easing off is a decaying glow with nothing on it to press — an
 * afterglow rather than a claim.
 *
 * It was never a cut and still is not: an abrupt on->off luminance change is a photosensitivity
 * trigger (DESIGN.md), which is why the ramp is linear at both ends. [DeviceControlOverlay] waits
 * out [DEVICE_CONTROL_EXIT_MS] before taking a window down, so no fade can outlive its surface.
 */
internal const val DEVICE_CONTROL_DISMISS_MS: Int = AuraMotion.deviceControlEaseMs

/**
 * The Stop pill's own exit — deliberately the short one, and the reason the long one above is safe.
 *
 * A pressed affordance still sitting there two seconds later invites a second press, and the window
 * behind it keeps its touch region over an app the user is already reaching past. Only the pill's
 * EXIT is quick; its entrance rides the shared window with everything else, because nothing about
 * an arriving surface is ambiguous.
 */
internal const val DEVICE_CONTROL_PILL_DISMISS_MS: Int = AuraMotion.scrimFadeMs

/**
 * What [DeviceControlOverlay] must wait out before this surface may be torn down: the LONGEST exit
 * on it, DERIVED rather than restated.
 *
 * A teardown shorter than an animation still in flight rips the window out mid-fade, which is
 * exactly the abrupt cut the fade exists to remove — and a second literal is how that happens the
 * next time either number is retuned.
 */
internal val DEVICE_CONTROL_EXIT_MS: Int =
    maxOf(DEVICE_CONTROL_DISMISS_MS, DEVICE_CONTROL_PILL_DISMISS_MS)

/** Air the bubble stack leaves under itself for the Stop pill, which lives in a SEPARATE,
 * touchable window at the same bottom edge (see [DeviceControlOverlay]). Derived from the pill's
 * own tokens rather than restated, so moving the pill moves the stack with it. */
private val BUBBLE_STACK_BOTTOM_INSET: Dp =
    AuraSpacing.Composer.bottomInset + AuraSpacing.ActionRow.cellSize + AuraSpacing.Composer.gapTight

/** TalkBack's label. The visible "Stop" is the notification action's own word, kept identical
 * because both tap the SAME seam; the spoken label says what is being stopped, which "Stop" alone
 * over somebody else's app does not. */
private const val STOP_A11Y_LABEL = "Stop Mewbo controlling this device"

/**
 * The pass-through layer: the edge glow that says an agent is driving this phone, plus whatever it
 * is currently saying.
 *
 * Hosted in a `FLAG_NOT_TOUCHABLE` window, so nothing here can take a touch — which is the point.
 * The taps `device_action` injects have to reach the app underneath, and a window that ate them
 * would break the very feature it exists to announce.
 *
 * [visible] goes false BEFORE the window is removed, so the surface eases off in place instead of
 * disappearing with it; see [DEVICE_CONTROL_DISMISS_MS]. It drives the ENVELOPE and nothing else —
 * the glow is deliberately never handed `Hidden` on the way out, because the shader's own dismiss
 * ramp underneath this one would multiply into a curve with a fast final segment.
 *
 * **[visible] and [narrating] are different questions and the glow answers only the first.** The
 * glow is up for the whole grant wherever the user is, because a shell-UID takeover is otherwise
 * invisible; the bubbles are the part that would merely repeat a transcript the user is already
 * reading, so they are the part that stands down in the app.
 *
 * [shape] arrives as a PARAMETER rather than through `LocalDeviceShape`, and that is load-bearing:
 * this tree is hosted in a raw `WindowManager` view, not under `MainActivity`, so nothing provides
 * that local here and reading it would silently return the `Handheld` default with nothing
 * reporting it. The window controller injects the shape and hands it down.
 */
@Composable
fun DeviceControlAura(
    bubbles: List<ControlBubble>,
    visible: Boolean,
    narrating: Boolean,
    shape: DeviceShape,
    modifier: Modifier = Modifier,
) {
    val alpha = rememberSurfaceAlpha(visible = visible, exitMs = DEVICE_CONTROL_DISMISS_MS)
    // The SHADER's own entrance ramp has to start from "not showing", and only a composition that
    // has already happened can provide that: animateFloatAsState initialises AT its target, so a
    // glow composed already-Listening has its internal envelope at full on frame one. The surface
    // envelope above would hide that anyway — this keeps the two rising together rather than
    // leaving one at rest under the other.
    var entered by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { entered = true }

    Box(
        modifier = modifier
            .fillMaxSize()
            // ONE envelope over the whole decoration, read in the LAYER phase — so seconds of fade
            // cost no recomposition, and the glow and the narration stack cannot arrive or leave
            // at different times because there is only one value to arrive or leave by.
            .graphicsLayer { this.alpha = alpha.value },
    ) {
        // The overlay values verbatim: the full multi-hue field and the persistent edge-lit
        // perimeter (ui/aurora/CLAUDE.md Rule 4b + the perimeter floor). Listening's WIDE reach
        // and slow breathe is the "alive, not urgent" profile — the same one a fresh invocation
        // uses. Thinking's contracted, corner-suppressed hug is shaped around a composer pill that
        // is not on screen here, and Resting is deliberately static, which would read as nothing
        // happening while an agent is actively driving. Reduced motion flattens the breathe and
        // freezes the field inside the composable, so nothing needs gating at this call site.
        // Not gated on `visible`: the surface envelope owns the exit, and handing the shader
        // `Hidden` as well would run its own short dismiss underneath the long one.
        AuroraEdgeGlow(
            state = if (entered) EdgeGlowState.Listening(rmsDb = 0f) else EdgeGlowState.Hidden,
            colors = AuraColors.auroraOverlayLiveBloom,
            hueDriftAmount = 1f,
            perimeterPresence = 1f,
            // The BORDER profile, and this surface is the only caller that asks for it: a bottom
            // bloom is shaped around a composer pill, and there is none here — what has to be
            // legible is that the whole screen belongs to an agent right now. One knob rather than
            // six, because each of its effects alone re-opens the imbalance it exists to fix
            // (ui/aurora/CLAUDE.md § "The border profile").
            perimeterBias = 1f,
            // Both constants, never re-targeted mid-flight, so neither can jump the wave's phase.
            speedScale = AuraMotion.deviceControlFlowScale,
            // The border profile runs its side rails the FULL height of the surface, which reads as
            // a frame on a tall handheld and as a wash over most of a short, wide 16:9 panel. The
            // shape owns how far this may rise; `Handheld` answers 0f, which the shader skips
            // entirely, so that side is byte-identical.
            riseFraction = shape.controlAuraRiseFraction,
        )
        ControlBubbleStack(
            // Emptied rather than skipped, so the stack keeps its place in the layout and a line
            // arriving the moment the user leaves the app fades in where the last one was.
            bubbles = if (narrating) bubbles else emptyList(),
            modifier = Modifier.align(Alignment.BottomCenter),
        )
    }
}

/**
 * The one entrance/exit envelope every component of this surface rides — 0 while it is not there,
 * 1 while it is, and a linear ramp between.
 *
 * **[Animatable], not `animateFloatAsState`, and that is the entrance.** `animateFloatAsState`
 * initialises AT its target, so a surface composed already-[visible] would be at full strength on
 * its first frame with nothing left to animate; an `Animatable(0f)` genuinely starts at 0 whatever
 * it is first told to do.
 *
 * Callers read it in the LAYER phase (`graphicsLayer`), so a fade running at frame rate never
 * recomposes what it is fading — the same rule [ControlBubbleRow] follows.
 *
 * Reduced motion FLATTENS this, never removes it: a fade is opacity rather than travel (DESIGN.md),
 * and a cut is the photosensitivity trigger in either direction. [exitMs] is per-component because
 * the components do not leave together — see [DEVICE_CONTROL_PILL_DISMISS_MS] — while the arrival
 * is one shared window for all of them.
 */
@Composable
private fun rememberSurfaceAlpha(visible: Boolean, exitMs: Int): State<Float> {
    val reducedMotion = LocalAssistantExtras.current.reducedMotion
    val alpha = remember { Animatable(0f) }
    LaunchedEffect(visible, reducedMotion, exitMs) {
        val durationMs = when {
            reducedMotion -> AuraMotion.reducedBlockFadeMs
            visible -> AuraMotion.deviceControlEaseMs
            else -> exitMs
        }
        // Linear at both ends: a ramp with a fast segment reads as the flash it replaced.
        alpha.animateTo(if (visible) 1f else 0f, tween(durationMs, easing = LinearEasing))
    }
    return alpha.asState()
}

@Composable
private fun ControlBubbleStack(bubbles: List<ControlBubble>, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .wrapContentHeight()
            .windowInsetsPadding(WindowInsets.navigationBars)
            .padding(horizontal = AuraSpacing.screenGutter)
            .padding(bottom = BUBBLE_STACK_BOTTOM_INSET),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
    ) {
        bubbles.forEach { bubble ->
            key(bubble.id) { ControlBubbleRow(bubble) }
        }
    }
}

/**
 * One line, fading itself in and back out.
 *
 * ONE [Animatable] owns both ends, which is what keeps the fold pure: the fade-out starts BEFORE
 * [AuraMotion.transientDismissMs] elapses — the SAME window `expire` drops the line at — so by the
 * time the fold drops it, it is already invisible and its removal is not a visible cut. Re-keying
 * on [ControlBubble.shownAtMs] restarts the timer every time a delta refreshes the line, so a
 * sentence still being written never fades out from under the reader.
 */
@Composable
private fun ControlBubbleRow(bubble: ControlBubble) {
    val reducedMotion = LocalAssistantExtras.current.reducedMotion
    // A fade is opacity, not travel, so reduced motion KEEPS it and only flattens the duration —
    // the same rule the transcript rows follow (DESIGN.md).
    val fadeMs = if (reducedMotion) AuraMotion.reducedBlockFadeMs else AuraMotion.actionRowFadeMs
    val alpha = remember(bubble.id) { Animatable(0f) }

    LaunchedEffect(bubble.id, bubble.shownAtMs) {
        alpha.animateTo(1f, tween(fadeMs))
        delay((AuraMotion.transientDismissMs - 2L * fadeMs).coerceAtLeast(0L))
        alpha.animateTo(0f, tween(fadeMs))
    }

    Box(
        modifier = Modifier
            // The animated value is read inside graphicsLayer's block, i.e. in the LAYER phase —
            // so a fade running at frame rate never recomposes this row.
            .graphicsLayer { this.alpha = alpha.value }
            .clip(AuraShape.radiusPill)
            .background(AuraColors.surfaceNotice)
            .padding(
                horizontal = AuraSpacing.Composer.internalPadding,
                vertical = AuraSpacing.Composer.gapTight,
            ),
    ) {
        Text(
            text = bubble.text,
            style = AuraType.chipLabel,
            color = AuraColors.textSecondary,
            // The fold already caps the length; this is the belt for a narrow screen, never the
            // primary shortening (which has to happen on the TEXT so it can keep the tail).
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * The user's off-switch: the ONE thing on this surface that takes a touch.
 *
 * It lives in its OWN small window, and that is the whole reason touch pass-through works — a
 * `FLAG_NOT_TOUCH_MODAL` window only consumes touches inside its own bounds, and a single
 * full-screen window has no supported way to be selectively transparent to touch. Everything
 * decorative sits in the separate `FLAG_NOT_TOUCHABLE` layer.
 *
 * It arrives on the surface's shared window and leaves on its own short one
 * ([DEVICE_CONTROL_PILL_DISMISS_MS]) — the asymmetry IS the design: an arriving off-switch is never
 * ambiguous, while one that lingers after a press reads as a press that did nothing. It stays
 * tappable throughout its entrance, opacity being no part of where a window's touch region is, so
 * an agent driving the phone can be stopped from the first frame.
 */
@Composable
fun DeviceControlStopPill(visible: Boolean, onStop: () -> Unit, modifier: Modifier = Modifier) {
    val alpha = rememberSurfaceAlpha(visible = visible, exitMs = DEVICE_CONTROL_PILL_DISMISS_MS)
    Box(
        modifier = modifier
            .graphicsLayer { this.alpha = alpha.value }
            .windowInsetsPadding(WindowInsets.navigationBars)
            .padding(bottom = AuraSpacing.Composer.bottomInset),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                // The a11y touch floor, and the same height the bubble stack reserves above it.
                .height(AuraSpacing.ActionRow.cellSize)
                .clip(AuraShape.radiusPill)
                .background(AuraColors.surfaceOverlayPill)
                .clickable(onClick = onStop)
                .padding(horizontal = AuraSpacing.Composer.internalPadding)
                .semantics { contentDescription = STOP_A11Y_LABEL },
        ) {
            Icon(
                imageVector = Icons.Filled.Stop,
                contentDescription = null,
                tint = AuraColors.iconPrimary,
                modifier = Modifier.size(AuraSpacing.ActionRow.iconSize),
            )
            Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
            Text(
                text = "Stop",
                style = AuraType.chipLabel,
                color = AuraColors.textPrimary,
            )
        }
    }
}
