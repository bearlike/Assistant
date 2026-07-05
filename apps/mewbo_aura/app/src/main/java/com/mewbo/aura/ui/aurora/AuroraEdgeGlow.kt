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
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.orb.GlslNoise
import com.mewbo.aura.ui.orb.rememberShaderTimeSeconds
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlin.math.PI
import kotlin.math.sin

// AGSL (RuntimeShader, API 33+) - rebuilt against four real-device reference-app overlay captures (Gitea
// #181; supersedes the Rev E frame-derived version). Three root-caused defects, all fixed here:
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
//      zero banding). A mandatory hash-noise dither term kills residual 8-bit banding - "no
//      dithering anywhere" was half the low-quality complaint. The halftone dot texture is deleted
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
// #181 review refinement, occlusion nuance: the worst remaining artifact on-device was a flat crop
// line where the opaque composer pill sliced across the old shader's still-bright plateau, plus
// concentric ring seams from the angular hue wheel with zero dither (confirmed: the old file
// imported GlslNoise.GLSL_CORE but never called it - dead import, no dithering ever actually ran).
// This rebuild fixes both by construction rather than by patching the symptom: verticalGlow always
// PEAKS at distFromBottom=0 (the true screen edge, mostly hidden below/behind the pill) and decays
// continuously from there - there is no bright plateau left for an opaque layer to crop a visible
// line across, and the mandatory hash dither (below) kills the ring-seam banding outright.
//
// #181 review refinement, state-dependent reach: idle-listening reads WIDE (measured: faint but
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
uniform float3 iColorLight;
uniform float3 iColorDeep;

${GlslNoise.GLSL_CORE}

