package com.mewbo.aura.ui.aurora

import android.graphics.RuntimeShader
import android.os.Build
import androidx.annotation.RequiresApi
import androidx.compose.animation.core.LinearEasing
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ShaderBrush
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.orb.AuraShaders
import com.mewbo.aura.ui.orb.GlslNoise
import com.mewbo.aura.ui.orb.rememberShaderTimeSeconds
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.GradientStop
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlin.math.PI
import kotlin.math.sin

// AGSL (RuntimeShader, API 33+). Bottom bloom: a bottom-anchored exponential falloff, no radial
// mask, no plateau - ignition grows [decayLength] from near-zero to its resting depth so every
// column lights at the same rate, and the continuous decay leaves no flat region for an opaque
// layer above it (the composer pill) to crop a line across. Dither (below,
// [GlslNoise.ditherPremul]) removes 8-bit banding. Hue is a two-stop blend by the bloom's own
// intensity, never by angle - no phase uniform to drift out of sync. Horizontal center-weighting
// and a slow x/time waviness (GlslNoise) keep it from reading as a flat static band.
//
// Listening reads WIDE (reach into the bottom corners); Thinking contracts to hug the pill with
// no corner reach. Both peak at the same bottom-edge position - only reach (decayLength) and
// corner suppression (centerWeight) differ, via [EdgeGlowUniformMath.decayDepthDp]/
// [EdgeGlowUniformMath.centerWeightStrength]. Decay length alone can't suppress corners (they sit
// on the same distFromBottom=0 row as the peak), so Thinking also raises centerWeight.
//
// The BORDER PROFILE (`perimeterBias`, [EdgeGlowUniformMath]'s BORDER_* ratios) shifts a caller's
// whole balance from the bottom-anchored bloom toward the edge-anchored perimeter, for a surface
// that must read as a border round the whole screen rather than as a bottom wash. It adds no
// mechanism: the terms marked "Border profile" below take a bias-driven mix, and the rest are
// rebalanced CPU-side in [EdgeGlowUniformMath]. Every one of them is an EXACT identity at bias 0 -
// `mix(x, y, 0)` is x and a `* 1.0` is exact - so the chat and assist-overlay byte-paths are
// unchanged by construction, not by tuning.
private val AURORA_EDGE_SHADER_SRC = """
uniform float2 iResolution;
uniform float iDecayLengthPx;
uniform float iIgniteProgress;
uniform float iIntensity;
uniform float iCenterWeight;
uniform float iTopFadeFraction;
uniform float iWaveAmplitude;
uniform float iWaveFreqX;
uniform float iWaveSpeedHz;
uniform float iTime;
uniform float iVisible;
uniform float iPerimeterBloom;
uniform float iPerimeterFloor;
uniform float iPerimeterBias;
uniform float iBottomWeight;
uniform float iRiseFraction;
uniform float iHueDrift;
uniform float3 iColorLight;
uniform float3 iColorDeep;
uniform float3 iAuroraLightB;
uniform float3 iAuroraDeepB;
uniform float3 iAuroraLightC;
uniform float3 iAuroraDeepC;

${GlslNoise.GLSL_CORE}

half4 main(float2 fragCoord) {
    float2 res = iResolution;

    // Low-frequency drift over (x, time) modulates the bloom's reach so it breathes rather than
    // reading as a frozen gradient. The noise x-argument is offset by iTime so the undulation
    // reads as one wave flowing across the screen, not shimmer in place; the second (coarse+fine)
    // octave breaks the silhouette so the boundary reads as liquid rather than one sine wave.
    // Both terms are iTime × rate - phase-continuous, never reset mid-state.
    float waveCoarse = valueNoise(float2(
        fragCoord.x * iWaveFreqX + iTime * iWaveSpeedHz,
        iTime * iWaveSpeedHz * 0.37)) - 0.5;
    float waveFine = valueNoise(float2(
        fragCoord.x * iWaveFreqX * 2.3 + 7.31 + iTime * iWaveSpeedHz * 1.9,
        iTime * iWaveSpeedHz * 0.71 + 3.17)) - 0.5;
    float wave = waveCoarse * 0.65 + waveFine * 0.35;
    float igniteFloor = 0.02; // keeps the decay length nonzero so exp() never divides by zero
    float decayLength = iDecayLengthPx * max(iIgniteProgress, igniteFloor) *
        (1.0 + wave * 2.0 * iWaveAmplitude);

    // Scaling decayLength by iIgniteProgress IS the whole ignition animation: at progress ~0 only
    // a thin sliver at the bottom edge is lit, widening to the resting reach - no radial mask, so
    // nothing crops against the corners at any progress value.
    float distFromBottom = max(res.y - fragCoord.y, 0.0);
    float verticalGlow = exp(-distFromBottom / max(decayLength, 0.5));

    // Horizontal center-weighting: brightest under/around the composer, fainter but never fully
    // zero toward the corners.
    float distFromCenterX = abs(fragCoord.x - res.x * 0.5);
    float horizontalWeight = 1.0 - iCenterWeight * smoothstep(0.0, res.x * 0.5, distFromCenterX);

    float vGlow = clamp(verticalGlow * horizontalWeight, 0.0, 1.0);

    // A large caller reachScale (the in-app chat's 5.5x) leaves the bottom-anchored falloff
    // clearly nonzero at the TOP draw bound, hard-stopping into a seam where the surface clips.
    // Feather to EXACTLY 0 over the top iTopFadeFraction of the surface height. Applies to the
    // BOTTOM-ANCHORED term ONLY - the perimeter terms below are edge-anchored and PEAK at the draw
    // bounds by construction, so there is no mid-falloff value for a clip edge to slice through.
    // No-op for the small-reach overlay.
    vGlow *= smoothstep(0.0, res.y * iTopFadeFraction, fragCoord.y);

    // Border profile (1/4): iBottomWeight holds the BOTTOM-ANCHORED term level with the rails
    // (1.0 = the balance every caller renders today). It is the only term that can, since `glow` is
    // `vGlow + perimeter` and a bottom edge brighter than the sides is the bottom wash again. At
    // the border's contracted reach it sits AT 1.0: what stops the lower third saturating there is
    // the reach contraction, not damping - see EdgeGlowUniformMath.BORDER_PARITY_LEVEL.
    vGlow *= iBottomWeight;

    // Invocation perimeter bloom: per-row/per-column exponential falloffs from the side and top
    // edges (edge-anchored, no radial mask - same construction as the bottom term, rotated),
    // weighted by a bottom bias so the light reads as EMANATING from the pill. sideDecay derives
    // from decayLength, so the perimeter's inner boundary undulates with zero extra noise terms.
    // iPerimeterFloor keeps the perimeter edge-lit after iPerimeterBloom settles to 0 -
    // max(iPerimeterBloom, iPerimeterFloor) means the bloom wins whenever it's live. The per-row
    // sideLevel wave below makes the edge reach visibly rise and fall along the screen edges.
    float sideLevel = valueNoise(float2(
        fragCoord.y * iWaveFreqX * 3.5 + iTime * iWaveSpeedHz * 1.4,
        iTime * iWaveSpeedHz * 0.53 + 11.7)) - 0.5;
    float sideDecay = max(decayLength * 0.6 * (1.0 + sideLevel * 2.0 * iWaveAmplitude), 0.5);
    float sideGlow = exp(-fragCoord.x / sideDecay) + exp(-(res.x - fragCoord.x) / sideDecay);
    float heightFrac = fragCoord.y / res.y;
    // Border profile (2/4): the bottom bias and the half-strength/half-thickness top are the
    // two terms that make the perimeter read as EMANATING from the composer pill. iPerimeterBias
    // flattens both toward an even four-edge border, for a surface that has no pill at its bottom
    // edge and wants a border rather than an emanation. At bias 0 each mix returns its inner value
    // exactly (mix(x, y, 0) == x), so this is a no-op for every caller that does not opt in.
    float bottomBias = mix(mix(0.45, 1.0, heightFrac * heightFrac), 1.0, iPerimeterBias);
    float topParity = mix(0.5, 1.0, iPerimeterBias);
    float topGlow = exp(-fragCoord.y / max(sideDecay * topParity, 0.5)) * topParity;
    float perimeter = clamp((sideGlow * bottomBias + topGlow) * max(iPerimeterBloom, iPerimeterFloor), 0.0, 1.0);

    float glow = clamp(vGlow + perimeter, 0.0, 1.0);

    // Rise bound. The BORDER profile's side rails run the full height of the surface by
    // construction (iPerimeterBias flattens bottomBias to 1.0), which reads as a frame on a tall
    // handheld and as a wash over most of a short, wide 16:9 panel. This bounds how far up the
    // WHOLE composite reaches - rails, top term and bottom term alike - so the surface stays a
    // bottom-anchored announcement instead of eating the screen.
    //
    // Branch-skipped at 0, the iHueDrift idiom: every existing caller is byte-identical by
    // construction rather than by tuning. Fades to EXACTLY 0 well below the bound, over the
    // upper 45% of the allowed rise, so nothing hard-stops into a seam - the same edge-window law
    // iTopFadeFraction follows, applied here to the edge-anchored terms the top fade deliberately
    // leaves alone (they no longer reach a draw bound once this is on, so there is a mid-falloff
    // for a clip edge to slice through and it has to be feathered).
    if (iRiseFraction > 0.0) {
        float rise = res.y * iRiseFraction;
        glow *= 1.0 - smoothstep(rise * 0.55, rise, distFromBottom);
    }

    // Hue drifts through three families (A blue -> B violet -> C ember) via a bounded, aperiodic
    // value-noise field, never an angle/phase term (a rotating hue can periodically let the wrong
    // color dominate). INTENSITY still selects light-vs-deep within the sampled family and drives
    // alpha. iHueDrift = 0 collapses to the pure A pair and skips the noise sample entirely - the
    // chat byte-path.
    float hueT = 0.0;
    if (iHueDrift > 0.0) {
        // Border profile (3/4): iPerimeterBias raises the field's SPATIAL frequency - never
        // its rate, never an angle (Rule 4b). At bias 0 roughly one noise cell spans the whole
        // surface, so a frame sits inside a single family; at 1 it is two to three, and a rail
        // traverses more of the A->B->C path along its OWN length, which is what makes the three
        // families visibly play across a perimeter instead of tinting it one colour at a time.
        float hueSpatial = mix(1.0, 2.2, iPerimeterBias);
        float hueNoise = valueNoise(float2(
            fragCoord.x * iWaveFreqX * 0.55 * hueSpatial + iTime * iWaveSpeedHz * 0.63,
            fragCoord.y * iWaveFreqX * 0.35 * hueSpatial + iTime * iWaveSpeedHz * 0.29 + 5.43));
        hueT = clamp(hueNoise, 0.0, 1.0) * iHueDrift;
    }
    // Written branch-free (never a ternary on a float3): a ternary-with-vector form risks a
    // runtime-compiler failure that renders the whole shader black. segAB runs A->B as hueT climbs
    // 0->0.5, segBC then runs B->C over 0.5->1 - bit-for-bit the same result as the two-branch form.
    float segAB = clamp(hueT * 2.0, 0.0, 1.0);
    float segBC = clamp((hueT - 0.5) * 2.0, 0.0, 1.0);
    float3 hueLight = mix(mix(iColorLight, iAuroraLightB, segAB), iAuroraLightC, segBC);
    float3 hueDeep = mix(mix(iColorDeep, iAuroraDeepB, segAB), iAuroraDeepC, segBC);
    float3 col = mix(hueDeep, hueLight, glow);

    // The bottom-edge sample (#6D85B9 over the #2A2A2E scrim) back-solves to a near-opaque peak.
    // Stripe-avoidance comes from the continuous exponential falloff, not from a low peak alpha -
    // capping alpha low here would render the effect invisible behind the overlay's bottom chrome.
    //
    // Border profile (4/4): 0.9 is a BLOOM's reservation - it sits under an app's own chrome and
    // must not read as a sheet of colour over it. A border IS the message, so at full bias it
    // spends the last tenth. Nothing downstream is left unguarded: the composited result is capped
    // by the host window's obscuring-alpha ceiling (ui/control), which is a WINDOW property and the
    // one number in this chain that is not available - per-pixel alpha does not enter it. Exact
    // identity at bias 0 under either lowering of mix (`x*(1-0) + y*0` and `x + 0*(y-x)` are both
    // x), the same argument bottomBias and topParity above already rest on.
    float peakAlpha = mix(0.9, 1.0, iPerimeterBias);
    float alpha = clamp(glow * iIntensity * iVisible, 0.0, 1.0) * peakAlpha;

    // Dither must apply to the PREMULTIPLIED colour (col * alpha) - the space actually quantized
    // to 8 bits. Dithering `col` before the `* alpha` scales the perturbation BY alpha, fading it
    // out precisely where the ramp is flattest and banding is worst. Alpha itself stays
    // un-dithered: the compositor's `premul + dst*(1-alpha)` keeps a 0.5/255 alpha error's visible
    // impact under 0.03 LSB against this family's near-black canvas.
    return half4(ditherPremul(col * alpha, alpha, fragCoord), alpha);
}
"""

