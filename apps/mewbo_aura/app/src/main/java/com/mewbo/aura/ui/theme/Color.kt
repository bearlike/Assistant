package com.mewbo.aura.ui.theme

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/**
 * Placeholder fill for hand-rolled [androidx.compose.ui.graphics.vector.ImageVector]s
 * (`ui/chat/ChatIcons.kt`). `ImageVector.Builder.path {}` requires a non-null, opaque [Color] to
 * paint anything at all - [PathComponent][androidx.compose.ui.graphics.vector.PathComponent]
 * skips its `drawPath` call entirely when `fill == null`, and `Color.Unspecified` has zero alpha,
 * so either would silently render the glyph invisible. `Icon(tint = ...)` always replaces this
 * value via a `SrcIn` color filter at every call site (same convention the generated AndroidX
 * icon files use) - it is never a visible design color, just the one place that fact is on record.
 */
internal val VectorGlyphFill: Color = Color.Black

/**
 * One color-gradient anchor: a hue plus its position (0f=start, 1f=end) along whatever axis the
 * spec describes (linear blend, radial falloff, or perimeter sweep) — the renderer picks the
 * geometry (W1-B), this only carries the data.
 */
data class GradientStop(val color: Color, val fraction: Float)

/**
 * Design-v2 token set — spec §3 (Aura Design Language Reconciliation, Rev C) + Rev D §D-2/§D-3
 * branding deltas. Every literal `Color(...)` for the app's *design language* lives here, verbatim
 * against the spec doc, token names matching exactly (module CLAUDE.md theme discipline). Dark-only
 * v1 (spec §2): there is no light palette — [AuraOrbColors] (orb v2 shader palette, pre-existing,
 * unrelated to this spec) is the only other `Color(...)` site in the app.
 */
object AuraColors {
    // ---- §3.1 Neutrals & surfaces ----
    val surfaceCanvas = Color(0xFF000000)
    val surfaceInput = Color(0xFF1E1F23)

    /** `#141518` at ~92% alpha (spec estimate) — user bubble fill when rendered over an aurora wash. */
    val surfaceBubbleOnWash = Color(0xEB141518)
    val surfaceSelected = Color(0xFF26282C)

    /** `#0A0A0A` at 80% alpha — pill/circle scrims behind top-bar icons when over an aurora wash. */
    val surfaceIconScrim = Color(0xCC0A0A0A)
    val textPrimary = Color(0xFFE9EAED)
    val textSecondary = Color(0xFF9AA0A6)
    val textTertiary = Color(0xFF5F6368)
    val iconPrimary = Color(0xFFE9EAED)
    val outlineHairline = Color(0xFF2A2B2E)

    // ---- §3.2 Accent & brand ----
    val accentPrimary = Color(0xFF4C6EF5)
    val accentOnAccent = Color(0xFFFFFFFF)
    val accentMuted = Color(0xFF3A4570)
    val accentError = Color(0xFFE46962)

    /**
     * Brand mark gradient (Rev D §D-2): conic clay → coral → violet → gold. Supersedes the spec's
     * four-point-star stops — designer's stops 2-4 retained, `#4285F4` replaced by brand clay as
     * the leading stop. Rendered by the clay-flower SDF orb/spark (out of this task's ownership).
     */
    val sparkGradient: List<Color> = listOf(
        Color(0xFFC15F3C), // clay (brand), leading stop
        Color(0xFFD96570),
        Color(0xFF9B72CB),
        Color(0xFFF2A60C),
    )

    // ---- §6.2 C5 / §6.9 component-specific fills — not in the original §3 table, added once
    // downstream tasks hit real gaps; Gitea #181's four real-device captures (pixel-sampled,
    // outrank the earlier Rev E frame estimate) confirm these hues precisely. ----

    /** Overlay floating composer's pill AND response card fill — one shared token (#181: both
     * surfaces sampled to the exact same hex across four captures). Measured `#020B2B`; supersedes
     * the Rev E frame estimate `#10182E` (lighter/more saturated than reality). */
    val surfaceOverlayPill = Color(0xFF020B2B)

    /** Overlay composer's filled mic circle (spec §6.2 C5): light-blue fill, dark glyph. Measured
     * `#709FF8` (#181, pixel-sampled center of the real filled circle); supersedes the Rev E
     * estimate `#A9C7FF` (notably lighter/whiter than reality). */
    val accentOverlayMic = Color(0xFF709FF8)

    /** Glyph color atop [accentOverlayMic] — same value as [surfaceCanvas], named for the pairing. */
    val onAccentOverlayMic = surfaceCanvas

    /** §6.9 toast/notice fill. Also [AuraLegacyColorScheme]'s `primaryContainer` stand-in. */
    val surfaceNotice = Color(0xFF182136)

    // ---- §3.3 Aurora (liveness) gradient set — non-interactive, decorative only ----
    // Data only: stop lists + geometry hints. Rendering (linear/radial/edge-sweep composition,
    // hue blending, dither, waviness) is owned by ui/aurora/, not this file.

    /** Landing/generation top wash: gold↔green top blend, fading to fully transparent (revealing
     * [surfaceCanvas]) by roughly this fraction of screen height. Gitea #181: real captures show
     * the wash still visibly fading past 40% of screen height (a prior 0.35 estimate cut it short). */
    const val auroraWashTopFadeHeightFraction = 0.42f

    /** Gitea #181, four real-device captures (pixel-sampled — supersedes the Rev C "purple↔amber"
     * eyeballed estimate, which no capture ever showed): gold top-left fading toward green
     * top-right, both measured with smooth monotonic falloff and zero banding — used for BOTH the
     * resting landing state and the active generation wash (see [AuroraState.Resting]). */
    val auroraWashTop: List<GradientStop> = listOf(
        GradientStop(Color(0xFFCBC541), 0f), // gold, top-left — measured peak
        GradientStop(Color(0xFF4E7120), 1f), // green, top-right — measured
    )

