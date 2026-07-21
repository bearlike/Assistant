package com.mewbo.aura.ui.chat

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.VectorGlyphFill

/**
 * FROZEN LEGACY hand-rolled glyph set. These vectors predate `material-icons-extended`, which was
 * added to the dependency catalog on 2026-07-14 (apps/mewbo_aura/CLAUDE.md § Iconography). They cover
 * glyphs the app's original `material-icons-core`-only floor lacked (composer: mic, stop, waveform,
 * stop-tile, up-arrow; chat chrome: two-line menu, content-copy, volume-up; tool cards: clock) plus a
 * few with no Material analog at all (StopTile/TwoLineMenu/ContentCopy - see their own docs). The set
 * is FROZEN: existing reuses stay (a reused hand-rolled glyph is not a "new hand-roll"), but NEW
 * glyphs pull from `material-icons-extended` first - do not add a hand-rolled path here. Path data
 * mirrors the standard Material glyphs at 24dp where one exists; fill/stroke is [VectorGlyphFill]
 * (ui/theme/Color.kt) so `Icon`'s default `tint = LocalContentColor.current` paints these paths - no
 * raw `Color` reference here (ui/CLAUDE.md token discipline). ChatIcons.Stop is also reused,
 * unmodified, as the C4 trailing-circle glyph in ui/composer (do not rename/remove without checking
 * that lane).
 */
object ChatIcons {

    // --- Scope glyphs: off-the-shelf material-icons-extended aliases (NOT hand-rolled) ---
    // Composer scope row + project/tool pickers (user directive 2026-07-14). One-line library
    // references, centralized so every scope glyph choice lives in one place. These are NOT part of
    // this file's legacy hand-rolled path set (frozen — new glyphs pull from material-icons-extended,
    // added to the dependency catalog 2026-07-14). Filled weight, matching every other icon surface.

    /** The "project / workspace" scope prefix (composer scope row + real project rows) — a folder.
     * Tinted [com.mewbo.aura.ui.theme.AuraColors.scopeProject]. */
    val ProjectScope: ImageVector get() = Icons.Filled.Folder

    /** The ephemeral "Temporary" project's DISTINCT prefix (project pickers) — a clock (schedule),
     * marking the throwaway temp-dir cwd apart from real, saved projects (paired with a divider below
     * it, §F). Reads "time-bound / transient" without the destructive-action ambiguity a trash glyph
     * would carry next to a selectable row. */
    val TemporaryProjectScope: ImageVector get() = Icons.Filled.Schedule

    /** The "tools" scope prefix (composer scope row) — a wrench (`build`). Tinted
     * [com.mewbo.aura.ui.theme.AuraColors.scopeTool]. */
    val ToolScope: ImageVector get() = Icons.Filled.Build