/**
 * Pure uniform math per [EdgeGlowState] - same discipline as the orb's uniform-math objects: one
 * exhaustive `when(state)` per uniform. [reducedMotion] is threaded through as a parameter rather
 * than a second shader/composable (M8 here means "feed static values", not "swap shaders" - there
 * is no lower-fidelity fallback shape to fall back to, unlike the orb/spark silhouettes).
 */
internal object EdgeGlowUniformMath {
    private val BREATHE_HZ = 1000f / AuraMotion.listeningBreathePeriodMs // M2

    // M2: "loosely synced to RMS" - rms never overrides the 1.2s breathe cadence, it scales how
    // big that swing gets, up to this fraction on top of the named base amplitude at full loudness.
    private const val RMS_AMPLITUDE_BOOST_MAX = 0.5f
    private const val RMS_NORMALIZATION_DB = 10f // matches OrbUniformMath's SpeechRecognizer range

    private const val THINKING_INTENSITY = 1.3f // M3: "intensify" - no named numeric token, own tuning constant

    // Dismissal plays the ignite growth in reverse at AuraMotion.dismissSpeedMultiplier - derived
    // from those two tokens rather than a private literal, since no separate duration is named for
    // Hidden<->active transitions outside that growth itself.
    val VISIBILITY_TRANSITION_MS = (AuraMotion.edgeSweepMs / AuraMotion.dismissSpeedMultiplier).toInt()

