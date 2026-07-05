package com.mewbo.aura.ui.theme

import androidx.compose.material3.Typography as M3Typography
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.mewbo.aura.R

/**
 * Figtree (variable, OFL-licensed) — the v1 substitute for a license-restricted geometric-humanist
 * family (spec §4), one family app-wide. Two static entries pin the variable font's `wght` axis:
 * Compose matches a requested [FontWeight] to the closest registered [Font] in the family, it
 * doesn't interpolate the axis live, and the spec only ever uses 400/500 (titleBar's two-tone
 * product/variant split is the one place 500 appears) — no other axis pin is needed.
 */
@OptIn(ExperimentalTextApi::class)
private val FigtreeFamily = FontFamily(
    Font(
        R.font.figtree,
        weight = FontWeight.Normal,
        variationSettings = FontVariation.Settings(FontVariation.weight(400)),
    ),
    Font(
        R.font.figtree,
        weight = FontWeight.Medium,
        variationSettings = FontVariation.Settings(FontVariation.weight(500)),
    ),
    Font(
        R.font.figtree_italic,
        weight = FontWeight.Normal,
        style = FontStyle.Italic,
        variationSettings = FontVariation.Settings(FontVariation.weight(400)),
    ),
)

/**
 * Design-v2 type scale (spec §4) — token names as printed in the spec doc, since the M3
 * [Typography] slot names (titleLarge, bodyLarge, ...) don't correspond 1:1. Call sites read e.g.
 * `AuraType.bodyMessage`. Default tracking everywhere, no all-caps (spec §4 closing line).
 */
