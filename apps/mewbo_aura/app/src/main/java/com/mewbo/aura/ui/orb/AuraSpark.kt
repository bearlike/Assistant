package com.mewbo.aura.ui.orb

import android.graphics.RuntimeShader
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ShaderBrush
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlin.math.PI
import kotlin.math.sin

// AGSL (RuntimeShader, API 33+). Reuses [ClayFlowerSdf.GLSL_CORE] for the exact same silhouette as
// [Orb] — the brand mark must never visibly differ between the two renderings (D-6: "Spark/orb
// everywhere use the brand clay-flower geometry — no four-point stars, no generic blobs"). On top
// of that silhouette this shader paints the D-2 conic brand gradient (a closed 4-stop sweep, swept
// independently of the shape's own slow spin) plus a soft shimmer noise — no halo/ring/error terms;
// those are orb-only (§8.1's exterior-glow language never applies to the spark).
private val SPARK_SHADER_SRC = """
uniform float2 iResolution;
uniform float iRotation;
uniform float iAmp;
uniform float iSweepPhase;
uniform float iShimmer;
uniform float3 iStop0;
uniform float3 iStop1;
uniform float3 iStop2;
uniform float3 iStop3;

${GlslNoise.GLSL_CORE}
${ClayFlowerSdf.GLSL_CORE}

half4 main(float2 fragCoord) {
    float2 res = iResolution;
    float m = min(res.x, res.y);
    float2 uv = (fragCoord - 0.5 * res) / m;
    float aa = 1.5 / m;

    float shapeAlpha = flowerShapeAlpha(uv, iRotation, iAmp, aa);

    // Conic D-2 gradient: a closed 4-stop sweep around raw screen-space angle (NOT iRotation, so
    // the hue drifts across the lobes instead of spinning rigidly with the silhouette).
    float theta = atan(uv.y, uv.x);
    float t = fract(theta / 6.2831853 + 0.5 + iSweepPhase);
    float seg = t * 4.0;
    float3 col;
    if (seg < 1.0) col = mix(iStop0, iStop1, seg);
    else if (seg < 2.0) col = mix(iStop1, iStop2, seg - 1.0);
    else if (seg < 3.0) col = mix(iStop2, iStop3, seg - 2.0);
    else col = mix(iStop3, iStop0, seg - 3.0);

    float shimmer = valueNoise(uv * 10.0 + iSweepPhase * 6.0) * 0.5
                  + valueNoise(uv * 19.0 - iSweepPhase * 4.0) * 0.25;
    col += (shimmer - 0.375) * iShimmer * 0.16;

    float alpha = clamp(shapeAlpha, 0.0, 1.0);
    return half4(col * alpha, alpha);
}
"""

/** TalkBack label per [SparkState]. "Aura" is the internal codename only (Rev D-4) - matches
 * [Orb]'s "Assistant …" house style rather than introducing a second user-facing convention. */
private fun SparkState.accessibilityLabel(): String = when (this) {
    SparkState.Shimmer -> "Assistant"
    SparkState.Thinking -> "Assistant thinking"
}

/**
 * Pure uniform math per [SparkState] — same discipline as [Orb]'s `OrbUniformMath`: every
 * per-state branch is one exhaustive `when(state)`, so a third [SparkState] fails compilation
 * instead of silently losing behavior in only one of these functions.
 */
internal object SparkUniformMath {
    private const val SHIMMER_SPIN_REVS_PER_SEC = 1f / 24f // matches the orb's idle/console spin
    private const val THINKING_SPIN_REVS_PER_SEC = 1f / 6f // matches the orb's thinking spin

    private const val SHIMMER_SWEEP_HZ = 1f / 4f // D-2/§6.6: "subtle 4s loop"
    private const val THINKING_SWEEP_HZ = 1f / 1.2f // busier sweep reads as "working"

    private const val THINKING_PULSE_HZ = 1f / 1.2f
    private const val THINKING_PULSE_SWING = 0.14f

    private const val SHIMMER_STRENGTH = 1f
    private const val THINKING_SHIMMER_STRENGTH = 0.55f

    fun rotationRevsPerSec(state: SparkState): Float = when (state) {
        SparkState.Shimmer -> SHIMMER_SPIN_REVS_PER_SEC
        SparkState.Thinking -> THINKING_SPIN_REVS_PER_SEC
    }

    fun sweepHz(state: SparkState): Float = when (state) {
        SparkState.Shimmer -> SHIMMER_SWEEP_HZ
        SparkState.Thinking -> THINKING_SWEEP_HZ
    }

    fun shimmerStrength(state: SparkState): Float = when (state) {
        SparkState.Shimmer -> SHIMMER_STRENGTH
        SparkState.Thinking -> THINKING_SHIMMER_STRENGTH
    }

    /** M3's "pulse": a gentle lobe-amplitude breathe so Thinking visibly differs from Shimmer's
     * constant amplitude, without touching the shared silhouette function's contract. */
    fun lobeAmplitude(state: SparkState, timeSeconds: Float): Float = when (state) {
        SparkState.Shimmer -> 1f
        SparkState.Thinking ->
            1f + THINKING_PULSE_SWING * sin(2f * PI.toFloat() * THINKING_PULSE_HZ * timeSeconds)
    }
}