    // Listening keeps corners at ~50% of vertical-only brightness; Thinking suppresses to ~15%,
    // since a contracted decay length alone can't suppress corners (they sit on the same
    // distFromBottom=0 row as the peak).
    private const val LISTENING_CENTER_WEIGHT_STRENGTH = 0.5f
    private const val THINKING_CENTER_WEIGHT_STRENGTH = 0.85f

    // Fraction of surface HEIGHT, from the top edge, over which the bloom feathers to exactly 0 -
    // kills the seam a large reachScale (the in-app 5.5x) would otherwise hard-stop into at the top
    // draw bound. A no-op for small-reach bottom-anchored callers.
    const val TOP_FADE_FRACTION = 0.18f

    /** Shader-free fallback ONLY (API 30-32, [AuraShaders]): the drawn band's height as a multiple
     * of the state's own [decayDepthDp]. A Compose linear gradient has no exponential tail that
     * fades out on its own, so the band has to be cut somewhere - at 3x the decay length the
     * shader's `exp(-d/L)` is down to ~5% of peak, which is where the ramp stops being visible
     * against this family's near-black canvas. A ratio OF a measured token, never a token itself. */
    const val FALLBACK_BAND_DECAY_MULTIPLE = 3f

    // Thinking's decay length as a fraction of AuraColors.auroraOverlayBloomDecayDepth (the
    // measured WIDE/Listening reach).
    private const val THINKING_DECAY_FRACTION = 0.22f

    // Overlay session-resting pool: fractions of the ONE measured reach token
    // (auroraOverlayBloomDecayDepth) and of the active baseline.
    private const val OVERLAY_RESTING_DECAY_FRACTION = 0.45f
    private const val OVERLAY_RESTING_INTENSITY = 0.55f

    // Persistent edge-lit floors so the perimeter keeps a faint side/top presence after the bloom
    // settles, instead of collapsing to zero. Ratios of the bloom's full perimeter strength;
    // Thinking stays lowest since its contracted, corner-suppressed profile is a designed state,
    // not an edge-lit one.
    private const val LISTENING_PERIMETER_FLOOR = 0.35f
    private const val RESTING_PERIMETER_FLOOR = 0.30f
    private const val THINKING_PERIMETER_FLOOR = 0.15f

    /** Per-state persistent perimeter presence (0 = bottom-only). Scaled by the
     * composable's caller knob (`perimeterPresence`: overlay 1f, chat default 0f → byte-path). */
    fun perimeterFloor(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> LISTENING_PERIMETER_FLOOR
        is EdgeGlowState.Listening -> LISTENING_PERIMETER_FLOOR
        EdgeGlowState.Thinking -> THINKING_PERIMETER_FLOOR
        EdgeGlowState.Resting -> RESTING_PERIMETER_FLOOR
    }

    // ---- Border profile: the caller's `perimeterBias` 0..1 ----
    // Rebalancing ratios, not new mechanisms. Each is a ratio OF a value already named above, and
    // each reaches its endpoint only at bias 1; at bias 0 every one of them is exactly 1.0, which
    // is what keeps every existing caller byte-identical rather than merely close.

