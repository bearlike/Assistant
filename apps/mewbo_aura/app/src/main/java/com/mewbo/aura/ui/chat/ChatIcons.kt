package com.mewbo.aura.ui.chat

import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.VectorGlyphFill

/**
 * Hand-rolled vectors for glyphs that aren't in the app's curated `material-icons-core` set
 * (composer: mic, stop, waveform, stop-tile, up-arrow; chat chrome: two-line menu, content-copy,
 * volume-up) - `material-icons-extended` isn't in the dependency catalog and this task may not
 * edit build files (apps/mewbo_aura/CLAUDE.md). Path data mirrors the standard Material glyphs at
 * 24dp where one exists (StopTile/TwoLineMenu/ContentCopy have no Material analog - see their own
 * docs); fill/stroke is [VectorGlyphFill] (ui/theme/Color.kt) so `Icon`'s default `tint =
 * LocalContentColor.current` paints these paths - no raw `Color` reference here (ui/CLAUDE.md
 * token discipline). ChatIcons.Stop is also reused, unmodified, as the C4 trailing-circle glyph
 * in ui/composer (out of this task's ownership - do not rename/remove without checking that lane).
 */
object ChatIcons {

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

    /** Composer options sheet's "Photos" pill leading glyph (Gitea #177 W2) - no Material analog
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

    /** Composer options sheet's "Files" pill leading glyph (Gitea #177 W2) - simplified
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
}