private fun RuntimeShader.setSparkUniforms(
    resolution: Size,
    rotation: Float,
    amp: Float,
    sweepPhase: Float,
    shimmer: Float,
    stops: List<Color>,
) {
    setFloatUniform("iResolution", resolution.width, resolution.height)
    setFloatUniform("iRotation", rotation)
    setFloatUniform("iAmp", amp)
    setFloatUniform("iSweepPhase", sweepPhase)
    setFloatUniform("iShimmer", shimmer)
    setFloatUniform("iStop0", stops[0].red, stops[0].green, stops[0].blue)
    setFloatUniform("iStop1", stops[1].red, stops[1].green, stops[1].blue)
    setFloatUniform("iStop2", stops[2].red, stops[2].green, stops[2].blue)
    setFloatUniform("iStop3", stops[3].red, stops[3].green, stops[3].blue)
}

/**
 * The Aura spark (D-2): the same clay-flower silhouette as [Orb], painted with the brand conic
 * gradient instead of the orb's sweep/halo treatment. Greeting mark (48dp, [SparkState.Shimmer])
 * and in-chat thinking indicator ([SparkState.Thinking], replaces the Rev C `LoadingIndicator`
 * per M3) — never the orb itself, which is overlay-only per D-1.
 */
@Composable
fun AuraSpark(
    state: SparkState,
    modifier: Modifier = Modifier,
    size: Dp = AuraSpacing.Greeting.sparkSize,
) {
    val extras = LocalAssistantExtras.current
    val describedModifier = modifier.semantics { contentDescription = state.accessibilityLabel() }

    if (extras.reducedMotion) {
        ReducedMotionSpark(modifier = describedModifier, size = size)
        return
    }

    val shader = remember { RuntimeShader(SPARK_SHADER_SRC) }
    val timeSeconds = rememberShaderTimeSeconds()

    // M3's ramp-in duration doubles as this crossfade spec: its own KDoc names the spark pulse as
    // one of its two consumers ("the spark/orb pulses as the thinking indicator").
    val transitionSpec = tween<Float>(AuraMotion.thinkingRampMs)
    val rotationSpeedAnim: State<Float> = animateFloatAsState(
        targetValue = SparkUniformMath.rotationRevsPerSec(state),
        animationSpec = transitionSpec,
        label = "spark-rotation-speed",
    )
    val sweepHzAnim: State<Float> = animateFloatAsState(
        targetValue = SparkUniformMath.sweepHz(state),
        animationSpec = transitionSpec,
        label = "spark-sweep-hz",
    )
    val shimmerAnim: State<Float> = animateFloatAsState(
        targetValue = SparkUniformMath.shimmerStrength(state),
        animationSpec = transitionSpec,
        label = "spark-shimmer",
    )

    val rotationState = remember { ShaderRotationAccumulator() }
    val sweepState = remember { ShaderRotationAccumulator() }

    Box(
        modifier = describedModifier
            .size(size)
            .drawWithCache {
                val brush = ShaderBrush(shader)
                onDrawBehind {
                    val t = timeSeconds.floatValue
                    val rotation = rotationState.advance(t, rotationSpeedAnim.value)
                    // Reuse the same accumulator shape for the gradient sweep phase (0..2*PI
                    // radians), then normalize to the shader's expected 0..1 phase.
                    val sweepPhase = sweepState.advance(t, sweepHzAnim.value) / (2f * PI.toFloat())

                    shader.setSparkUniforms(
                        resolution = this.size,
                        rotation = rotation,
                        amp = SparkUniformMath.lobeAmplitude(state, t),
                        sweepPhase = sweepPhase,
                        shimmer = shimmerAnim.value,
                        stops = AuraColors.sparkGradient,
                    )
                    drawRect(brush = brush)
                }
            },
    )
}

/**
 * Reduced-motion fallback: the SAME crisp shader silhouette + gradient, frozen (no spin, no sweep,
 * no shimmer), with only a slow opacity breathe — mirrors [Orb]'s `ReducedMotionOrb` treatment so
 * neither brand mark ever falls back to a lower-fidelity shape under accessibility settings. Both
 * [SparkState] variants freeze to this identical static frame: unlike the orb, the spark shader has
 * no per-state energy/desaturation uniforms, so there is nothing left to distinguish once motion is
 * removed (the accessibility label alone still differs, applied by the caller's modifier).
 */
@Composable
private fun ReducedMotionSpark(modifier: Modifier, size: Dp) {
    val shader = remember { RuntimeShader(SPARK_SHADER_SRC) }
    // Gitea #181 fix wave, finding 4: the lifecycle-gated clock every other shader consumer in
    // this family uses, not a second ungated rememberInfiniteTransition() - see
    // reducedMotionBreatheAlpha's own KDoc.
    val timeSeconds = rememberShaderTimeSeconds()

    Box(
        modifier = modifier
            .size(size)
            .graphicsLayer { alpha = reducedMotionBreatheAlpha(timeSeconds.floatValue, min = 0.75f) }
            .drawWithCache {
                val brush = ShaderBrush(shader)
                onDrawBehind {
                    shader.setSparkUniforms(
                        resolution = this.size,
                        rotation = 0f,
                        amp = 1f,
                        sweepPhase = 0f,
                        shimmer = 0f,
                        stops = AuraColors.sparkGradient,
                    )
                    drawRect(brush = brush)
                }
            },
    )
}
