package com.mewbo.aura.ui.aurora

import android.graphics.RuntimeShader
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
import androidx.compose.ui.graphics.ShaderBrush
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.orb.GlslNoise
import com.mewbo.aura.ui.orb.rememberShaderTimeSeconds
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.GradientStop
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlin.math.PI
import kotlin.math.sin

// AGSL (RuntimeShader, API 33+) - rebuilt against four real-device reference-app overlay captures
// (supersedes the Rev E frame-derived version). Three root-caused defects, all fixed here:
//   1. "Cropped in a circular arc" - the old igniteMask was a growing CIRCLE from bottom-center,
//      radius res.x*0.5, which under-covers the diagonal to the corners (>40% brightness loss
//      starting ~220px in from each edge, exact zero within ~88px of the edges - verified
//      numerically in the investigation). There is no radial mask at all now: ignition instead
//      grows the vertical falloff's OWN reach from near-zero to its resting depth (see
//      [decayLength] below), which by construction covers every column at the same rate - nothing
//      left to crop against a diagonal.
//   2. "Opaque/banded, not a smooth blend" - the old flat plateau (0..624px @ 100%) + a short
//      168px feather is gone, replaced by one continuous exponential decay from the bottom edge:
//      asymptotic, never a hard edge, matching every measured scanline (smooth monotonic falloff,
//      zero banding). A mandatory dither term kills residual 8-bit banding - "no dithering
//      anywhere" was half the low-quality complaint. The halftone dot texture is deleted
//      outright: it does not appear in any of the four real captures at any zoom level.
//   3. Wrong hue family - the old shader was a 4-stop angular wheel (blue/green/clay/violet) with a
//      periodic hue-rotation bug (team-lead review, W1-B capture 01: blue/green could periodically
//      dominate the visible band). Every scanline across two real states (idle-listening,
//      streaming; 9 columns total) is blue-family only - zero green/clay/violet observed. The hue
//      is now a plain two-stop blend by the bloom's OWN intensity (pale where faint, deep where
//      bright) - matches the measured pattern (deep near the source, pale toward the reach limit)
//      and removes the whole hue-rotation bug class, since there is no angle math or phase uniform
//      left to misalign.
// A subtle horizontal center-weighting (brightest under/around the composer, fainter but never
// zero toward the corners) and a slow, low-frequency waviness (GlslNoise over x and time) give it
// the organic, "living" character the flat version lacked, per ref2/ref3.
//
// occlusion nuance: the worst remaining artifact on-device was a flat crop
// line where the opaque composer pill sliced across the old shader's still-bright plateau, plus
// concentric ring seams from the angular hue wheel with zero dither (confirmed: the old file
// imported GlslNoise.GLSL_CORE but never called it - dead import, no dithering ever actually ran).
// This rebuild fixes both by construction rather than by patching the symptom: verticalGlow always
// PEAKS at distFromBottom=0 (the true screen edge, mostly hidden below/behind the pill) and decays
// continuously from there - there is no bright plateau left for an opaque layer to crop a visible
// line across, and the mandatory dither (below, [GlslNoise.ditherPremul]) kills the ring-seam
// banding outright.
//
// state-dependent reach: idle-listening reads WIDE (measured: faint but
// real reach into the bottom corners, visible up to ~143dp+ at the screen edges); streaming/
// thinking contracts to hug the pill region with no corner reach. Both states share the SAME
// bottom-edge peak position above - only the reach (decayLength) and how aggressively corners are
// suppressed (centerWeight) differ, via [EdgeGlowUniformMath.decayDepthDp]/[EdgeGlowUniformMath
// .centerWeightStrength]. A contracted decay length alone can't suppress corners on its own (the
// bottom-LEFT/RIGHT corners sit on the same distFromBottom=0 row as the peak), so Thinking also
// gets a much stronger center-weight to read as "no corner reach" despite still peaking there.
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

    // Waviness: slow, low-frequency drift over (x, time) modulates the bloom's reach so it breathes
    // organically instead of reading as a frozen gradient - every real capture shows smooth, living
    // motion, never a static band.
    // The noise pattern TRANSLATES horizontally with time (x offset by iTime) so the undulation
    // reads as one slow wave flowing across the screen, not shimmer churning in place (user
    // directive); the slower second axis keeps successive passes from repeating identically.
    // [R5] Two octaves: the coarse swell carries the slow flowing wave; the finer, faster octave
    // breaks its silhouette so the boundary reads as liquid, not a single sine-like undulation.
    // Both are iTime × rate terms — phase-continuous by construction (§7.12).
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

    // Continuous exponential falloff from the bottom edge - no plateau, no hard extent, asymptotic
    // to zero. Scaling decayLength by iIgniteProgress above IS the whole ignition animation: at
    // progress ~0 only a thin sliver at the very bottom edge is lit, widening as progress grows to
    // its resting reach - no circular radial mask, so there is nothing to crop against the corners
    // at any progress value.
    float distFromBottom = max(res.y - fragCoord.y, 0.0);
    float verticalGlow = exp(-distFromBottom / max(decayLength, 0.5));

    // Horizontal center-weighting (measured ref2/ref3): brightest under/around the composer,
    // fainter but never fully zero toward the corners.
    float distFromCenterX = abs(fragCoord.x - res.x * 0.5);
    float horizontalWeight = 1.0 - iCenterWeight * smoothstep(0.0, res.x * 0.5, distFromCenterX);

    float vGlow = clamp(verticalGlow * horizontalWeight, 0.0, 1.0);

    // Top-edge window (the orb halo edge-window law, ui/CLAUDE.md; the SAME bug class as the orb's
    // once-visible square edge): a large caller reachScale (the in-app chat's 5.5x) leaves the
    // bottom-anchored falloff clearly nonzero at the TOP draw bound, which hard-stops into a thin
    // horizontal seam where the surface clips. Feather it to EXACTLY 0 over the top iTopFadeFraction
    // of the surface height so there is no bright edge left for the clip bound to slice a line
    // across. It applies to the BOTTOM-ANCHORED term ONLY: the perimeter terms below are
    // edge-anchored - they PEAK at the draw bounds by construction, exactly like this bottom term
    // peaks at the bottom bound, so there is no mid-falloff value for a clip edge to slice a seam
    // across (§7.9 is about clipped falloffs, not edge anchors). Monotonic smoothstep, no plateau
    // (Rule 2 intact); the mandatory dither below still runs. A no-op for the small-reach overlay.
    vGlow *= smoothstep(0.0, res.y * iTopFadeFraction, fragCoord.y);

    // [R4] Phase-1 invocation perimeter bloom: per-row/per-column exponential falloffs from the
    // side and top edges (Rule 1: no radial masks - same construction as the bottom term, rotated),
    // weighted by a strong bottom bias so the light reads as EMANATING from the pill: bottom corners
    // full strength, fading to ~25% at the top; the top edge gets its own faint short term. The
    // existing valueNoise wave already modulates decayLength, which sideDecay derives from - the
    // bloom's inner boundary undulates organically with zero new noise terms at this [R4] layer
    // (the [R5] sideLevel noise below is a separate, later addition to sideDecay itself). The
    // bloom term (iPerimeterBloom) still collapses to 0 at the settle; what keeps the perimeter
    // edge-lit through the live states past that point is the [R5] iPerimeterFloor below, via
    // max(iPerimeterBloom, iPerimeterFloor) - the bloom, not the floor, wins whenever it's live.
    // [R5] Fluid level: a per-ROW wave modulates the side reach so the edge light visibly rises
    // and falls along the screen edges (liquid-level read). Same amplitude token as the bottom
    // wave; phase-continuous (iTime × rate).
    float sideLevel = valueNoise(float2(
        fragCoord.y * iWaveFreqX * 3.5 + iTime * iWaveSpeedHz * 1.4,
        iTime * iWaveSpeedHz * 0.53 + 11.7)) - 0.5;
    float sideDecay = max(decayLength * 0.6 * (1.0 + sideLevel * 2.0 * iWaveAmplitude), 0.5);
    float sideGlow = exp(-fragCoord.x / sideDecay) + exp(-(res.x - fragCoord.x) / sideDecay);
    float heightFrac = fragCoord.y / res.y;
    // [R5] stronger edges: bias floor 0.25 -> 0.45, top term 0.35 -> 0.5 (device feedback).
    float bottomBias = mix(0.45, 1.0, heightFrac * heightFrac);
    float topGlow = exp(-fragCoord.y / max(sideDecay * 0.5, 0.5)) * 0.5;
    // [R5] the floor keeps the live states edge-lit after the bloom settles; bloom still wins.
    float perimeter = clamp((sideGlow * bottomBias + topGlow) * max(iPerimeterBloom, iPerimeterFloor), 0.0, 1.0);

    float glow = clamp(vGlow + perimeter, 0.0, 1.0);

    // [R5] Aurora hue field (Rule 4b): a slow, bounded, APERIODIC value-noise drift travels the
    // hue through three families (A blue -> B violet -> C ember) while INTENSITY still selects
    // light-vs-deep within the sampled family and still drives alpha — the Rule 4 alpha/hue
    // coupling is intact, and the outlawed bug class (a PERIODIC ANGULAR wheel with a phase
    // uniform) cannot recur: noise is bounded and never rotates. iHueDrift = 0 collapses to the
    // pure A pair (mix(a, b, 0) == a) and skips the noise sample — the chat byte-path. The bright
    // bottom edge still reads as each family's PALE stop fading through its deep stop (Rule 6
    // order preserved: mix(deep, light, glow)).
    float hueT = 0.0;
    if (iHueDrift > 0.0) {
        float hueNoise = valueNoise(float2(
            fragCoord.x * iWaveFreqX * 0.55 + iTime * iWaveSpeedHz * 0.63,
            fragCoord.y * iWaveFreqX * 0.35 + iTime * iWaveSpeedHz * 0.29 + 5.43));
        hueT = clamp(hueNoise, 0.0, 1.0) * iHueDrift;
    }
    // Branch-free two-segment family blend (AGSL note: avoids the ternary-with-float3 form, which
    // risks a runtime-compiler failure that would render the whole shader black): segAB runs A->B
    // as hueT climbs 0->0.5 (its clamp saturates to B past 0.5), segBC then runs B->C over 0.5->1
    // — bit-for-bit the two-branch ternary, with no scalar-cond branch on a vector result.
    float segAB = clamp(hueT * 2.0, 0.0, 1.0);
    float segBC = clamp((hueT - 0.5) * 2.0, 0.0, 1.0);
    float3 hueLight = mix(mix(iColorLight, iAuroraLightB, segAB), iAuroraLightC, segBC);
    float3 hueDeep = mix(mix(iColorDeep, iAuroraDeepB, segAB), iAuroraDeepC, segBC);
    float3 col = mix(hueDeep, hueLight, glow);

    // Measured, not lore: the bottom-edge sample (#6D85B9 over the #2A2A2E scrim, ref2 edge
    // column) back-solves to a near-opaque peak - the first cut's 0.5 cap ("glow, never a stripe",
    // Rev E eyeball-era wording) halved the real thing and, combined with an undershot decay
    // length, rendered the whole effect invisible behind the overlay's bottom chrome (the "renders
    // black" device-gate finding). The stripe-avoidance the old cap was protecting against comes
    // from the continuous exponential falloff now, not from starving peak alpha.
    float peakAlpha = 0.9;
    float alpha = clamp(glow * iIntensity * iVisible, 0.0, 1.0) * peakAlpha;

    // Mandatory dither (Rule 3), via the ONE shared primitive — applied to the PREMULTIPLIED colour,
    // the only space that is actually quantized to 8 bits. Dithering `col` before the `* alpha` (as
    // this shader did until now) scales the perturbation BY alpha, so it faded out precisely in the
    // faint reaches where the ramp is flattest and banding is worst. Alpha itself is left
    // un-dithered on purpose: the compositor computes `premul + dst*(1-alpha)`, so an alpha
    // quantization error of 0.5/255 perturbs the result by only `dst * 0.5` LSB — against this
    // family's near-black canvas (dst ~14/255) that is ~0.03 LSB, far below anything visible.
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

    // §7.0 rule 5: dismissal plays M1's ignite growth in reverse at AuraMotion.dismissSpeedMultiplier;
    // no separate duration is named for Hidden<->active transitions outside that growth itself, so
    // this derives from the two M1 tokens rather than a private literal.
    val VISIBILITY_TRANSITION_MS = (AuraMotion.edgeSweepMs / AuraMotion.dismissSpeedMultiplier).toInt()

    // (state-dependent reach): Listening keeps corners at ~50% of the
    // vertical-only brightness ("faint reach"); Thinking suppresses them to ~15% ("no corner
    // reach") since a contracted decay length alone can't do that on its own (corners sit on the
    // same distFromBottom=0 row as the peak - see the shader header above).
    private const val LISTENING_CENTER_WEIGHT_STRENGTH = 0.5f
    private const val THINKING_CENTER_WEIGHT_STRENGTH = 0.85f

    // Top-edge fade window (orb halo edge-window law, ui/CLAUDE.md): the fraction of the surface
    // HEIGHT, measured down from the top edge, over which the bloom is feathered to exactly 0 - kills
    // the thin horizontal seam a large reachScale (the in-app 5.5x) would otherwise hard-stop into at
    // the top draw bound. A behavioral tuning constant, not a measured token; a no-op for small-reach
    // bottom-anchored callers whose bloom is already ~0 that high up.
    const val TOP_FADE_FRACTION = 0.18f

    // Thinking's decay length as a fraction of AuraColors.auroraOverlayBloomDecayDepth (the
    // measured WIDE/Listening reach) - a behavioral tuning ratio, not a second measured token,
    // same convention the old RESTING_EXTENT_FRACTION used before this rebuild retired it.
    private const val THINKING_DECAY_FRACTION = 0.22f

    // [R4 2026-07-10] Overlay session-resting pool: fractions of the ONE measured reach token
    // (auroraOverlayBloomDecayDepth) / of the active baseline - behavioral tuning constants per
    // this file's provenance rule, deliberately NOT the deleted chat-wash RESTING_* constants
    // (ui/aurora/CLAUDE.md regression note - that was the CHAT surface; this is the overlay).
    private const val OVERLAY_RESTING_DECAY_FRACTION = 0.45f
    private const val OVERLAY_RESTING_INTENSITY = 0.55f

    // [R5 2026-07-11] Persistent edge-lit floors: the perimeter terms no longer collapse to zero
    // after the bloom settles — the live states keep a faint side/top presence so the overlay
    // reads as edge-lit ("not strong towards the edges" device feedback). Behavioral tuning
    // ratios of the bloom's full perimeter strength; Thinking stays lowest (its contracted,
    // corner-suppressed profile is a designed state, not an edge-lit one).
    private const val LISTENING_PERIMETER_FLOOR = 0.35f
    private const val RESTING_PERIMETER_FLOOR = 0.30f
    private const val THINKING_PERIMETER_FLOOR = 0.15f

    /** [R5 2026-07-11] Per-state persistent perimeter presence (0 = bottom-only). Scaled by the
     * composable's caller knob (`perimeterPresence`: overlay 1f, chat default 0f → byte-path). */
    fun perimeterFloor(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> LISTENING_PERIMETER_FLOOR
        is EdgeGlowState.Listening -> LISTENING_PERIMETER_FLOOR
        EdgeGlowState.Thinking -> THINKING_PERIMETER_FLOOR
        EdgeGlowState.Resting -> RESTING_PERIMETER_FLOOR
    }

    // "the organic character": low-frequency spatial+temporal noise modulates the decay
    // length by up to this fraction, drifting slowly - a wobble, never a hard geometric change.
    const val WAVE_AMPLITUDE = 0.22f // raised 0.15->0.22 with the flowing-wave translation (user directive: visible, calm undulation)
    private const val WAVE_SPATIAL_CYCLES_ACROSS_WIDTH = 1.5f
    private const val WAVE_DRIFT_HZ = 0.035f // slow drift, matches M2/M4's "atmospheric" pacing

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
     * shader's [AURORA_EDGE_SHADER_SRC] `decayLength` scale directly - see that file's header for
     * why this replaces the old circular radial mask. */
    fun igniteProgress(state: EdgeGlowState, reducedMotion: Boolean): Float {
        if (reducedMotion) return 1f // M8: static bloom frame, no growth animation
        return when (state) {
            EdgeGlowState.Hidden -> 1f
            is EdgeGlowState.Igniting -> state.progress.coerceIn(0f, 1f)
            is EdgeGlowState.Listening -> 1f
            EdgeGlowState.Thinking -> 1f
            EdgeGlowState.Resting -> 1f
        }
    }

    /** Resting decay length (review refinement): [EdgeGlowState.Igniting] targets
     * Listening's WIDE reach (it is the entry animation immediately before Listening begins, per
     * §7.0's timeline), while [EdgeGlowState.Thinking] contracts to hug the pill. [Hidden]'s value
     * is unreachable in practice (alpha targets 0 there) but still must resolve to something for
     * the exhaustive `when`. [EdgeGlowState.Resting] ([R4 2026-07-10], overlay-scoped) sits between
     * the two: a session-lifetime pool wider than Thinking's contracted hug but narrower than
     * Listening's full wide reach. */
    fun decayDepthDp(state: EdgeGlowState): Dp = when (state) {
        EdgeGlowState.Hidden -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Igniting -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Listening -> AuraColors.auroraOverlayBloomDecayDepth
        EdgeGlowState.Thinking -> AuraColors.auroraOverlayBloomDecayDepth * THINKING_DECAY_FRACTION
        EdgeGlowState.Resting -> AuraColors.auroraOverlayBloomDecayDepth * OVERLAY_RESTING_DECAY_FRACTION
    }

    /** Horizontal center-weight strength per state (review refinement) - see this
     * object's constants above for the measured targets each value is tuned to. [Resting] reuses
     * Listening's strength ([R4 2026-07-10]) - it is not a corner-suppression concern, unlike
     * Thinking. */
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
        // [R4 2026-07-10] static - no time term, unlike Listening's breathe: a session-lifetime
        // presence pool must not oscillate (it would read as still "doing something").
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
 * Deliberately NOT a `LaunchedEffect(transitionKey) { stateEnteredAt = timeSeconds.value }` (the
 * pattern [com.mewbo.aura.ui.orb.OrbUniformMath] uses for its error-pulse envelope): that pattern
 * races against [rememberShaderTimeSeconds]'s own internal `LaunchedEffect` - both launch on the
 * same composition pass with no guaranteed relative order, so the transition-tracking effect can
 * read `timeSeconds` before the frame clock has produced its first tick, capturing 0 instead of the
 * true (large, absolute-clock-based) current time and silently re-breaking the phase-reset this
 * class exists to fix. Comparing inside the draw phase instead is race-free: draw strictly follows
 * that frame's `timeSeconds` update, always. Still consumed by the rebuilt shader (feeds the
 * waviness noise's time input, `iTime` below) so the wavy drift always starts from a stable phase
 * at state entry rather than an ever-growing absolute clock.
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
     * uniforms cannot fade (its intensity is 0f, which zeroes alpha instantly; its reach is wide and
     * would pop the bloom), so the frame is HELD in place and only the alpha envelope (`iVisible`)
     * ramps to 0. The matching wave speed is held too (via [resolvedSpeed]) so the drift neither jumps
     * nor slows mid-fade, and keying `iTime` off THIS (held) state means the phase never resets
     * mid-fade either. Mutated + read EXCLUSIVELY inside `onDrawBehind` (this file's
     * no-LaunchedEffect transition-tracking law - same reason [elapsedSinceTransition] lives here).
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
 * Bottom-anchored edge glow (§6.10, §7.0, M1/M2; geometry rebuilt against four
 * real-device captures - supersedes the Rev E frame-derived version). [AuraColors.auroraOverlayBloom]'s
 * two stops (pale/deep blue) are blended by the bloom's own intensity, not by angle or position -
 * see [AURORA_EDGE_SHADER_SRC]'s header comment for the full root-cause-to-fix mapping.
 * [EdgeGlowState.Igniting]'s progress is driven by the caller (mirrors [OrbState]'s externally-driven
 * `rmsDb` pattern) - this composable only turns it into pixels.
 */
