package com.mewbo.aura.ui.aurora

import android.graphics.RuntimeShader
import androidx.compose.animation.core.AnimationSpec
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.graphics.ShaderBrush
import com.mewbo.aura.ui.orb.GlslNoise
import com.mewbo.aura.ui.orb.rememberShaderTimeSeconds
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.LocalAssistantExtras

// AGSL (RuntimeShader, API 33+). A full-bleed top wash: gold blended to green across the width
// (pixel-sampled from four real-device captures — supersedes the Rev C "purple↔amber"
// eyeballed estimate, which no capture ever showed), fading to nothing by
// [AuraColors.auroraWashTopFadeHeightFraction] of the screen height (§3.3, S1). The color "drift"
// (M4: "drifting hue slowly") is approximated cheaply by sliding the gold/green blend boundary
// left-right over time rather than a full HSV hue rotation — visually reads as the gradient
// breathing/living, at a fraction of the shader cost. The shared [GlslNoise.ditherPremul] breaks 8-bit
// banding on the smooth vertical falloff (the one place this brief calls out banding risk
// explicitly; reconfirmed zero banding in every real capture, so this stays mandatory).
private val AURORA_WASH_SHADER_SRC = """
uniform float2 iResolution;
uniform float iTime;
uniform float iDriftHz;
uniform float iIntensity;
uniform float iFadeHeightFraction;
uniform float3 iColorGold;
uniform float3 iColorGreen;

${GlslNoise.GLSL_CORE}

half4 main(float2 fragCoord) {
    float2 uv = fragCoord / iResolution;

    float driftPhase = 0.5 + 0.5 * sin(6.2831853 * iDriftHz * iTime);
    float mixT = clamp(uv.x + (driftPhase - 0.5) * 0.3, 0.0, 1.0);
    float3 col = mix(iColorGold, iColorGreen, mixT);

    // smoothstep keeps the vertical falloff itself banding-free.
    float verticalFade = 1.0 - smoothstep(0.0, iFadeHeightFraction, uv.y);

    float alpha = clamp(verticalFade * iIntensity, 0.0, 1.0);

    // Mandatory dither (Rule 3), via the ONE shared primitive, on the PREMULTIPLIED colour (see
    // [GlslNoise.ditherPremul]: dithering before the `* alpha` scales the perturbation by alpha and
    // neuters it exactly where the falloff is flattest). This replaces a hand-rolled
    // `valueNoise(fragCoord * 0.5)` at 3/255 — value noise is spatially CORRELATED, which is the one
    // thing a dither must never be: it smears banding into blotches instead of breaking it.
    return half4(ditherPremul(col * alpha, alpha, fragCoord), alpha);
}
"""

/**
 * Pure uniform math per [AuroraState] — same discipline as the orb's uniform-math objects: one
 * exhaustive `when(state)` per uniform, so a third [AuroraState] variant fails compilation instead
 * of silently missing a case here.
 */
internal object AuroraWashUniformMath {
    // AuraMotion names the edge glow's hue-rotation rate (M1/M2) explicitly; the wash's own M4
    // "drifting hue slowly" has no separate named Hz, so Streaming reuses that same atmospheric
    // rate; Thinking and Resting each derive a deliberately subtler fraction of it (calmer reads
    // for "not actively generating" — Resting is the calmest of the three, an ambient idle state).
    private const val THINKING_DRIFT_FRACTION = 0.7f
    private const val RESTING_DRIFT_FRACTION = 0.4f

    fun targetIntensity(state: AuroraState): Float = when (state) {
        AuroraState.Hidden -> 0f
        AuroraState.Resting -> 1f
        AuroraState.Thinking -> 1f
        AuroraState.Streaming -> 1f
    }

    fun driftHz(state: AuroraState): Float = when (state) {
        AuroraState.Hidden -> 0f
        AuroraState.Resting -> AuraMotion.edgeHueRotationHz * RESTING_DRIFT_FRACTION
        AuroraState.Thinking -> AuraMotion.edgeHueRotationHz * THINKING_DRIFT_FRACTION
        AuroraState.Streaming -> AuraMotion.edgeHueRotationHz
    }

    /** Ramping in (entering Thinking/Streaming, M3) and fading out (settling to Hidden, M4) use
     * different named durations - direction is fully determined by the target alone: intensity
     * only ever targets 0 (fade-out) or 1 (ramp-in), never a partial value. */
    fun transitionSpec(targetIntensity: Float): AnimationSpec<Float> =
        tween(if (targetIntensity > 0f) AuraMotion.thinkingRampMs else AuraMotion.settleFadeMs)
}

/**
 * Full-bleed top wash (§3.3, S1, M3/M4): gold-to-green gradient (measured) fading to
 * canvas by [AuraColors.auroraWashTopFadeHeightFraction], with slow hue drift while active — active
 * for [AuroraState.Resting] (landing/idle: real captures show it visibly present at rest, not
 * only during generation) as well as [AuroraState.Thinking]/[AuroraState.Streaming]. Renders BEHIND
 * content and never tints text/icons/touch targets (§3.3 rule) — callers place it as the
 * bottom-most layer in a `Box`. Draws nothing once fully settled to [AuroraState.Hidden] (skips the
 * shader entirely, not just alpha-zeroed).
 */
@Composable
fun AuroraWashTop(state: AuroraState, modifier: Modifier = Modifier) {
    val extras = LocalAssistantExtras.current
    val target = AuroraWashUniformMath.targetIntensity(state)
    // kept as an un-destructured State<Float> (no `by`) - see
    // AuroraEdgeGlow.kt's identical fix for the full rationale (composable-scope `by` reads
    // subscribe this composable to recompose on every animation frame; onDrawBehind's `.value`
    // read below doesn't). The render-or-not decision routes through derivedStateOf instead, which
    // only recomposes when the boolean itself flips.
    val intensity: State<Float> = animateFloatAsState(
        targetValue = target,
        animationSpec = if (extras.reducedMotion) snap() else AuroraWashUniformMath.transitionSpec(target),
        label = "aurora-wash-intensity",
    )
    val shouldRender by remember(target) { derivedStateOf { intensity.value > 0f || target > 0f } }

    if (!shouldRender) return

    val shader = remember { RuntimeShader(AURORA_WASH_SHADER_SRC) }
    val timeSeconds = rememberShaderTimeSeconds()
    val driftHz = if (extras.reducedMotion) 0f else AuroraWashUniformMath.driftHz(state)
    val gold = AuraColors.auroraWashTop[0].color
    val green = AuraColors.auroraWashTop[1].color

    Box(
        modifier = modifier
            .fillMaxSize()
            .drawWithCache {
                val brush = ShaderBrush(shader)
                onDrawBehind {
                    shader.setFloatUniform("iResolution", size.width, size.height)
                    shader.setFloatUniform("iTime", timeSeconds.floatValue % 1000f)
                    shader.setFloatUniform("iDriftHz", driftHz)
                    shader.setFloatUniform("iIntensity", intensity.value)
                    shader.setFloatUniform("iFadeHeightFraction", AuraColors.auroraWashTopFadeHeightFraction)
                    shader.setFloatUniform("iColorGold", gold.red, gold.green, gold.blue)
                    shader.setFloatUniform("iColorGreen", green.red, green.green, green.blue)
                    drawRect(brush = brush)
                }
            },
    )
}