half4 main(float2 fragCoord) {
    float2 res = iResolution;

    // Waviness: slow, low-frequency drift over (x, time) modulates the bloom's reach so it breathes
    // organically instead of reading as a frozen gradient - every real capture shows smooth, living
    // motion, never a static band.
    // The noise pattern TRANSLATES horizontally with time (x offset by iTime) so the undulation
    // reads as one slow wave flowing across the screen, not shimmer churning in place (user
    // directive); the slower second axis keeps successive passes from repeating identically.
    float wave = valueNoise(float2(
        fragCoord.x * iWaveFreqX + iTime * iWaveSpeedHz,
        iTime * iWaveSpeedHz * 0.37)) - 0.5;
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

    float glow = clamp(verticalGlow * horizontalWeight, 0.0, 1.0);

    // Top-edge window (the orb halo edge-window law, ui/CLAUDE.md; the SAME bug class as the orb's
    // once-visible square edge): a large caller reachScale (the in-app chat's 5.5x) leaves the
    // exponential falloff clearly nonzero at the TOP draw bound, which hard-stops into a thin
    // horizontal seam where the surface clips. Feather the bloom to EXACTLY 0 over the top
    // iTopFadeFraction of the surface height so there is no bright edge left for the clip bound to
    // slice a line across. Monotonic smoothstep, no plateau (Rule 2 intact); the mandatory dither
    // below still runs. Bottom-anchored small-reach callers (the overlay, reachScale 1) are already
    // ~0 this high up, so this is a no-op for them.
    glow *= smoothstep(0.0, res.y * iTopFadeFraction, fragCoord.y);

    // Intensity-driven two-stop blue blend, ordered per the measured scanlines: the BRIGHT bottom
    // edge reads as the PALE blue (#6D85B9 sampled at 0dp) and the fade toward the reach limit
    // passes through the deeper blue before vanishing into the scrim - the first cut of this
    // rebuild had the order inverted (deep at the bright edge), which read wrong against every
    // capture. Color still derives from the SAME bounded [glow] value that drives alpha, so there
    // is no separate phase/angle uniform left to drift out of sync with it.
    float3 col = mix(iColorDeep, iColorLight, glow);

    // Hash dither: per-pixel, uncorrelated with the smooth [wave] noise above, so it kills 8-bit
    // banding without adding visible structure of its own.
    float dither = (hash21(fragCoord) - 0.5) * (2.0 / 255.0);
    col += dither;

    // Measured, not lore: the bottom-edge sample (#6D85B9 over the #2A2A2E scrim, ref2 edge
    // column) back-solves to a near-opaque peak - the first cut's 0.5 cap ("glow, never a stripe",
    // Rev E eyeball-era wording) halved the real thing and, combined with an undershot decay
    // length, rendered the whole effect invisible behind the overlay's bottom chrome (the "renders
    // black" device-gate finding). The stripe-avoidance the old cap was protecting against comes
    // from the continuous exponential falloff now, not from starving peak alpha.
    float peakAlpha = 0.9;
    float alpha = clamp(glow * iIntensity * iVisible, 0.0, 1.0) * peakAlpha;
    return half4(col * alpha, alpha);
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

    // #181 review refinement (state-dependent reach): Listening keeps corners at ~50% of the
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

    // #181 P2 "the organic character": low-frequency spatial+temporal noise modulates the decay
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
    }

    fun targetVisible(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> 1f
        is EdgeGlowState.Listening -> 1f
        EdgeGlowState.Thinking -> 1f
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
        }
    }

    /** Resting decay length (Gitea #181 review refinement): [EdgeGlowState.Igniting] targets
     * Listening's WIDE reach (it is the entry animation immediately before Listening begins, per
     * §7.0's timeline), while [EdgeGlowState.Thinking] contracts to hug the pill. [Hidden]'s value
     * is unreachable in practice (alpha targets 0 there) but still must resolve to something for
     * the exhaustive `when`. */
    fun decayDepthDp(state: EdgeGlowState): Dp = when (state) {
        EdgeGlowState.Hidden -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Igniting -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Listening -> AuraColors.auroraOverlayBloomDecayDepth
        EdgeGlowState.Thinking -> AuraColors.auroraOverlayBloomDecayDepth * THINKING_DECAY_FRACTION
    }

    /** Horizontal center-weight strength per state (Gitea #181 review refinement) - see this
     * object's constants above for the measured targets each value is tuned to. */
    fun centerWeightStrength(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> LISTENING_CENTER_WEIGHT_STRENGTH
        is EdgeGlowState.Igniting -> LISTENING_CENTER_WEIGHT_STRENGTH
        is EdgeGlowState.Listening -> LISTENING_CENTER_WEIGHT_STRENGTH
        EdgeGlowState.Thinking -> THINKING_CENTER_WEIGHT_STRENGTH
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
 * Bottom-anchored edge glow (§6.10, §7.0, M1/M2; geometry rebuilt per Gitea #181 against four
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
) {
    val extras = LocalAssistantExtras.current

    val target = EdgeGlowUniformMath.targetVisible(state)
    // Gentle fade-OFF (see [dismissFadeMs]): opt-in, and only when easing to Hidden (target 0). Only
    // the alpha ENVELOPE ([visible]) is animated here; the held-frame resolution that lets Hidden fade
    // in place (rather than snap - its intensity 0f zeroes alpha, its wide reach pops the bloom) lives
    // in EdgeGlowTransitionTracker.resolveUniformState, mutated in the DRAW phase (this file's
    // no-LaunchedEffect transition-tracking law - see that tracker + the two time-based traps in
    // ui/aurora/CLAUDE.md; state-dependent reach itself is #181's Listening-wide vs Thinking-contracted).
    val fadeOffMs = if (target == 0f) dismissFadeMs else null
    // Gitea #181 fix wave, finding 7: kept as an un-destructured State<Float> (no `by`) so reading
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
    val colorLight = AuraColors.auroraOverlayBloom[0].color
    val colorDeep = AuraColors.auroraOverlayBloom[1].color
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
                    shader.setFloatUniform("iColorLight", colorLight.red, colorLight.green, colorLight.blue)
                    shader.setFloatUniform("iColorDeep", colorDeep.red, colorDeep.green, colorDeep.blue)
                    drawRect(brush = brush)
                }
            },
    )
}