    /** At full bias, the level BOTH the bottom edge and the side/top rails peak at, as a fraction
     * of the shader's `glow` term. Every other border constant derives from this one, which is
     * what makes the border EVEN the whole way round: a border whose bottom is brighter than its
     * sides is the bottom wash again with extra steps, and two independently-tuned numbers drift
     * into exactly that the first time one of them moves.
     *
     * **1.0 = the border renders at the glow term's full strength, and it is the ceiling** - the
     * surface still passes through `iIntensity`, the shader's `peakAlpha`, and finally the host
     * window's obscuring-alpha cap, which is the one factor in the chain that is not available.
     *
     * The previous 0.70 reserved headroom "against the bottom term where the two meet at the
     * corners", and that reservation was already spent: at 0.70, on a 1080x2400 @2.75 frame with
     * noise held at 0, the bottom-left join summed vGlow 0.697 + perimeter 0.695 = 1.391 and
     * clamped anyway. The corner join saturates at any parity above 0.5, so the reserve bought no
     * gradient where it claimed to and cost 30% of the surface's luminance everywhere else. What
     * it did buy is a SMALLER saturated fillet at the two bottom corners (~1.8k px, against ~11k
     * at parity 1.0 - 0.07% vs 0.43% of the frame); the hue-drift field still varies across that
     * fillet, only the light-vs-deep ramp within a family is flat there.
     *
     * Pinned to LISTENING's floor below, because Listening is the state a border-profile surface
     * holds for its whole lifetime. A biased Thinking frame sits under parity (its floor is
     * deliberately the lowest), which is correct: its contracted, corner-suppressed hug is a
     * designed shape, not an edge-lit one. */
    private const val BORDER_PARITY_LEVEL = 1f

    /** At full bias, the multiplier on the state's own [perimeterFloor] - it lifts the edge rails
     * from a faint presence to the surface's SUBJECT. Derived, never picked: it is exactly the
     * factor taking [LISTENING_PERIMETER_FLOOR] to [BORDER_PARITY_LEVEL] (0.35 -> 1.0). The float32
     * round-trip `0.35f * (1f / 0.35f)` is exactly 1.0, so the rails-equal-bottom identity holds
     * bit-exactly rather than to within a tolerance. */
    private const val BORDER_PERIMETER_GAIN = BORDER_PARITY_LEVEL / LISTENING_PERIMETER_FLOOR

    /** At full bias, the decay length as a fraction of the state's own reach ([decayDepthDp]) -
     * the same shape as [THINKING_DECAY_FRACTION], and the term that turns a haze into a border.
     * ONE number tightens both the bottom band and the rail thickness, because the shader's
     * `sideDecay` derives from `decayLength`. Sits between Thinking's 0.22 pill-hug and Resting's
     * 0.45 pool, and composes multiplicatively with the caller's own `reachScale`, which stays
     * free to scale on top of it. */
    private const val BORDER_REACH_FRACTION = 0.28f

    /** The caller's `perimeterBias`, clamped. The ONE place the 0..1 domain is enforced, so every
     * derived value below and the shader's own `iPerimeterBias` can never disagree about it. */
    fun borderAmount(perimeterBias: Float): Float = perimeterBias.coerceIn(0f, 1f)

    /** Shader `iBottomWeight`: 1 at bias 0 (the bottom-anchored balance), [BORDER_PARITY_LEVEL] at
     * bias 1 - which at the current parity is the identity at every bias, i.e. no damping at all.
     * What keeps the bottom band from swallowing the lower third at full bias is
     * [BORDER_REACH_FRACTION]'s contraction (39dp of decay against the 139dp the damping was first
     * written against - at 39dp the bottom term is under 0.001 by a third of the way up). The term
     * stays because parity is the knob: any parity below 1 damps the bottom through this, and this
     * is the only thing that can keep the bottom edge LEVEL with the rails when it does. */
    fun bottomWeight(perimeterBias: Float): Float =
        1f + (BORDER_PARITY_LEVEL - 1f) * borderAmount(perimeterBias)

    /** Multiplies the state's decay length (after the caller's `reachScale`): 1 at bias 0,
     * [BORDER_REACH_FRACTION] at bias 1. */
    fun reachFraction(perimeterBias: Float): Float =
        1f + (BORDER_REACH_FRACTION - 1f) * borderAmount(perimeterBias)

    /** Multiplies the resolved perimeter floor: 1 at bias 0, [BORDER_PERIMETER_GAIN] at bias 1.
     * Applied AFTER the caller's `perimeterPresence`, so a caller at presence 0 (chat) stays at
     * exactly 0 for any bias. */
    fun perimeterGain(perimeterBias: Float): Float =
        1f + (BORDER_PERIMETER_GAIN - 1f) * borderAmount(perimeterBias)

    /** Shader `iCenterWeight` for the frame: the caller's [override] or the state's own
     * [centerWeightStrength], flattened toward 0 as the bias climbs. A bloom is brightest under
     * the composer pill; a border's bottom edge is EVEN, and a center-weighted one would dip
     * between its bright centre and its bright corners. */
    fun centerWeight(state: EdgeGlowState, override: Float?, perimeterBias: Float): Float =
        (override ?: centerWeightStrength(state)) * (1f - borderAmount(perimeterBias))

    // Low-frequency spatial+temporal noise modulates the decay length by up to this fraction,
    // drifting slowly - a wobble, never a hard geometric change.
    const val WAVE_AMPLITUDE = 0.22f
    private const val WAVE_SPATIAL_CYCLES_ACROSS_WIDTH = 1.5f
    private const val WAVE_DRIFT_HZ = 0.035f

    /** Discriminates the state *kind* (ignoring [EdgeGlowState.Listening]'s rms payload) so the
     * caller can detect "we just entered Listening" without retriggering on every rms tick - same
     * pattern as [com.mewbo.aura.ui.orb.OrbState]'s error-pulse tracking. */
    fun transitionKey(state: EdgeGlowState): Int = when (state) {
        EdgeGlowState.Hidden -> 0
        is EdgeGlowState.Igniting -> 1
        is EdgeGlowState.Listening -> 2
        EdgeGlowState.Thinking -> 3
        EdgeGlowState.Resting -> 4
    }

    fun targetVisible(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> 1f
        is EdgeGlowState.Listening -> 1f
        EdgeGlowState.Thinking -> 1f
        EdgeGlowState.Resting -> 1f
    }

