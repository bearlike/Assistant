package com.mewbo.aura.ui.theme

import androidx.compose.animation.core.FiniteAnimationSpec
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.VisibilityThreshold
import androidx.compose.animation.core.spring
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp

/**
 * The single spring/motion source (spec §7 motion table, M1-M8). material3 1.4.0's
 * `MaterialExpressiveTheme` / `MotionScheme` / `ExperimentalMaterial3ExpressiveApi` are all
 * `internal` in the resolved AAR — verified by the Kotlin compiler ("internal in file"), not just
 * decompiled bytecode, which can't see Kotlin visibility. So [AuraTheme] uses plain `MaterialTheme`,
 * and this object is the fallback path: the only `spring(...)` construction site in the app. Each
 * constant is KDoc-tagged with its M-number so reviewers can diff against the spec table directly.
 */
object AuraMotion {
    // ---- M1 — Invocation edge-light (long-press → overlay, spec §7.0 timeline) ----
    /** M1: scrim fade-in duration. */
    const val scrimFadeMs: Int = 180

    /** M1: aurora edge-glow ignition — the bottom bloom ignites at bottom-center and rises to its
     * resting extent by this point (Rev E §E-1: bottom-anchored, top edge stays dark). */
    const val edgeSweepMs: Int = 450

    /** M1: floating composer bar slide+fade starts at this point in the timeline (spring, not tween). */
    const val barSlideStartMs: Int = 120

    /** M1: floating composer bar is settled by this point in the timeline. */
    const val barSlideSettleMs: Int = 340

    /** [R4 2026-07-10] M1 phase-2 "settle": the invocation perimeter bloom decays 1→0 over this
     * window (FastOutSlowIn — an exhale, not a linear wipe), starting when the 450ms ignite
     * completes. Top/side edges fade with it; the bottom term persists into the live states. */
    const val bloomSettleMs: Int = 950

    /** M1: spring damping ratio for the composer bar slide-up — also D-1's orb dock ("spring, same damping as M1"). */
    const val invocationSpringDamping: Float = 0.8f

    /** M1/D-1: the composer bar slide-up and the orb-dock spring both use this spec. */
    val invocationSpring: FiniteAnimationSpec<Float> =
        spring(dampingRatio = invocationSpringDamping, stiffness = Spring.StiffnessMedium)

    /** M1: dismiss plays the invocation sequence in reverse at this speed multiplier, no haptic. */
    const val dismissSpeedMultiplier: Float = 1.3f

    /** M1: edge glow hue keeps rotating at this rate while listening (M2). */
    const val edgeHueRotationHz: Float = 0.05f

    // ---- M2 — Listening ----
    /** M2: edge glow breathe intensity, peak-to-trough. */
    const val listeningBreatheAmplitude: Float = 0.20f

    /** Slowed 1200 → 6500 (three rounds) (user directive): the 1.2s cycle read as frantic
     * ("not a breathing machine") - the ambient glow should drift calmly. One shared token: every
     * breathe consumer (glow, orb) slows together. */
    const val listeningBreathePeriodMs: Int = 6500

    // ---- M3 — Thinking (send → first delta) ----
    /** M3: edge glow (overlay) / auroraWashTop (in-app) ramp-in duration; the spark/orb pulses as the thinking indicator. */
    const val thinkingRampMs: Int = 400

    // ---- M4 — Streaming text ----
    /** M4: per-word opacity fade duration (the comet-tail). */
    const val wordFadeMs: Int = 350
    const val trailWindowMinWords: Int = 2
    const val trailWindowMaxWords: Int = 6

    /** M4: on `completion`, the aurora wash fades out over this duration — the "settle" moment. */
    const val settleFadeMs: Int = 900

    /** M4: streaming-markdown reparse throttle. `TranscriptReducer.appendDelta` emits a new assistant
     * buffer per SSE token; reparsing + relaying-out the whole growing markdown tree on every one
     * (30–80 Hz) thrashes layout — the streaming jitter. `rememberStreamedText` samples the buffer to
     * one reparse per this window while streaming (~3 frames @60fps ≈ 20 Hz), imperceptible vs
     * per-token but ~4× fewer relayouts. 48ms is the widely-used LLM token-batching window (the
     * 30–50ms / 30fps-floor band). Pairs with [com.mewbo.aura.ui.common.MarkdownMessage]'s
     * `retainState` reparse, which removes the blank-flash. */
    const val markdownStreamReparseMs: Int = 48

