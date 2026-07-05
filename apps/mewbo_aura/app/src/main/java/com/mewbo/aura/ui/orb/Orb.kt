package com.mewbo.aura.ui.orb

import android.graphics.RuntimeShader
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
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
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.sin

// AGSL (RuntimeShader, API 33+). Geometry (silhouette + hole + petalShape FFT fit) comes from
// [ClayFlowerSdf.GLSL_CORE] — shared with `AuraSpark` so the two renderings can never drift apart.
// This shader's own job is everything ON TOP of that silhouette: an interior iridescent sweep +
// shimmer masked to the shape, and an exterior halo/ring that an explicit edge window forces to
// exactly zero before the draw bounds.
private val ORB_SHADER_SRC = """
uniform float2 iResolution;
uniform float iTime;
uniform float iRotation;
uniform float iAmp;
uniform float iEnergy;
uniform float iRing;
uniform float iDesaturate;
uniform float iErrorBlend;
uniform float iErrorPulse;
uniform float iSweepSpeed;
uniform float3 iColorA;
uniform float3 iColorB;
uniform float3 iColorC;

${GlslNoise.GLSL_CORE}
${ClayFlowerSdf.GLSL_CORE}

half4 main(float2 fragCoord) {
    float2 res = iResolution;
    float m = min(res.x, res.y);
    float2 uv = (fragCoord - 0.5 * res) / m;
    float aa = 1.5 / m; // ~1.5px antialiasing window — vector-sharp, not a soft gaussian ramp

    float dist = length(uv);
    float theta = atan(uv.y, uv.x) - iRotation;
    float shapeAlpha = flowerShapeAlpha(uv, iRotation, iAmp, aa);

    // Interior iridescent sweep, co-rotating with the petals (theta already carries -iRotation),
    // plus a subtle shimmer — both confined inside the crisp silhouette via shapeAlpha below.
    float sweepT = iTime * iSweepSpeed;
    float sweep1 = 0.5 + 0.5 * sin(theta * 2.0 + sweepT * 0.6);
    float sweep2 = 0.5 + 0.5 * sin(theta * 3.0 - sweepT * 0.9 + 1.7);
    float3 col = mix(iColorA, iColorB, sweep1);
    col = mix(col, iColorC, sweep2 * 0.55);

    float shimmer = valueNoise(uv * 9.0 + sweepT) * 0.5
                  + valueNoise(uv * 17.0 - sweepT * 0.7) * 0.25;
    col += (shimmer - 0.375) * 0.10;

    float gray = dot(col, float3(0.3, 0.59, 0.11));
    col = mix(col, float3(gray, gray, gray), iDesaturate);

    float3 errorColor = float3(1.0, 0.30, 0.24);
    col = mix(col, errorColor, iErrorBlend * (0.55 + 0.45 * iErrorPulse));
    col *= 0.85 + iEnergy * 0.35 + iErrorBlend * iErrorPulse * 0.3;

    // Binding fix (v1 review): an explicit edge window forces alpha to EXACTLY 0 well before the
    // draw bounds (dist=0.5) instead of relying on an asymptotic falloff constant — v1's ring used
    // exp(-90*d*d) alone, which still read ~0.2-0.4 alpha at the sampling boundary and produced a
    // visible square edge. Both halo and ring below are windowed by this multiplicatively.
    float edgeWindow = 1.0 - smoothstep(0.44, 0.5, dist);

    float haloFalloff = exp(-10.0 * max(dist - ${ClayFlowerSdf.FLOWER_SCALE} * 0.85, 0.0));
    float halo = haloFalloff * edgeWindow * (0.20 + iEnergy * 0.35) * (1.0 - shapeAlpha);

    float ringR = ${ClayFlowerSdf.FLOWER_SCALE} * (1.05 + iRing * 0.20);
    float ringDelta = dist - ringR;
    float ring = exp(-260.0 * ringDelta * ringDelta) * iRing * edgeWindow;

    float3 haloColor = mix(iColorA, iColorC, 0.5);
    float3 outColor = mix(haloColor, col, shapeAlpha);
    outColor = mix(outColor, iColorC, clamp(ring, 0.0, 1.0) * 0.85);

    float alpha = clamp(shapeAlpha + halo + ring, 0.0, 1.0) * edgeWindow;
    return half4(outColor * alpha, alpha);
}
"""