    val Mic: ImageVector by lazy {
        ImageVector.Builder(name = "Mic", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(12f, 14f)
                curveToRelative(1.66f, 0f, 2.99f, -1.34f, 2.99f, -3f)
                lineTo(15f, 5f)
                curveToRelative(0f, -1.66f, -1.34f, -3f, -3f, -3f)
                reflectiveCurveTo(9f, 3.34f, 9f, 5f)
                verticalLineToRelative(6f)
                curveToRelative(0f, 1.66f, 1.34f, 3f, 3f, 3f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(17f, 11f)
                curveToRelative(0f, 2.76f, -2.24f, 5f, -5f, 5f)
                reflectiveCurveToRelative(-5f, -2.24f, -5f, -5f)
                horizontalLineTo(5f)
                curveToRelative(0f, 3.53f, 2.61f, 6.43f, 6f, 6.92f)
                verticalLineTo(21f)
                horizontalLineToRelative(2f)
                verticalLineToRelative(-3.08f)
                curveToRelative(3.39f, -0.49f, 6f, -3.39f, 6f, -6.92f)
                horizontalLineToRelative(-2f)
                close()
            }
            .build()
    }

    val Stop: ImageVector by lazy {
        ImageVector.Builder(name = "Stop", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(6f, 6f)
                horizontalLineToRelative(12f)
                verticalLineToRelative(12f)
                horizontalLineToRelative(-12f)
                close()
            }
            .build()
    }

    /** Composer C1 idle trailing-circle glyph (spec §6.2: "accentMuted circle with waveform glyph
     * (Live)"). Five bars of varying height, mirroring Material "graphic_eq". */
    val Waveform: ImageVector by lazy {
        ImageVector.Builder(name = "Waveform", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(7f, 18f)
                horizontalLineToRelative(2f)
                verticalLineTo(6f)
                horizontalLineTo(7f)
                verticalLineTo(18f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(3f, 14f)
                horizontalLineToRelative(2f)
                verticalLineToRelative(-4f)
                horizontalLineTo(3f)
                verticalLineTo(14f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(11f, 22f)
                horizontalLineToRelative(2f)
                verticalLineTo(2f)
                horizontalLineToRelative(-2f)
                verticalLineTo(22f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(15f, 18f)
                horizontalLineToRelative(2f)
                verticalLineTo(6f)
                horizontalLineToRelative(-2f)
                verticalLineTo(18f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(19f, 14f)
                horizontalLineToRelative(2f)
                verticalLineToRelative(-4f)
                horizontalLineToRelative(-2f)
                verticalLineTo(14f)
                close()
            }
            .build()
    }

    /** Composer C2/C3/C4-with-draft trailing-circle glyph (spec §6.2: "↑ send arrow"), mirroring
     * Material "arrow_upward". */
    val UpArrow: ImageVector by lazy {
        ImageVector.Builder(name = "UpArrow", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(4f, 12f)
                lineToRelative(1.41f, 1.41f)
                lineTo(11f, 7.83f)
                verticalLineTo(20f)
                horizontalLineToRelative(2f)
                verticalLineTo(7.83f)
                lineToRelative(5.59f, 5.58f)
                lineTo(20f, 12f)
                lineToRelative(-8f, -8f)
                close()
            }
            .build()
    }

    /** Composer C3 dictation's separate stop button (Rev E §E-3: "outlined rounded-square stop
     * tile" - amends the original filled reading). Stroked, not filled, so the glyph itself reads
     * as an outline with no separate colored container needed at the call site; [Stop] (plain
     * filled square) stays the C4 trailing-circle glyph, keeping the two visually distinct. No
     * Material analog; hand-drawn via quadratic corners. */
    val StopTile: ImageVector by lazy {
        ImageVector.Builder(name = "StopTile", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
                moveTo(9f, 6f)
                horizontalLineTo(15f)
                quadTo(18f, 6f, 18f, 9f)
                verticalLineTo(15f)
                quadTo(18f, 18f, 15f, 18f)
                horizontalLineTo(9f)
                quadTo(6f, 18f, 6f, 15f)
                verticalLineTo(9f)
                quadTo(6f, 6f, 9f, 6f)
                close()
            }
            .build()
    }

    /** Top bar's drawer trigger (spec §6.1: "two-line hamburger" - deliberately not Material's
     * 3-line "menu" glyph). Two plain bars, no Material analog. */
    val TwoLineMenu: ImageVector by lazy {
        ImageVector.Builder(name = "TwoLineMenu", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(3f, 7f)
                horizontalLineToRelative(18f)
                verticalLineToRelative(2f)
                horizontalLineToRelative(-18f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(3f, 15f)
                horizontalLineToRelative(18f)
                verticalLineToRelative(2f)
                horizontalLineToRelative(-18f)
                close()
            }
            .build()
    }

    /** Action row's copy glyph (spec §6.5 ⧉) and the top bar's "Copy conversation" menu item (Rev
     * E-5). Two overlapping stroked squares - simplified (sharp corners) from Material
     * "content_copy", same house style as [StopTile]. */
    val ContentCopy: ImageVector by lazy {
        ImageVector.Builder(name = "ContentCopy", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
                moveTo(3f, 3f)
                horizontalLineToRelative(12f)
                verticalLineToRelative(12f)
                horizontalLineToRelative(-12f)
                close()
            }
            .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
                moveTo(9f, 9f)
                horizontalLineToRelative(12f)
                verticalLineToRelative(12f)
                horizontalLineToRelative(-12f)
                close()
            }
            .build()
    }

    /** Composer options sheet's "Photos" pill leading glyph - no Material analog
     * available at this task's dependency floor, so a simplified mountain-in-frame silhouette, same
     * stroked house style as [StopTile]/[ContentCopy]. */
    val PhotoGlyph: ImageVector by lazy {
        ImageVector.Builder(name = "PhotoGlyph", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
                moveTo(4f, 5f)
                horizontalLineTo(20f)
                verticalLineTo(19f)
                horizontalLineTo(4f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(8.5f, 10f)
                curveToRelative(0.83f, 0f, 1.5f, -0.67f, 1.5f, -1.5f)
                reflectiveCurveToRelative(-0.67f, -1.5f, -1.5f, -1.5f)
                reflectiveCurveToRelative(-1.5f, 0.67f, -1.5f, 1.5f)
                reflectiveCurveToRelative(0.67f, 1.5f, 1.5f, 1.5f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(6f, 17f)
                lineTo(10f, 12f)
                lineTo(13f, 15.5f)
                lineTo(15f, 13f)
                lineTo(18f, 17f)
                close()
            }
            .build()
    }

    /** Composer options sheet's "Files" pill leading glyph - simplified
     * document-with-folded-corner outline, same stroked house style as [PhotoGlyph]. */
    val FileGlyph: ImageVector by lazy {
        ImageVector.Builder(name = "FileGlyph", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
                moveTo(6f, 3f)
                horizontalLineTo(14f)
                lineTo(19f, 8f)
                verticalLineTo(21f)
                horizontalLineTo(6f)
                close()
            }
            .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
                moveTo(14f, 3f)
                verticalLineTo(8f)
                horizontalLineTo(19f)
            }
            .build()
    }

    /**
     * The alarm action card's header glyph (`ui/chat/toolcards/AlarmToolCard`), mirroring Material
     * "schedule" (clock face + hands) - `material-icons-core` carries no clock or alarm glyph at
     * all, so there was nothing to reuse.
     *
     * The face is ONE path with two subpaths wound in OPPOSITE directions (outer circle
     * counter-clockwise, inner clockwise) - that's what punches the hole out under the default
     * nonzero fill rule, exactly as Material's own path data does it. Splitting them into two
     * `.path {}` blocks (the house style everywhere else in this file) would fill the inner circle
     * back in and render a solid disc.
     */
    val Clock: ImageVector by lazy {
        ImageVector.Builder(name = "Clock", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(11.99f, 2f)
                curveTo(6.47f, 2f, 2f, 6.48f, 2f, 12f)
                reflectiveCurveToRelative(4.47f, 10f, 9.99f, 10f)
                curveTo(17.52f, 22f, 22f, 17.52f, 22f, 12f)
                reflectiveCurveTo(17.52f, 2f, 11.99f, 2f)
                close()
                moveTo(12f, 20f)
                curveToRelative(-4.42f, 0f, -8f, -3.58f, -8f, -8f)
                reflectiveCurveToRelative(3.58f, -8f, 8f, -8f)
                reflectiveCurveToRelative(8f, 3.58f, 8f, 8f)
                reflectiveCurveToRelative(-3.58f, 8f, -8f, 8f)
                close()
                moveTo(12.5f, 7f)
                horizontalLineTo(11f)
                verticalLineToRelative(6f)
                lineToRelative(5.25f, 3.15f)
                lineToRelative(0.75f, -1.23f)
                lineToRelative(-4.5f, -2.67f)
                close()
            }
            .build()
    }

    /** Action row's pinned read-aloud glyph (spec §6.5 🔊), mirroring Material "volume_up"'s
     * speaker-cone subpath verbatim plus one (of its two) sound-wave arcs - simplified to one arc,
     * legible at 24dp without over-detailing. */
    val VolumeUp: ImageVector by lazy {
        ImageVector.Builder(name = "VolumeUp", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(3f, 9f)
                verticalLineToRelative(6f)
                horizontalLineToRelative(4f)
                lineToRelative(5f, 5f)
                verticalLineTo(4f)
                lineTo(7f, 9f)
                horizontalLineTo(3f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(16.5f, 12f)
                curveToRelative(0f, -1.77f, -1.02f, -3.29f, -2.5f, -4.03f)
                verticalLineToRelative(8.05f)
                curveToRelative(1.48f, -0.73f, 2.5f, -2.25f, 2.5f, -4.02f)
                close()
            }
            .build()
    }

    /** The widget card's "enter full screen" affordance glyph, mirroring Material
     * "fullscreen" - four corner brackets. Predates `material-icons-extended` (added 2026-07-14); a
     * legacy hand-roll kept as-is under the frozen-set rule above, not evidence extended is
     * unavailable. Four disjoint corners, one `.path {}` each (house style). */
    val Fullscreen: ImageVector by lazy {
        ImageVector.Builder(name = "Fullscreen", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(7f, 14f)
                horizontalLineTo(5f)
                verticalLineToRelative(5f)
                horizontalLineToRelative(5f)
                verticalLineToRelative(-2f)
                horizontalLineTo(7f)
                verticalLineToRelative(-3f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(5f, 10f)
                horizontalLineToRelative(2f)
                verticalLineTo(7f)
                horizontalLineToRelative(3f)
                verticalLineTo(5f)
                horizontalLineTo(5f)
                verticalLineToRelative(5f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(17f, 17f)
                horizontalLineToRelative(-3f)
                verticalLineToRelative(2f)
                horizontalLineToRelative(5f)
                verticalLineToRelative(-5f)
                horizontalLineToRelative(-2f)
                verticalLineToRelative(3f)
                close()
            }
            .path(fill = SolidColor(VectorGlyphFill)) {
                moveTo(14f, 5f)
                verticalLineToRelative(2f)
                horizontalLineToRelative(3f)
                verticalLineToRelative(3f)
                horizontalLineToRelative(2f)
                verticalLineTo(5f)
                horizontalLineToRelative(-5f)
                close()
            }
            .build()
    }
}