    /** Vertical exponential decay length for [auroraOverlayBloom]'s falloff at its WIDEST
     * (idle-listening). BACK-SOLVED from the ref2 edge-column scanline, not eyeballed: the sample
     * still reads ~36% of peak glow at 143dp above the bottom edge (#424856 vs #6D85B9 over the
     * #2A2A2E scrim) → L = -143/ln(0.358) ≈ 139dp. The first cut set 45dp by taking "visible to
     * ~143dp" as the decay length directly — an exponential with L=45dp is at 4% by 143dp, which
     * (behind the overlay's opaque bottom chrome) rendered the whole effect invisible: the
     * device-gate "aurora renders black" finding. Streaming/thinking contracts to a fraction of
     * this (see `EdgeGlowUniformMath.THINKING_DECAY_FRACTION`, ui/aurora — a behavioral tuning
     * ratio, not a second measured token) to hug the pill with no corner reach. */
    val auroraOverlayBloomDecayDepth = 139.dp

    /**
     * Bottom bloom, blue family only (Gitea #181, four real-device captures — zero
     * green/clay/violet in any scanline). Blended by the bloom's own intensity in `ui/aurora/`
     * (bright→lighter stop, faint→darker stop) rather than by angle — this also removes the
     * periodic wrong-dominant-hue bug the old angular wheel had.
     *
     * VALUES are user-directed dark-blend tuning (2026-07-03), not the raw capture samples: the
     * scanline-sampled stops (#6D85B9/#4562A0) were composited over the overlay SCRIM and read
     * far too bright over the app's pure-black canvas and under foreground content. The directive:
     * shades very close to the black background so the glow blends smoothly and never competes
     * with content on top — near-black navies, the effect reads as atmosphere, not a light source.
     */
    val auroraOverlayBloom: List<GradientStop> = listOf(
        GradientStop(Color(0xFF33436E), 0f), // brightest stop — dark slate blue, still near-black
        GradientStop(Color(0xFF131C36), 1f), // fade stop — near-black navy, melts into the canvas
    )

    /** `#000000` at 40% (spec range 35-45%, midpoint) + slight desaturation of the app underneath
     * (rendering detail, not tokenized here). Gitea #181 measured the composited result at
     * `#2A2A2E` over Chrome's dark surface but flagged the backdrop as uncontrolled/unverified —
     * kept as-is pending a controlled-backdrop re-measure (see the issue's honest caveat). */
    val scrimOverlay = Color(0x66000000)
}

/**
 * Orb v2 shader palette — the pre-existing brand clay-flower mark palette (`ui/orb/`), unrelated
 * to the design-v2 token set in [AuraColors]. Unchanged by this task; kept exactly as before (same
 * hex values) so [AssistantExtras.orbPalette] keeps compiling for `ui/orb` (outside this task's
 * file ownership).
 */
internal object AuraOrbColors {
    private val clayDeep = Color(0xFF7A3A22)
    private val clayCore = Color(0xFFC15F3C)
    private val clayBright = Color(0xFFE8916A)
    private val clayGold = Color(0xFFE3B36B)
    private val thinkingSlate = Color(0xFF5B6470)
    private val thinkingMist = Color(0xFF9AA3AE)
    private val thinkingFrost = Color(0xFFC7CDD4)
    private val errorRedContainer = Color(0xFF5C1A1A)
    private val errorRed = Color(0xFFFF6B6B)
    private val errorRedBright = Color(0xFFFFB4AB)

    val idle = listOf(clayDeep, clayCore, clayBright)
    val listening = listOf(clayCore, clayBright, clayGold)
    val thinking = listOf(thinkingSlate, thinkingMist, thinkingFrost)
    val error = listOf(errorRedContainer, errorRed, errorRedBright)
}

/**
 * M3 [ColorScheme] bridge for pre-migration call sites outside `ui/theme/` that still read
 * `MaterialTheme.colorScheme.*` directly (`ui/chat`, `ui/sessions`, `ui/overlay` — outside this
 * task's file ownership, brief W1-A). Slots with a direct spec analog map 1:1 to [AuraColors].
 * `errorContainer`/`onErrorContainer`/`tertiaryContainer`/`onTertiaryContainer` and the
 * `surfaceContainer*` ladder have no spec analog at all — rather than invent non-spec hexes for a
 * shim slated for deletion, those fall through to `darkColorScheme()`'s own M3 defaults; only
 * `primaryContainer` gets an explicit stand-in ([AuraColors.surfaceNotice], the spec §6.9 toast
 * fill — the closest documented navy). Simplify once those surfaces read [AuraColors] tokens
 * directly (tracked: W2-A, W2-B, W3).
 */
internal val AuraLegacyColorScheme: ColorScheme = darkColorScheme(
    background = AuraColors.surfaceCanvas,
    onBackground = AuraColors.textPrimary,
    surface = AuraColors.surfaceCanvas,
    onSurface = AuraColors.textPrimary,
    onSurfaceVariant = AuraColors.textSecondary,
    outline = AuraColors.outlineHairline,
    primary = AuraColors.accentPrimary,
    onPrimary = AuraColors.accentOnAccent,
    primaryContainer = AuraColors.surfaceNotice, // stand-in: spec §6.9 toast fill, the closest documented navy
    onPrimaryContainer = AuraColors.textPrimary,
    error = AuraColors.accentError,
    onError = AuraColors.accentOnAccent,
    surfaceContainerLow = AuraColors.surfaceInput,
    surfaceContainer = AuraColors.surfaceInput,
    surfaceContainerHigh = AuraColors.surfaceSelected,
)