    /** In-app bottom edge-glow run-end ease-OFF (user directive 2026-07-04 round 3): when a run
     * finishes, [com.mewbo.aura.ui.aurora.AuroraEdgeGlow] must fade to solid rest over AT LEAST 3s
     * rather than snap to black — an abrupt on→off luminance change reads as a flash and is a
     * photosensitivity trigger. Consumed caller-side as `AuroraEdgeGlow(dismissFadeMs = ...)` by
     * `ChatScreen` only; the assist overlay passes no override and keeps the quick
     * `EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS` dismiss. Deliberately much longer than
     * [settleFadeMs] (the reverted top-wash's 900ms settle). */
    const val edgeRestFadeMs: Int = 3000

    // ---- M5 — Composer morph ----
    /** M5: trailing-circle width/color morph + center-content crossfade; never a hard swap. */
    const val composerMorphMs: Int = 200
    val composerMorphSpring: FiniteAnimationSpec<Float> =
        spring(dampingRatio = Spring.DampingRatioNoBouncy, stiffness = Spring.StiffnessMedium)

    /** M5, `Color`-typed sibling of [composerMorphSpring]: `animateColorAsState` needs an
     * `AnimationSpec<Color>`, and `spring(...)` construction is reserved to this file. */
    val composerMorphColorSpring: FiniteAnimationSpec<Color> =
        spring(dampingRatio = Spring.DampingRatioNoBouncy, stiffness = Spring.StiffnessMedium)

    // ---- M7 — Action row ----
    const val actionRowFadeMs: Int = 250
    val actionRowRise: Dp = 4.dp

    // ---- Transcript item transitions (streaming reflow smoothing) ----
    /** Placement spring for the transcript's `LazyColumn` items (`Modifier.animateItem`): smooths
     * the abrupt pop-in / shuffle / pop-out of live-turn content — in-flight text growth, tool
     * activity rows, the spark, the disclaimer — that read as flicker. `StiffnessMediumLow`
     * tracks the ~20 Hz streaming reflow as a continuous drift rather than lagging behind it;
     * `NoBouncy` so a settling row never overshoots its resting position. Reduced motion drops
     * placement travel entirely (rows snap) — a fade is opacity, not travel, so the flat fades stay
     * (M8 spirit). `IntOffset.VisibilityThreshold` (1px) matches the platform `animateItem` default so
     * the spring resolves promptly instead of trailing sub-pixel. */
    val transcriptItemPlacementSpring: FiniteAnimationSpec<IntOffset> =
        spring(
            dampingRatio = Spring.DampingRatioNoBouncy,
            stiffness = Spring.StiffnessMediumLow,
            visibilityThreshold = IntOffset.VisibilityThreshold,
        )

    // ---- M8 — Reduced motion ----
    /**
     * M8 reduced-motion substitutions (documented, not all are numeric): edge glow → static
     * gradient frame; word-fade → flat per-delta block fade at [reducedBlockFadeMs]; waveform →
     * 3-dot pulse. [reducedMotion][AssistantExtras.reducedMotion] gates all of it via
     * `AuraTheme(reducedMotion = ...)` → `CompositionLocal`.
     */
    const val reducedBlockFadeMs: Int = 100

    /** M8 reduced-motion "opacity breathe" shared by every reduced-motion shader fallback in the
     * orb/spark brand-mark family ([com.mewbo.aura.ui.orb.rememberShaderTimeSeconds]'s
     * `reducedMotionBreatheAlpha`) - half-period of the slow sine breathe (full cycle = 2x this),
     * named once so `Orb.kt`'s `ReducedMotionOrb` and `AuraSpark.kt`'s `ReducedMotionSpark` share
     * one value instead of each hardcoding the same literal. */
    const val reducedMotionBreatheHalfPeriodMs: Int = 1800

    // ---- Orb state transitions (predate the M-table; values unchanged from v1's tuning) ----
    /** Orb state-change ease for desaturate/error-blend/scale — a state switch retargets the
     * shader's crossfaded uniforms through this instead of snapping them. */
    val orbTransitionSpring: FiniteAnimationSpec<Float> =
        spring(dampingRatio = Spring.DampingRatioMediumBouncy, stiffness = Spring.StiffnessMedium)

    /** Orb rotation-speed glide — slower, overshoot-free so angular velocity ramps rather than
     * bounces (the rotation ACCUMULATOR integrates this; overshoot would read as wobble). */
    val orbGlideSpring: FiniteAnimationSpec<Float> =
        spring(dampingRatio = Spring.DampingRatioNoBouncy, stiffness = Spring.StiffnessLow)
}