@Composable
fun AuroraEdgeGlow(
    state: EdgeGlowState,
    modifier: Modifier = Modifier,
    /** Multiplies the state's decay length. 1f = the overlay's capture-measured reach. The in-app
     * chat landing passes a larger scale (user directive: the glow must rise at least a bit above
     * the composer area there - the overlay-measured reach reads tiny under the app's taller
     * bottom stack). A caller-side design knob, not a second measured token. */
    reachScale: Float = 1f,
    /** Replaces the state's horizontal center-weighting when non-null. The state defaults produce
     * a center-anchored bloom (correct for the overlay, whose glow radiates from the pill); the
     * in-app landing passes a near-zero value so the bloom covers the WHOLE bottom edge evenly
     * (user directive: "not a tick coming from the center"). */
    centerWeightOverride: Float? = null,
    /** Scales the final alpha (on top of the state's intensity envelope). The in-app landing runs
     * softer than the overlay's capture-measured peak (user directive: less opaque, smoother). */
    alphaScale: Float = 1f,
    /** Multiplies the wave-drift speed ONLY (the slow horizontal undulation, `iWaveSpeedHz`) - it
     * leaves reach/center-weight/alpha and the breathe cadence untouched. 1f = the state's own pace.
     * The in-app chat passes a >1 value for the ACTIVE (Thinking) glow so a live run reads slightly
     * livelier (user directive 2026-07-04); idle/overlay callers keep the default. Reduced-motion is
     * unaffected (the base speed is already 0 there). A caller-side behavioral knob, not a measured
     * token (ui/aurora/CLAUDE.md provenance rule).
     *
     * SMOOTHNESS (ui/aurora/CLAUDE.md time-trap): this scales the RATE (`iWaveSpeedHz`), NEVER the
     * accumulated phase. The phase input `iTime` is [EdgeGlowTransitionTracker]'s `elapsedInState`,
     * which resets to 0 at every state transition - and a state transition is the ONLY moment
     * `speedScale` changes (it is derived from the glow state). At `iTime == 0` the term
     * `iTime * iWaveSpeedHz` is 0 regardless of scale, so a scale change can never jump the phase
     * mid-flight; within a state the phase grows continuously from 0 at the scaled rate. */
    speedScale: Float = 1f,
    /** Opt-in gentle fade-OFF duration (ms) for the run-end / dismiss transition to
     * [EdgeGlowState.Hidden]. `null` (the overlay default) keeps the quick
     * [EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS] dismiss. The in-app chat passes a long value
     * ([com.mewbo.aura.ui.theme.AuraMotion.edgeRestFadeMs], >=3s) so the bottom glow eases OFF
     * smoothly when a run ends instead of snapping to black — an abrupt on->off luminance change is a
     * photosensitivity trigger (user directive). While such a fade is in flight the LAST active
     * state's whole frame (reach, intensity, wave phase AND speed) is HELD and only the alpha
     * envelope ([iVisible]) ramps to 0: snapping to [Hidden]'s own uniforms cannot fade — its
     * intensity is 0f (would zero alpha instantly, making the ramp moot) and its reach is WIDE (would
     * pop the bloom wider for the fade). Holding the speed too is load-bearing: the caller drops
     * [speedScale] at the state flip, and a wave-rate change while `iTime` is already large would
     * jump the drift (the §12 "speed changes that jump" trap, ui/aurora/CLAUDE.md). A caller-side
     * behavioral knob, same provenance rule as reach/alpha/speed above. */
    dismissFadeMs: Int? = null,
    /** [R4 2026-07-10] The two-stop color pair (Rule 4: blended by the glow's own intensity,
     * never angle). Default = the chat-darkened [AuraColors.auroraOverlayBloom] so ChatScreen is
     * untouched; the overlay passes [AuraColors.auroraOverlayLiveBloom] (capture-restored,
     * visible). Caller-knob, never fork — same law as reachScale/alphaScale/dismissFadeMs. */
    colors: List<GradientStop> = AuraColors.auroraOverlayBloom,
    /** [R4 2026-07-10] Phase-1 perimeter-bloom envelope (0..1), caller-animated (the overlay's
     * invocation choreography owns the timeline; this composable only turns it into pixels — the
     * same externally-driven pattern as [EdgeGlowState.Igniting]'s progress). Also lerps the color
     * pair from [AuraColors.auroraIgnitionBloom] (bloom=1) down to the caller's [colors] pair
     * (bloom=0) — the overlay's live pair; chat never raises bloom, so its darkened pair renders
     * unchanged. Read in the draw phase only. Forced 0 under reduced motion (no traveling light). */
    perimeterBloom: State<Float>? = null,
    /** [R5 2026-07-11] 0..1 amount of the aurora hue-drift field (A blue -> B violet -> C ember).
     * Default 0 = the legacy pure-[colors] two-stop path — ChatScreen untouched, byte-path
     * (the shader skips the hue-noise sample entirely at 0). The overlay passes 1f. Reduced
     * motion keeps the FIELD (static multi-hue frame): all drift speeds are already 0 there. */
    hueDriftAmount: Float = 0f,
    /** [R5 2026-07-11] scales [EdgeGlowUniformMath.perimeterFloor] (persistent edge-lit side/top
     * presence in the live states). Default 0 = bottom-only (chat byte-path); overlay passes 1f. */
    perimeterPresence: Float = 0f,
) {
    val extras = LocalAssistantExtras.current

    val target = EdgeGlowUniformMath.targetVisible(state)
    // Gentle fade-OFF (see [dismissFadeMs]): opt-in, and only when easing to Hidden (target 0). Only
    // the alpha ENVELOPE ([visible]) is animated here; the held-frame resolution that lets Hidden fade
    // in place (rather than snap - its intensity 0f zeroes alpha, its wide reach pops the bloom) lives
    // in EdgeGlowTransitionTracker.resolveUniformState, mutated in the DRAW phase (this file's
    // no-LaunchedEffect transition-tracking law - see that tracker + the two time-based traps in
    // ui/aurora/CLAUDE.md; state-dependent reach itself is v5's Listening-wide vs Thinking-contracted).
    val fadeOffMs = if (target == 0f) dismissFadeMs else null
    // kept as an un-destructured State<Float> (no `by`) so reading
    // it doesn't subscribe THIS composable to recompose on every animation frame during a
    // visibility transition - only `onDrawBehind` below reads `.value`, matching Orb.kt's own
    // pattern for every one of its animated uniforms. The one place this composable genuinely needs
    // to react in COMPOSITION (deciding whether to render at all) goes through `derivedStateOf`
    // instead, which only triggers recomposition when the boolean itself flips, not on every
    // continuous float tick in between - keyed on `target` since that's a plain local recomputed
    // fresh each recomposition, not a remembered value the closure could go stale over.
    val visible: State<Float> = animateFloatAsState(
        targetValue = target,
        animationSpec = when {
            extras.reducedMotion -> snap()
            // Gentle >=3s LINEAR ease-OFF when a run ends (user directive; photosensitivity - a
            // constant, controlled rate with no fast segment). The quick dismiss below stays for the
            // fade-IN and for callers (the overlay) that pass no override.
            fadeOffMs != null -> tween(fadeOffMs, easing = LinearEasing)
            else -> tween(EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS)
        },
        label = "aurora-edge-visible",
    )
    val shouldRender by remember(target) { derivedStateOf { visible.value > 0f || target > 0f } }

    if (!shouldRender) return

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
                    // Draw-phase held-frame resolution (see EdgeGlowTransitionTracker): during an
                    // opt-in dismiss fade this is the LAST ACTIVE state, else the live one. Keying the
                    // uniforms (incl. transitionKey/iTime and decayLength) off it means a fade-off
                    // holds the active frame and never resets phase or pops the reach mid-fade.
                    val uniformState = transitionTracker.resolveUniformState(
                        state = state,
                        speedScale = speedScale,
                        holdOnDismiss = dismissFadeMs != null,
                    )
                    val elapsedInState = transitionTracker.elapsedSinceTransition(
                        transitionKey = EdgeGlowUniformMath.transitionKey(uniformState),
                        timeSeconds = t,
                    )
                    // [R4] Phase-1 perimeter-bloom envelope, caller-animated (see [perimeterBloom]).
                    // Forced 0 under reduced motion (no traveling light) — this composable's own belt
                    // to the overlay choreography's suspenders. The ignition color pair is lerped
                    // CPU-side here (bloom=1 → the brand ignition pair, bloom=0 → the caller's own
                    // [colors] pair) so the shader stays a two-stop intensity blend (Rule 4), never a
                    // 3rd stop. Read in the draw phase (like visible.value / timeSeconds) so reading
                    // perimeterBloom's per-frame value never subscribes THIS composable to recompose.
                    val bloom = if (extras.reducedMotion) 0f else (perimeterBloom?.value ?: 0f)
                    val colorLight = lerp(colors[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val colorDeep = lerp(colors[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    // [R5] The B/C hue families lerp toward the SAME ignition pair as the A pair
                    // above, so a bloom unifies the whole multi-hue field into brand ignition light
                    // and drifts back apart as it settles. CPU-side (Rule 4: the shader still sees
                    // plain two-stop pairs, never a 3rd stop or an angle term).
                    val violetLight = lerp(AuraColors.auroraOverlayVioletBloom[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val violetDeep = lerp(AuraColors.auroraOverlayVioletBloom[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    val emberLight = lerp(AuraColors.auroraOverlayEmberBloom[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val emberDeep = lerp(AuraColors.auroraOverlayEmberBloom[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    val decayLengthPx = EdgeGlowUniformMath.decayDepthDp(uniformState).toPx() * reachScale
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
                        centerWeightOverride ?: EdgeGlowUniformMath.centerWeightStrength(uniformState),
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
                    // [R5] Persistent edge-lit floor (per-state × caller presence knob) and the
                    // hue-drift amount. Neither is reduced-motion-gated: the floor is a STATIC edge
                    // presence and the hue field stays (a static multi-hue frame) — only the drift
                    // SPEEDS are already 0 under reduced motion, so nothing travels.
                    shader.setFloatUniform(
                        "iPerimeterFloor",
                        EdgeGlowUniformMath.perimeterFloor(uniformState) * perimeterPresence,
                    )
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