    /** The ignition growth front: 1f once ignition is complete or state has moved past it entirely -
     * only [EdgeGlowState.Igniting] carries a genuine sub-1 value, animated by the caller. Feeds the
     * shader's [AURORA_EDGE_SHADER_SRC] `decayLength` scale directly. */
    fun igniteProgress(state: EdgeGlowState, reducedMotion: Boolean): Float {
        if (reducedMotion) return 1f // static bloom frame, no growth animation
        return when (state) {
            EdgeGlowState.Hidden -> 1f
            is EdgeGlowState.Igniting -> state.progress.coerceIn(0f, 1f)
            is EdgeGlowState.Listening -> 1f
            EdgeGlowState.Thinking -> 1f
            EdgeGlowState.Resting -> 1f
        }
    }

    /** [EdgeGlowState.Igniting] targets Listening's WIDE reach (it is the entry animation
     * immediately before Listening begins), while [EdgeGlowState.Thinking] contracts to hug the
     * pill. [Hidden]'s value is unreachable in practice (alpha targets 0 there) but still must
     * resolve to something for the exhaustive `when`. [EdgeGlowState.Resting] (overlay-scoped)
     * sits between the two: a session-lifetime pool wider than Thinking's hug but narrower than
     * Listening's full reach. */
    fun decayDepthDp(state: EdgeGlowState): Dp = when (state) {
        EdgeGlowState.Hidden -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Igniting -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Listening -> AuraColors.auroraOverlayBloomDecayDepth
        EdgeGlowState.Thinking -> AuraColors.auroraOverlayBloomDecayDepth * THINKING_DECAY_FRACTION
        EdgeGlowState.Resting -> AuraColors.auroraOverlayBloomDecayDepth * OVERLAY_RESTING_DECAY_FRACTION
    }

    /** Horizontal center-weight strength per state. [Resting] reuses Listening's strength - it is
     * presence, not a corner-suppression concern like Thinking. */
    fun centerWeightStrength(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> LISTENING_CENTER_WEIGHT_STRENGTH
        is EdgeGlowState.Igniting -> LISTENING_CENTER_WEIGHT_STRENGTH
        is EdgeGlowState.Listening -> LISTENING_CENTER_WEIGHT_STRENGTH
        EdgeGlowState.Thinking -> THINKING_CENTER_WEIGHT_STRENGTH
        EdgeGlowState.Resting -> LISTENING_CENTER_WEIGHT_STRENGTH
    }

    private fun rmsNormalized(rmsDb: Float): Float = (rmsDb / RMS_NORMALIZATION_DB).coerceIn(0f, 1f)

    fun intensity(state: EdgeGlowState, timeSeconds: Float, reducedMotion: Boolean): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> 1f
        is EdgeGlowState.Listening -> if (reducedMotion) {
            1f
        } else {
            val amplitude = AuraMotion.listeningBreatheAmplitude *
                (1f + rmsNormalized(state.rmsDb) * RMS_AMPLITUDE_BOOST_MAX)
            1f + amplitude * sin(2f * PI.toFloat() * BREATHE_HZ * timeSeconds)
        }
        EdgeGlowState.Thinking -> THINKING_INTENSITY
        // Static - no time term, unlike Listening's breathe: a session-lifetime presence pool
        // must not oscillate (it would read as still "doing something").
        EdgeGlowState.Resting -> OVERLAY_RESTING_INTENSITY
    }

    /** Waviness spatial frequency in shader-space (cycles per pixel), scaled so
     * [WAVE_SPATIAL_CYCLES_ACROSS_WIDTH] full cycles span the draw width regardless of device size. */
    fun waveFreqX(widthPx: Float): Float =
        if (widthPx > 0f) WAVE_SPATIAL_CYCLES_ACROSS_WIDTH / widthPx else 0f

    fun waveSpeedHz(reducedMotion: Boolean): Float = if (reducedMotion) 0f else WAVE_DRIFT_HZ
}

/**
 * Draw-phase-only "did the state kind just change" tracker, read exclusively inside `onDrawBehind`.
 * A `LaunchedEffect(transitionKey)` alternative races [rememberShaderTimeSeconds]'s own internal
 * `LaunchedEffect` - both launch on the same composition pass with no guaranteed relative order, so
 * it can read `timeSeconds` before the frame clock's first tick and capture 0 instead of the true
 * current time. Comparing inside the draw phase is race-free: draw strictly follows that frame's
 * `timeSeconds` update, always.
 */
private class EdgeGlowTransitionTracker {
    private var lastTransitionKey = Int.MIN_VALUE
    private var enteredAt = 0f
    private var heldState: EdgeGlowState = EdgeGlowState.Hidden
    private var heldSpeed: Float = 1f

    /** The wave speed matching the most recent [resolveUniformState] call - read straight after it. */
    var resolvedSpeed: Float = 1f
        private set

    /**
     * The state whose uniforms (reach/intensity/center-weight/transition-key) should drive the shader
     * THIS frame. Normally the live [state]; but during an opt-in dismiss fade ([holdOnDismiss] &&
     * [state] == [EdgeGlowState.Hidden]) it resolves to the LAST ACTIVE state instead - Hidden's own
     * uniforms can't fade (intensity 0f zeroes alpha instantly; reach is wide and would pop the
     * bloom), so the frame is HELD and only the alpha envelope (`iVisible`) ramps to 0. The
     * matching wave speed is held too (via [resolvedSpeed]) so the drift neither jumps nor slows,
     * and phase never resets mid-fade either. Mutated + read EXCLUSIVELY inside `onDrawBehind`.
     */
    fun resolveUniformState(state: EdgeGlowState, speedScale: Float, holdOnDismiss: Boolean): EdgeGlowState {
        if (state != EdgeGlowState.Hidden) {
            heldState = state
            heldSpeed = speedScale
        }
        val hold = holdOnDismiss && state == EdgeGlowState.Hidden
        resolvedSpeed = if (hold) heldSpeed else speedScale
        return if (hold) heldState else state
    }

    fun elapsedSinceTransition(transitionKey: Int, timeSeconds: Float): Float {
        if (transitionKey != lastTransitionKey) {
            lastTransitionKey = transitionKey
            enteredAt = timeSeconds
        }
        return (timeSeconds - enteredAt).coerceAtLeast(0f)
    }
}

