package com.mewbo.aura.ui.theme

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import com.mewbo.aura.ui.orb.OrbState

/**
 * Cross-cutting design tokens that don't fit [AuraColors] / [AuraShape] / [AuraType] /
 * [AuraSpacing]: orb palettes per state and the assist-overlay scrim. Provided once by [AuraTheme]
 * — never construct these ad hoc at call sites.
 *
 * `glassSheet`/`GlassSheetStyle` (spec §2: "No glass/blur" — the reference design language uses
 * opaque true-black surfaces + dim scrim) were removed here in the W3 overlay-invocation-v2 task,
 * the moment its sole consumer (`ui/overlay/AssistOverlayScreen.kt`'s Haze glass sheet) was deleted
 * — this file's own prior revision named that exact trigger condition. The `dev.chrisbanes.haze`
 * Gradle dependency is gone too (kept removed, not just unused, per the same spec line).
 */
data class AssistantExtras(
    val orbPalette: (OrbState) -> List<Color>,
    val scrimColor: Color,
    val reducedMotion: Boolean,
)

val LocalAssistantExtras = staticCompositionLocalOf<AssistantExtras> {
    error("AssistantExtras not provided — wrap content in AuraTheme")
}