/** TalkBack label per [OrbState] (ui/CLAUDE.md accessibility: "TalkBack labels for orb states"). */
private fun OrbState.accessibilityLabel(): String = when (this) {
    is OrbState.Idle -> "Assistant idle"
    is OrbState.Listening -> "Assistant listening"
    is OrbState.Thinking -> "Assistant thinking"
    is OrbState.Error -> "Assistant error"
}

/**
 * Pure uniform math per [OrbState] — kept beside its only caller, not a shared util file. Every
 * per-state branch funnels through ONE exhaustive `when(state)` per uniform here; a hypothetical
 * 5th [OrbState] variant fails compilation instead of silently losing behavior anywhere else.
 */
private object OrbUniformMath {
    private const val IDLE_ROT_REVS_PER_SEC = 1f / 24f // matches the console BrandMark's 24s spin
    private const val LISTENING_ROT_REVS_PER_SEC = 1f / 24f
    private const val THINKING_ROT_REVS_PER_SEC = 1f / 6f
    private const val ERROR_ROT_REVS_PER_SEC = 0f

    private const val IDLE_BREATHE_HZ = 0.5f // §8.1: idle breathes at 0.5Hz
    private const val IDLE_BREATHE_SWING = 0.35f
    private const val LISTENING_AMP_SWING = 1.2f
    private const val ERROR_AMP_POP = 0.4f

    private const val RMS_NORMALIZATION_DB = 10f // SpeechRecognizer#onRmsChanged practical range
    private const val ERROR_PULSE_DECAY = 5.5f
    private const val ERROR_DIM_HOLD = 0.32f

    /** Discriminates the state *kind* (ignoring [OrbState.Listening]'s rms payload) so callers can
     * detect "we just transitioned" without retriggering on every rms tick. */
    fun transitionKey(state: OrbState): Int = when (state) {
        is OrbState.Idle -> 0
        is OrbState.Listening -> 1
        is OrbState.Thinking -> 2
        is OrbState.Error -> 3
    }

    fun rotationRevsPerSec(state: OrbState): Float = when (state) {
        is OrbState.Idle -> IDLE_ROT_REVS_PER_SEC
        is OrbState.Listening -> LISTENING_ROT_REVS_PER_SEC
        is OrbState.Thinking -> THINKING_ROT_REVS_PER_SEC
        is OrbState.Error -> ERROR_ROT_REVS_PER_SEC
    }

    private fun rmsNormalized(state: OrbState.Listening): Float =
        (state.rmsDb / RMS_NORMALIZATION_DB).coerceIn(0f, 1f)

    /** Sharp spike on error entry, decaying to a dim hold — a pure function of elapsed time since
     * the orb last transitioned into [OrbState.Error] (tracked by the caller via [transitionKey]). */
    private fun errorPulseEnvelope(elapsedSeconds: Float): Float =
        max(exp(-ERROR_PULSE_DECAY * elapsedSeconds), ERROR_DIM_HOLD)

    /** [errorPulseEnvelope] gated by state — the ONE branch point for "is this the error pulse", so
     * the call site never spells out `if (state is OrbState.Error)` itself. */
    fun pulseEnvelope(state: OrbState, elapsedSeconds: Float): Float = when (state) {
        is OrbState.Idle -> 0f
        is OrbState.Listening -> 0f
        is OrbState.Thinking -> 0f
        is OrbState.Error -> errorPulseEnvelope(elapsedSeconds)
    }

    fun lobeAmplitude(state: OrbState, timeSeconds: Float, errorPulse: Float): Float = when (state) {
        is OrbState.Idle -> 1f + IDLE_BREATHE_SWING * sin(2f * PI.toFloat() * IDLE_BREATHE_HZ * timeSeconds)
        is OrbState.Listening -> 1f + rmsNormalized(state) * LISTENING_AMP_SWING
        is OrbState.Thinking -> 1f
        is OrbState.Error -> 1f + errorPulse * ERROR_AMP_POP
    }

    fun energy(state: OrbState, errorPulse: Float): Float = when (state) {
        is OrbState.Idle -> 0.15f
        is OrbState.Listening -> rmsNormalized(state)
        is OrbState.Thinking -> 0.08f
        is OrbState.Error -> errorPulse * 0.5f
    }

    fun ring(state: OrbState): Float = when (state) {
        is OrbState.Idle -> 0f
        is OrbState.Listening -> rmsNormalized(state)
        is OrbState.Thinking -> 0f
        is OrbState.Error -> 0f
    }