/**
 * Bottom-anchored edge glow. [AuraColors.auroraOverlayBloom]'s two stops (pale/deep blue) are
 * blended by the bloom's own intensity, never by angle or position - see
 * [AURORA_EDGE_SHADER_SRC]'s header. [EdgeGlowState.Igniting]'s progress is driven by the caller
 * (mirrors [OrbState]'s externally-driven `rmsDb` pattern) - this composable only turns it into
 * pixels.
 */
@Composable
fun AuroraEdgeGlow(
    state: EdgeGlowState,
    modifier: Modifier = Modifier,
    /** Multiplies the state's decay length. 1f = the overlay's capture-measured reach; the in-app
     * chat passes a larger scale since that reach reads tiny under the app's taller bottom stack. */
    reachScale: Float = 1f,
    /** Replaces the state's horizontal center-weighting when non-null. The state defaults produce a
     * center-anchored bloom (correct for the overlay, whose glow radiates from the pill); the
     * in-app chat passes a near-zero value so the bloom covers the whole bottom edge evenly. */
    centerWeightOverride: Float? = null,
    /** Scales the final alpha on top of the state's intensity envelope. The in-app chat runs
     * softer than the overlay's capture-measured peak. */
    alphaScale: Float = 1f,
    /** Multiplies the wave-drift speed ONLY (`iWaveSpeedHz`) - leaves reach/center-weight/alpha and
     * the breathe cadence untouched. 1f = the state's own pace; the in-app chat passes a >1 value
     * for the ACTIVE (Thinking) glow so a live run reads livelier. Reduced-motion is unaffected
     * (base speed is already 0 there).
     *
     * This scales the RATE, NEVER the accumulated phase: `iTime` is
     * [EdgeGlowTransitionTracker]'s `elapsedInState`, which resets to 0 at every state transition,
     * and a transition is the ONLY moment `speedScale` changes - so `iTime * iWaveSpeedHz` is 0
     * at the scale change regardless of scale, and the phase can never jump mid-flight. */
    speedScale: Float = 1f,
    /** Opt-in gentle fade-OFF duration (ms) for the run-end / dismiss transition to
     * [EdgeGlowState.Hidden]. `null` (the overlay default) keeps the quick
     * [EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS] dismiss. The in-app chat passes a long value
     * ([com.mewbo.aura.ui.theme.AuraMotion.edgeRestFadeMs], >=3s) so the glow eases off instead of
     * snapping to black on an abrupt on->off luminance change (a photosensitivity trigger). While
     * a fade is in flight the LAST active state's whole frame (reach, intensity, wave phase AND
     * speed) is HELD and only the alpha envelope ([iVisible]) ramps to 0 - snapping to [Hidden]'s
     * own uniforms can't fade (intensity 0f zeroes alpha instantly; reach is WIDE and would pop
     * the bloom). Holding the speed too is load-bearing: a wave-rate change while `iTime` is
     * already large would jump the drift. */
    dismissFadeMs: Int? = null,
    /** The two-stop color pair, blended by the glow's own intensity, never angle. Default = the
     * chat-darkened [AuraColors.auroraOverlayBloom]; the overlay passes
     * [AuraColors.auroraOverlayLiveBloom] (brighter, visible). */
    colors: List<GradientStop> = AuraColors.auroraOverlayBloom,
    /** Perimeter-bloom envelope (0..1), caller-animated - the composable only turns it into
     * pixels. Also lerps the color pair from [AuraColors.auroraIgnitionBloom] (bloom=1) down to
     * the caller's [colors] pair (bloom=0). Read in the draw phase only. Forced 0 under reduced
     * motion (no traveling light). */
    perimeterBloom: State<Float>? = null,
    /** 0..1 amount of the aurora hue-drift field (A blue -> B violet -> C ember). Default 0 =
     * the pure-[colors] two-stop path (the shader skips the hue-noise sample entirely) - chat's
     * byte-path; the overlay passes 1f. Reduced motion keeps the FIELD as a static multi-hue
     * frame: all drift speeds are already 0 there. */
    hueDriftAmount: Float = 0f,
    /** Scales [EdgeGlowUniformMath.perimeterFloor] (persistent edge-lit side/top presence in the
     * live states). Default 0 = bottom-only (chat byte-path); overlay passes 1f. */
    perimeterPresence: Float = 0f,
    /** 0..1 — how far this caller's glow is a BORDER round the whole surface rather than a
     * bottom-anchored bloom. Default 0 = the bottom-anchored balance every existing caller
     * renders; the device-control overlay passes 1f, because a surface whose entire job is to say
     * "an agent is driving your phone" has to be legible at every edge, has no composer pill for
     * the light to emanate from, and is watched for minutes rather than glanced at.
     *
     * One knob rather than seven, because all seven effects answer one question and tuning any of
     * them alone re-opens the imbalance:
     *  - the perimeter floor is gained up ([EdgeGlowUniformMath.perimeterGain]) until the rails
     *    reach the glow term's full strength — they are the SUBJECT, not what is left over;
     *  - the bottom-anchored term is held level with them
     *    ([EdgeGlowUniformMath.bottomWeight]) — `glow` is a SUM, and a bottom brighter than the
     *    sides is the bottom wash again;
     *  - the reach contracts ([EdgeGlowUniformMath.reachFraction]) — a border at a haze's decay
     *    length is a haze, and this tightens the rail thickness with the same number;
     *  - the horizontal center-weighting flattens ([EdgeGlowUniformMath.centerWeight]);
     *  - the perimeter's bottom-bias and its half-strength top flatten to four-edge parity;
     *  - `peakAlpha` spends its last tenth (0.9 → 1.0) — that reservation belongs to a bloom under
     *    an app's own chrome, not to a surface whose whole job is to be seen;
     *  - the hue field's SPATIAL frequency rises, so the three families play ALONG each rail
     *    rather than tinting the whole frame one family at a time.
     *
     * It does NOT touch the drift rate: that is `speedScale`, whose phase-continuity contract is
     * stated there and must stay the surface's single rate source.
     *
     * At 0 every one of the seven is an exact identity (`mix(x, y, 0)` is x; a `* 1.0` is exact),
     * so chat's and the assist overlay's byte-paths are preserved by construction. */
    perimeterBias: Float = 0f,
    /** How far up the surface the glow may reach, as a fraction of its height. `0f` (the default,
     * and every existing caller) means NO bound — the shader skips the window entirely, so those
     * callers are byte-identical by construction rather than by tuning.
     *
     * Exists because [perimeterBias]'s border profile runs its side rails the FULL height of the
     * surface: correct on a tall handheld, where that reads as a frame, and wrong on a short, wide
     * 16:9 panel, where the same construction covers most of the screen. Bounding the rise is the
     * cure rather than retuning the border, because the border is what makes the surface legible at
     * every edge in the first place.
     *
     * A fraction rather than a dp: the complaint this answers is about the PROPORTION of the screen
     * the surface occupies, and a dp says something different on every panel. */
    riseFraction: Float = 0f,
) {
    val extras = LocalAssistantExtras.current

    val target = EdgeGlowUniformMath.targetVisible(state)
    // Only when easing to Hidden (target 0). Only the alpha ENVELOPE ([visible]) is animated here;
    // the held-frame resolution that lets Hidden fade in place lives in
    // EdgeGlowTransitionTracker.resolveUniformState, mutated in the DRAW phase.
    val fadeOffMs = if (target == 0f) dismissFadeMs else null
    // Kept as an un-destructured State<Float> (no `by`) so reading it doesn't subscribe THIS
    // composable to recompose on every animation frame - only `onDrawBehind` below reads `.value`.
    // The one place this composable needs to react in COMPOSITION (deciding whether to render at
    // all) goes through `derivedStateOf` instead, which only recomposes when the boolean flips.
    val visible: State<Float> = animateFloatAsState(
        targetValue = target,
        animationSpec = when {
            extras.reducedMotion -> snap()
            // >=3s LINEAR ease-off (photosensitivity: a constant rate with no fast segment). The
            // quick dismiss below stays for the fade-IN and callers that pass no override.
            fadeOffMs != null -> tween(fadeOffMs, easing = LinearEasing)
            else -> tween(EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS)
        },
        label = "aurora-edge-visible",
    )
    val shouldRender by remember(target) { derivedStateOf { visible.value > 0f || target > 0f } }

    if (!shouldRender) return

    if (!AuraShaders.supported) {
        ShaderFreeEdgeGlow(
            state = state,
            modifier = modifier,
            reachScale = reachScale,
            alphaScale = alphaScale,
            colors = colors,
            visible = visible,
            riseFraction = riseFraction,
        )
        return
    }

    ShaderEdgeGlow(
        state = state,
        modifier = modifier,
        reachScale = reachScale,
        centerWeightOverride = centerWeightOverride,
        alphaScale = alphaScale,
        speedScale = speedScale,
        dismissFadeMs = dismissFadeMs,
        colors = colors,
        perimeterBloom = perimeterBloom,
        hueDriftAmount = hueDriftAmount,
        perimeterPresence = perimeterPresence,
        perimeterBias = perimeterBias,
        riseFraction = riseFraction,
        visible = visible,
    )
}

