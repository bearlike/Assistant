package com.mewbo.aura.ui.aurora

import android.graphics.RuntimeShader
import android.os.Build
import androidx.annotation.RequiresApi
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
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.ShaderBrush
import androidx.compose.ui.graphics.graphicsLayer
import com.mewbo.aura.ui.orb.AuraShaders
import com.mewbo.aura.ui.orb.GlslNoise
import com.mewbo.aura.ui.orb.rememberShaderTimeSeconds
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.LocalAssistantExtras

// AGSL (RuntimeShader, API 33+). A full-bleed top wash: gold blended to green across the width,
// fading to nothing by [AuraColors.auroraWashTopFadeHeightFraction] of the screen height. The
// color drift slides the gold/green blend boundary left-right over time rather than a full HSV
// hue rotation - reads as the gradient breathing, at a fraction of the shader cost. The shared
// [GlslNoise.ditherPremul] breaks 8-bit banding on the smooth vertical falloff.
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

    // Dither applies to the PREMULTIPLIED colour ([GlslNoise.ditherPremul]) - dithering before the
    // `* alpha` would scale the perturbation by alpha and neuter it where the falloff is flattest.
    return half4(ditherPremul(col * alpha, alpha, fragCoord), alpha);
}
"""

/**
 * Pure uniform math per [AuroraState] — same discipline as the orb's uniform-math objects: one
 * exhaustive `when(state)` per uniform, so a third [AuroraState] variant fails compilation instead
 * of silently missing a case here.
 */
internal object AuroraWashUniformMath {
    // Streaming reuses the edge glow's own hue-rotation rate (AuraMotion.edgeHueRotationHz);
    // Thinking and Resting each derive a subtler fraction of it - calmer reads for "not actively
    // generating," with Resting the calmest as an ambient idle state.
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

    /** Ramping in (entering Thinking/Streaming) and fading out (settling to Hidden) use different
     * named durations - direction is fully determined by the target alone: intensity only ever
     * targets 0 (fade-out) or 1 (ramp-in), never a partial value. */
    fun transitionSpec(targetIntensity: Float): AnimationSpec<Float> =
        tween(if (targetIntensity > 0f) AuraMotion.thinkingRampMs else AuraMotion.settleFadeMs)
}

/**
 * Full-bleed top wash: gold-to-green gradient fading to canvas by
 * [AuraColors.auroraWashTopFadeHeightFraction], with slow hue drift while active - active for
 * [AuroraState.Resting] (idle) as well as [AuroraState.Thinking]/[AuroraState.Streaming]. Renders
 * BEHIND content and never tints text/icons/touch targets - callers place it as the bottom-most
 * layer in a `Box`. Draws nothing once fully settled to [AuroraState.Hidden] (skips the shader
 * entirely, not just alpha-zeroed).
 */
@Composable
fun AuroraWashTop(state: AuroraState, modifier: Modifier = Modifier) {
    val extras = LocalAssistantExtras.current
    val target = AuroraWashUniformMath.targetIntensity(state)
    // Kept as an un-destructured State<Float> (no `by`) - a composable-scope `by` read would
    // subscribe this composable to recompose on every animation frame; onDrawBehind's `.value`
    // read below doesn't. The render-or-not decision routes through derivedStateOf, which only
    // recomposes when the boolean itself flips.
    val intensity: State<Float> = animateFloatAsState(
        targetValue = target,
        animationSpec = if (extras.reducedMotion) snap() else AuroraWashUniformMath.transitionSpec(target),
        label = "aurora-wash-intensity",
    )
    val shouldRender by remember(target) { derivedStateOf { intensity.value > 0f || target > 0f } }

    if (!shouldRender) return

    if (!AuraShaders.supported) {
        ShaderFreeWashTop(modifier = modifier, intensity = intensity)
        return
    }

    ShaderWashTop(state = state, modifier = modifier, intensity = intensity)
}

/** The live AGSL wash — what [AuroraWashTop] resolves to once [AuraShaders] confirms `RuntimeShader`. */
@RequiresApi(Build.VERSION_CODES.TIRAMISU)
@Composable
private fun ShaderWashTop(state: AuroraState, modifier: Modifier, intensity: State<Float>) {
    val extras = LocalAssistantExtras.current
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

/**
 * Shader-free fallback for API 30-32 ([AuraShaders]): the same gold-to-green top wash, drawn as a
 * horizontal Compose gradient masked to nothing by
 * [AuraColors.auroraWashTopFadeHeightFraction] of the height.
 *
 * The vertical fade is a `DstIn` alpha mask over an offscreen layer rather than a second colour
 * ramp - a linear gradient cannot carry the horizontal blend and the vertical falloff at once, and
 * painting the canvas colour back over the top would only work against one background. Dropped
 * against the shader: the drift (this is a static frame) and the dither, so the falloff bands at
 * 8 bits. Acceptable for a debug-showcase-only surface on a television.
 */
@Composable
private fun ShaderFreeWashTop(modifier: Modifier, intensity: State<Float>) {
    val gold = AuraColors.auroraWashTop[0].color
    val green = AuraColors.auroraWashTop[1].color

    Box(
        modifier = modifier
            .fillMaxSize()
            .graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }
            .drawWithCache {
                val wash = Brush.horizontalGradient(colors = listOf(gold, green))
                val fadeMask = Brush.verticalGradient(
                    colors = listOf(Color.Black, Color.Transparent),
                    startY = 0f,
                    endY = size.height * AuraColors.auroraWashTopFadeHeightFraction,
                )
                onDrawBehind {
                    drawRect(brush = wash, alpha = intensity.value.coerceIn(0f, 1f))
                    drawRect(brush = fadeMask, blendMode = BlendMode.DstIn)
                }
            },
    )
}