    fun desaturate(state: OrbState): Float = when (state) {
        is OrbState.Idle -> 0f
        is OrbState.Listening -> 0f
        is OrbState.Thinking -> 0.82f
        is OrbState.Error -> 0f
    }

    fun errorBlend(state: OrbState): Float = when (state) {
        is OrbState.Idle -> 0f
        is OrbState.Listening -> 0f
        is OrbState.Thinking -> 0f
        is OrbState.Error -> 1f
    }

    fun errorScale(state: OrbState): Float = when (state) {
        is OrbState.Idle -> 1f
        is OrbState.Listening -> 1f
        is OrbState.Thinking -> 1f
        is OrbState.Error -> 1.08f
    }

    fun sweepSpeed(state: OrbState): Float = when (state) {
        is OrbState.Idle -> 1f
        is OrbState.Listening -> 1.3f
        is OrbState.Thinking -> 2.4f
        is OrbState.Error -> 0.4f
    }
}

private fun RuntimeShader.setOrbUniforms(
    resolution: Size,
    time: Float,
    rotation: Float,
    amp: Float,
    energy: Float,
    ring: Float,
    desaturate: Float,
    errorBlend: Float,
    errorPulse: Float,
    sweepSpeed: Float,
    colorA: Color,
    colorB: Color,
    colorC: Color,
) {
    setFloatUniform("iResolution", resolution.width, resolution.height)
    setFloatUniform("iTime", time % 1000f) // bounded — GPU trig precision degrades on huge angles
    setFloatUniform("iRotation", rotation)
    setFloatUniform("iAmp", amp)
    setFloatUniform("iEnergy", energy)
    setFloatUniform("iRing", ring)
    setFloatUniform("iDesaturate", desaturate)
    setFloatUniform("iErrorBlend", errorBlend)
    setFloatUniform("iErrorPulse", errorPulse)
    setFloatUniform("iSweepSpeed", sweepSpeed)
    setFloatUniform("iColorA", colorA.red, colorA.green, colorA.blue)
    setFloatUniform("iColorB", colorB.red, colorB.green, colorB.blue)
    setFloatUniform("iColorC", colorC.red, colorC.green, colorC.blue)
}

/**
 * The assist orb (§8.1): a shader-crisp AGSL rendering of the Mewbo clay-flower brand mark when
 * motion is enabled, the same static shader plus an opacity breathe under reduced motion. Palette
 * comes from [LocalAssistantExtras]; time is driven by [rememberShaderTimeSeconds], which pauses
 * while the host lifecycle is not STARTED. State transitions crossfade (rotation speed,
 * desaturation, error blend, palette, scale) via Compose animation State read ONLY in the draw
 * phase — so the continuous per-frame motion never triggers recomposition.
 */