/**
 * The live AGSL bottom bloom — everything [AuroraEdgeGlow] resolves to once [AuraShaders] confirms
 * `RuntimeShader` exists. Every parameter is [AuroraEdgeGlow]'s own, documented there; [visible] is
 * the alpha envelope it already animated, passed down rather than re-derived so the fade survives
 * the split.
 */
@RequiresApi(Build.VERSION_CODES.TIRAMISU)
@Composable
private fun ShaderEdgeGlow(
    state: EdgeGlowState,
    modifier: Modifier,
    reachScale: Float,
    centerWeightOverride: Float?,
    alphaScale: Float,
    speedScale: Float,
    dismissFadeMs: Int?,
    colors: List<GradientStop>,
    perimeterBloom: State<Float>?,
    hueDriftAmount: Float,
    perimeterPresence: Float,
    perimeterBias: Float,
    riseFraction: Float,
    visible: State<Float>,
) {
    val extras = LocalAssistantExtras.current
    val shader = remember { RuntimeShader(AURORA_EDGE_SHADER_SRC) }
    val timeSeconds = rememberShaderTimeSeconds()
    val transitionTracker = remember { EdgeGlowTransitionTracker() }

    Box(
        modifier = modifier
            .fillMaxSize()
            .drawWithCache {
                val brush = ShaderBrush(shader)
                val waveFreqX = EdgeGlowUniformMath.waveFreqX(size.width)
                onDrawBehind {
                    val t = timeSeconds.floatValue
                    // During an opt-in dismiss fade this is the LAST ACTIVE state, else the live
                    // one - keying the uniforms off it means a fade-off holds the active frame and
                    // never resets phase or pops the reach mid-fade.
                    val uniformState = transitionTracker.resolveUniformState(
                        state = state,
                        speedScale = speedScale,
                        holdOnDismiss = dismissFadeMs != null,
                    )
                    val elapsedInState = transitionTracker.elapsedSinceTransition(
                        transitionKey = EdgeGlowUniformMath.transitionKey(uniformState),
                        timeSeconds = t,
                    )
                    // Forced 0 under reduced motion (no traveling light). The ignition color pair
                    // is lerped CPU-side (bloom=1 → the brand ignition pair, bloom=0 → the caller's
                    // own [colors] pair) so the shader stays a two-stop intensity blend, never a 3rd
                    // stop. Read in the draw phase so reading perimeterBloom's per-frame value
                    // never subscribes THIS composable to recompose.
                    val bloom = if (extras.reducedMotion) 0f else (perimeterBloom?.value ?: 0f)
                    val colorLight = lerp(colors[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val colorDeep = lerp(colors[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    // The B/C hue families lerp toward the SAME ignition pair as the A pair above,
                    // so a bloom unifies the whole multi-hue field into brand ignition light and
                    // drifts back apart as it settles.
                    val violetLight = lerp(AuraColors.auroraOverlayVioletBloom[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val violetDeep = lerp(AuraColors.auroraOverlayVioletBloom[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    val emberLight = lerp(AuraColors.auroraOverlayEmberBloom[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val emberDeep = lerp(AuraColors.auroraOverlayEmberBloom[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    val decayLengthPx = EdgeGlowUniformMath.decayDepthDp(uniformState).toPx() *
                        reachScale * EdgeGlowUniformMath.reachFraction(perimeterBias)
                    shader.setFloatUniform("iResolution", size.width, size.height)
                    shader.setFloatUniform("iDecayLengthPx", decayLengthPx)
                    shader.setFloatUniform(
                        "iIgniteProgress",
                        EdgeGlowUniformMath.igniteProgress(uniformState, extras.reducedMotion),
                    )
                    shader.setFloatUniform(
                        "iIntensity",
                        EdgeGlowUniformMath.intensity(uniformState, t, extras.reducedMotion) * alphaScale,
                    )
                    shader.setFloatUniform(
                        "iCenterWeight",
                        EdgeGlowUniformMath.centerWeight(uniformState, centerWeightOverride, perimeterBias),
                    )
                    shader.setFloatUniform("iTopFadeFraction", EdgeGlowUniformMath.TOP_FADE_FRACTION)
                    shader.setFloatUniform(
                        "iWaveAmplitude",
                        if (extras.reducedMotion) 0f else EdgeGlowUniformMath.WAVE_AMPLITUDE,
                    )
                    shader.setFloatUniform("iWaveFreqX", waveFreqX)
                    shader.setFloatUniform(
                        "iWaveSpeedHz",
                        EdgeGlowUniformMath.waveSpeedHz(extras.reducedMotion) * transitionTracker.resolvedSpeed,
                    )
                    shader.setFloatUniform("iTime", elapsedInState)
                    shader.setFloatUniform("iVisible", visible.value)
                    shader.setFloatUniform("iPerimeterBloom", bloom)
                    // Neither the floor nor the hue-drift amount is reduced-motion-gated: both are
                    // static presence, not travel - only the drift SPEEDS are already 0 there.
                    shader.setFloatUniform(
                        "iPerimeterFloor",
                        EdgeGlowUniformMath.perimeterFloor(uniformState) * perimeterPresence *
                            EdgeGlowUniformMath.perimeterGain(perimeterBias),
                    )
                    // The border profile's three shader-side terms. Not reduced-motion-gated: like
                    // the floor and the hue field, a border is static presence, not travel - only
                    // the drift SPEEDS are 0 there, so reduced motion keeps an even, multi-hue,
                    // frozen border rather than falling back to a bottom wash.
                    shader.setFloatUniform("iPerimeterBias", EdgeGlowUniformMath.borderAmount(perimeterBias))
                    shader.setFloatUniform("iBottomWeight", EdgeGlowUniformMath.bottomWeight(perimeterBias))
                    // Not reduced-motion-gated for the same reason as the two above: a bound on
                    // how far the surface reaches is geometry, not travel.
                    shader.setFloatUniform("iRiseFraction", riseFraction)
                    shader.setFloatUniform("iHueDrift", hueDriftAmount)
                    shader.setFloatUniform("iColorLight", colorLight.red, colorLight.green, colorLight.blue)
                    shader.setFloatUniform("iColorDeep", colorDeep.red, colorDeep.green, colorDeep.blue)
                    shader.setFloatUniform("iAuroraLightB", violetLight.red, violetLight.green, violetLight.blue)
                    shader.setFloatUniform("iAuroraDeepB", violetDeep.red, violetDeep.green, violetDeep.blue)
                    shader.setFloatUniform("iAuroraLightC", emberLight.red, emberLight.green, emberLight.blue)
                    shader.setFloatUniform("iAuroraDeepC", emberDeep.red, emberDeep.green, emberDeep.blue)
                    drawRect(brush = brush)
                }
            },
    )
}

/**
 * Shader-free fallback for API 30-32 ([AuraShaders]): a static bottom-anchored vertical gradient
 * through the caller's OWN two-stop [colors] pair - pale at the true bottom edge fading through
 * deep to transparent, the same stop order the shader uses (`mix(deep, light, glow)`).
 *
 * What it keeps: the bottom anchor, the palette, the state's reach ([EdgeGlowUniformMath.decayDepthDp]
 * x the caller's [reachScale]), the state's static intensity, and the [visible] envelope - so a
 * dismiss still fades rather than snapping. What it drops, deliberately: the exponential's shape
 * (a linear ramp), the reach wave, the perimeter, the hue-drift field and the dither. Those are
 * AGSL mechanisms with no cheap Compose equivalent, and this path serves a television where the
 * glow is decorative; a second animation system to keep in sync with the shader would cost more
 * than it renders.
 */
@Composable
private fun ShaderFreeEdgeGlow(
    state: EdgeGlowState,
    modifier: Modifier,
    reachScale: Float,
    alphaScale: Float,
    colors: List<GradientStop>,
    visible: State<Float>,
    riseFraction: Float,
) {
    // reducedMotion = true is not an accessibility read here: it is how this object spells "the
    // static frame of this state", which is the only frame a gradient can draw.
    val intensity = EdgeGlowUniformMath.intensity(state, timeSeconds = 0f, reducedMotion = true)

    Box(
        modifier = modifier
            .fillMaxSize()
            .drawWithCache {
                // The caller's rise bound applies here too. This path has no perimeter to run the
                // full height, so it was never the surface the bound was written for - but a
                // caller asking for a bounded rise must not get an unbounded band merely because
                // the device is too old for the shader.
                val riseCeiling =
                    if (riseFraction > 0f) size.height * riseFraction else size.height
                val bandPx = (
                    EdgeGlowUniformMath.decayDepthDp(state).toPx() * reachScale *
                        EdgeGlowUniformMath.FALLBACK_BAND_DECAY_MULTIPLE
                    ).coerceIn(1f, riseCeiling.coerceAtLeast(1f))
                val brush = Brush.verticalGradient(
                    colors = listOf(Color.Transparent, colors[1].color, colors[0].color),
                    startY = size.height - bandPx,
                    endY = size.height,
                )
                onDrawBehind {
                    drawRect(
                        brush = brush,
                        alpha = (intensity * alphaScale * visible.value).coerceIn(0f, 1f),
                    )
                }
            },
    )
}