object AuraType {
    val greetingDisplay = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 32.sp,
        lineHeight = 40.sp,
    )

    /**
     * Base style for the title bar's product word ("Aura", spec: 500 weight, `textPrimary`). The
     * muted variant word (backend model short name, Rev D §D-4, else "Core") is composed
     * downstream as a second `Text` with `.copy(fontWeight = FontWeight.Normal, color =
     * AuraColors.textSecondary)` — one style, two-tone composition per spec §4 titleBar row.
     */
    val titleBar = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Medium,
        fontSize = 22.sp,
        lineHeight = 28.sp,
        color = AuraColors.textPrimary,
    )
    /** Drawer wordmark (spec §6.7: "24sp") — same family/weight as [titleBar]'s product word, just larger. */
    val wordmark = titleBar.copy(fontSize = 24.sp)

    /**
     * Compact scale (user directive 2026-07-04): 16sp / 23sp — supersedes the Rev F/181 measured
     * value of 18sp / 26sp (itself the product of a round-3 multi-line re-measurement, 14 line
     * transitions, ink+leading = 26.0dp exact, against the reference app). The user found the
     * resulting type too large with no hierarchy and asked for a smaller, denser scale; do not
     * chase reference parity back up.
     *
     * Breathability retune, user directive 2026-07-04 round 2: lineHeight 23sp → 24sp (16sp size
     * stays) — line height a "tiny tad bit more spacious", font sizes are right and untouched.
     */
    val bodyMessage = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 16.sp,
        lineHeight = 24.sp,
    )

    /**
     * Compact scale (user directive 2026-07-04): 16sp / 22sp — supersedes the Rev F/181 measured
     * value of 18sp / 24sp, part of the same across-the-board type-scale reduction as
     * [bodyMessage]. Do not chase reference parity back up.
     *
     * Breathability retune, user directive 2026-07-04 round 2: lineHeight 22sp → 23sp (16sp size
     * stays) — line height a "tiny tad bit more spacious", font sizes are right and untouched.
     */
    val listItem = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 16.sp,
        lineHeight = 23.sp,
    )

    /**
     * Markdown heading ramp for assistant responses — compact scale (user directive 2026-07-04):
     * this ramp is the reference-app-measured ramp (2026-07-03 GMS redroid cap-height parity
     * capture, 28/24/20sp) scaled down one step per the compact-scale directive, landing at
     * 24/20/18sp. That measured capture is no longer the governing source for the absolute
     * values — do not chase reference parity back up. H2/H3 still carry Medium (the measured
     * stem-width optical compensation carries over unchanged); H1 stays Normal. There is
     * deliberately NO markdownH4+: h4..h6 still reuse [bodyMessage] verbatim, a heading by
     * placement only.
     */
    val markdownH1 = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 24.sp,
        lineHeight = 32.sp,
    )
    val markdownH2 = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Medium,
        fontSize = 20.sp,
        lineHeight = 28.sp,
    )
    val markdownH3 = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Medium,
        fontSize = 18.sp,
        lineHeight = 26.sp,
    )

    /**
     * Compact scale (user directive 2026-07-04): 14sp / 18sp — supersedes the previous 16sp /
     * 20sp value as part of the across-the-board type-scale reduction. Do not chase reference
     * parity back up.
     */
    val sectionHeader = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        lineHeight = 18.sp,
        color = AuraColors.textSecondary,
    )

    /**
     * Compact scale (user directive 2026-07-04): 14sp — supersedes the previous 16sp value as
     * part of the across-the-board type-scale reduction. Do not chase reference parity back up.
     */
    val metaTrailing = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        color = AuraColors.textSecondary,
    )

    /**
     * Compact scale (user directive 2026-07-04): 12sp / 16sp — supersedes the previous 14sp /
     * 18sp value as part of the across-the-board type-scale reduction. Do not chase reference
     * parity back up.
     *
     * Breathability retune, user directive 2026-07-04 round 2: lineHeight 16sp → 17sp (12sp size
     * stays) — line height a "tiny tad bit more spacious", font sizes are right and untouched.
     */
    val caption = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 12.sp,
        lineHeight = 17.sp,
        color = AuraColors.textSecondary,
    )

    /** Spec §6.11: activity chip labels default to `textSecondary`; other chip-family uses (e.g.
     * §6.2 C5's context chip) may override via `Text(style = AuraType.chipLabel, color = ...)`.
     *
     * Compact scale (user directive 2026-07-04): 13sp — supersedes the spec §4 value of 15sp as
     * part of the across-the-board type-scale reduction. Do not chase reference parity back up. */
    val chipLabel = TextStyle(
        fontFamily = FigtreeFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 13.sp,
        color = AuraColors.textSecondary,
    )

    /** M3 baseline sizes, Figtree'd — material3 1.4.0's `Typography` has no `defaultFontFamily`
     * convenience constructor, so every uncurated slot is rebuilt from the M3 default explicitly. */
    private val baseline = M3Typography()
    private fun TextStyle.withFigtree() = copy(fontFamily = FigtreeFamily)

    /**
     * M3 `Typography` bridge that [AuraTheme] hands to [androidx.compose.material3.MaterialTheme]
     * — required plumbing (`MaterialTheme` always needs one), not a migration shim. Every slot
     * carries Figtree so "one family app-wide" (spec §4) holds even for M3 slots this doesn't
     * explicitly curate; the three curated slots map onto the closest spec token for pre-migration
     * call sites in `ui/chat`/`ui/sessions`/`ui/settings` that still read `MaterialTheme.typography.*`
     * directly.
     */
    val Typography = M3Typography(
        displayLarge = baseline.displayLarge.withFigtree(),
        displayMedium = baseline.displayMedium.withFigtree(),
        displaySmall = baseline.displaySmall.withFigtree(),
        headlineLarge = baseline.headlineLarge.withFigtree(),
        headlineMedium = baseline.headlineMedium.withFigtree(),
        headlineSmall = baseline.headlineSmall.withFigtree(),
        titleLarge = titleBar,
        titleMedium = baseline.titleMedium.withFigtree(),
        titleSmall = baseline.titleSmall.withFigtree(),
        bodyLarge = bodyMessage,
        bodyMedium = caption,
        bodySmall = baseline.bodySmall.withFigtree(),
        labelLarge = baseline.labelLarge.withFigtree(),
        labelMedium = baseline.labelMedium.withFigtree(),
        labelSmall = baseline.labelSmall.withFigtree(),
    )
}