@Composable
fun Orb(
    state: OrbState,
    modifier: Modifier = Modifier,
    size: Dp = 96.dp,
) {
    val extras = LocalAssistantExtras.current
    val palette = extras.orbPalette(state)
    check(palette.size >= 3) { "orb palette must supply at least 3 colors, got ${palette.size}" }

    val describedModifier = modifier.semantics { contentDescription = state.accessibilityLabel() }

    if (extras.reducedMotion) {
        ReducedMotionOrb(state = state, palette = palette, modifier = describedModifier, size = size)
        return
    }

    val shader = remember { RuntimeShader(ORB_SHADER_SRC) }
    val timeSeconds = rememberShaderTimeSeconds()

    // Time we last entered the current state KIND (not every Listening rms tick) — feeds the pure
    // error-pulse envelope below. transitionKey changing is exactly "we just transitioned."
    val transitionKey = OrbUniformMath.transitionKey(state)
    val stateEnteredAt = remember { mutableFloatStateOf(0f) }
    LaunchedEffect(transitionKey) { stateEnteredAt.floatValue = timeSeconds.floatValue }

    // Crossfaded "current visual params": a state change eases these instead of snapping the
    // shader's uniforms (no hard jumps), while the continuous per-frame math (breathing, shimmer,
    // rotation integration) stays entirely in the draw phase below. Every State here is read ONLY
    // inside onDrawBehind/graphicsLayer, so the animation never recomposes Orb() itself.
    val rotationSpeedAnim: State<Float> = animateFloatAsState(
        targetValue = OrbUniformMath.rotationRevsPerSec(state),
        animationSpec = AuraMotion.orbGlideSpring,
        label = "orb-rotation-speed",
    )
    val desaturateAnim: State<Float> = animateFloatAsState(
        targetValue = OrbUniformMath.desaturate(state),
        animationSpec = AuraMotion.orbTransitionSpring,
        label = "orb-desaturate",
    )
    val errorBlendAnim: State<Float> = animateFloatAsState(
        targetValue = OrbUniformMath.errorBlend(state),
        animationSpec = AuraMotion.orbTransitionSpring,
        label = "orb-error-blend",
    )
    val errorScaleAnim: State<Float> = animateFloatAsState(
        targetValue = OrbUniformMath.errorScale(state),
        animationSpec = AuraMotion.orbTransitionSpring,
        label = "orb-scale",
    )
    val colorAAnim: State<Color> = animateColorAsState(palette[0], label = "orb-color-a")
    val colorBAnim: State<Color> = animateColorAsState(palette[1], label = "orb-color-b")
    val colorCAnim: State<Color> = animateColorAsState(palette[2], label = "orb-color-c")

    val rotationState = remember { ShaderRotationAccumulator() }

    Box(
        modifier = describedModifier
            .size(size)
            // Binding fix (v1 review): graphicsLayer{} instead of Modifier.scale() so the
            // transition-driven pulse scale never recomposes — only reads State in the draw phase.
            .graphicsLayer {
                val s = errorScaleAnim.value
                scaleX = s
                scaleY = s
            }
            .drawWithCache {
                val brush = ShaderBrush(shader)
                onDrawBehind {
                    val t = timeSeconds.floatValue
                    val rotation = rotationState.advance(t, rotationSpeedAnim.value)

                    val elapsedInState = (t - stateEnteredAt.floatValue).coerceAtLeast(0f)
                    val errorPulse = OrbUniformMath.pulseEnvelope(state, elapsedInState)

                    shader.setOrbUniforms(
                        resolution = this.size,
                        time = t,
                        rotation = rotation,
                        amp = OrbUniformMath.lobeAmplitude(state, t, errorPulse),
                        energy = OrbUniformMath.energy(state, errorPulse),
                        ring = OrbUniformMath.ring(state),
                        desaturate = desaturateAnim.value,
                        errorBlend = errorBlendAnim.value,
                        errorPulse = errorPulse,
                        sweepSpeed = OrbUniformMath.sweepSpeed(state),
                        colorA = colorAAnim.value,
                        colorB = colorBAnim.value,
                        colorC = colorCAnim.value,
                    )
                    drawRect(brush = brush)
                }
            },
    )
}

/**
 * Reduced-motion fallback (§8.1): the SAME crisp shader silhouette, frozen (no rotation, no
 * shimmer drift), with only a slow opacity breathe. v1's fallback swapped to a soft radial
 * gradient circle — a visibly different, lower-fidelity shape; the brief requires a static but
 * still-crisp flower here, not a fallback shape.
 */
@Composable
private fun ReducedMotionOrb(state: OrbState, palette: List<Color>, modifier: Modifier, size: Dp) {
    val shader = remember { RuntimeShader(ORB_SHADER_SRC) }
    // Gitea #181 fix wave, finding 4: the lifecycle-gated clock every other shader consumer in
    // this family uses, not a second ungated rememberInfiniteTransition() - see
    // reducedMotionBreatheAlpha's own KDoc.
    val timeSeconds = rememberShaderTimeSeconds()

    Box(
        modifier = modifier
            .size(size)
            .graphicsLayer { alpha = reducedMotionBreatheAlpha(timeSeconds.floatValue, min = 0.7f) }
            .drawWithCache {
                val brush = ShaderBrush(shader)
                onDrawBehind {
                    shader.setOrbUniforms(
                        resolution = this.size,
                        time = 0f,
                        rotation = 0f,
                        amp = 1f,
                        energy = OrbUniformMath.energy(state, errorPulse = 0f),
                        ring = 0f,
                        desaturate = OrbUniformMath.desaturate(state),
                        errorBlend = OrbUniformMath.errorBlend(state),
                        errorPulse = 0f,
                        sweepSpeed = 0f,
                        colorA = palette[0],
                        colorB = palette[1],
                        colorC = palette[2],
                    )
                    drawRect(brush = brush)
                }
            },
    )
}
