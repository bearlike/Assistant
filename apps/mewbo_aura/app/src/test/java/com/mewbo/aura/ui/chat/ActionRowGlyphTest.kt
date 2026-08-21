package com.mewbo.aura.ui.chat

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.graphics.vector.VectorGroup
import androidx.compose.ui.graphics.vector.VectorPath
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.theme.AuraTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The action row's five glyphs, as INK rather than as presence.
 *
 * User report: "I do not see any speaker button consistently being shown at the footer of each
 * response." The button was present, wired, and inside the same [AssistantMessageRow] visibility
 * gate as its four neighbours the whole time — so every presence-shaped assertion was green while
 * the user was right. What was wrong was how much of its cell the glyph actually painted.
 *
 * `ChatIcons.VolumeUp` was a hand-rolled path carrying Material's speaker cone plus ONE of the two
 * sound-wave arcs. Measured against the 24-unit viewport every glyph in this row shares:
 *
 * ```
 * hand-rolled VolumeUp   ink x=[3.0,16.5]  w=13.5  inkCentre=9.75
 * ContentCopy            ink x=[3.0,21.0]  w=18.0  inkCentre=12.0
 * Filled.ThumbUp         ink x=[1.0,23.0]  w=22.0  inkCentre=12.0
 * Filled.MoreVert        ink x=[10.0,14.0] w= 4.0  inkCentre=12.0
 * ```
 *
 * Two defects in one path, and they compound. It painted three quarters of the copy glyph's width,
 * and — because the dropped arc was the OUTER one — what remained sat 2.25 units left of centre
 * while all four neighbours centred on 12. At `ActionRow.iconSize` (20dp, cut from 24dp by the
 * compact-scale directive in ui/theme/Spacing.kt, after that path's "legible at 24dp" note was
 * written) a glyph that is both smaller and off-centre, alone across a weighted spacer at 80%
 * opacity, reads as an absent control rather than a lighter one.
 *
 * So the law here is comparative, never a dp threshold: the trailing glyph must paint its cell the
 * way the leading cluster does. A threshold would need retuning on every icon-scale change; this
 * survives one, and it is the property the user was actually reporting.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ActionRowGlyphTest {

    @get:Rule
    val rule = createComposeRule()

    @Test
    fun `every action-row glyph centres its ink in the shared viewport`() {
        // Stated over the WHOLE row, not just the glyph that broke: a centring law that only ever
        // looks at one member is a law about that member. MoreVert is deliberately in here — it is
        // 4 units of ink and perfectly centred, which is exactly why "narrow" and "off-centre" have
        // to be separate assertions rather than one combined judgement of weight.
        for ((name, glyph) in rowGlyphs()) {
            val ink = inkOf(glyph)
            assertEquals(
                "$name paints its ink off-centre: centre=${ink.center.x}, viewport centre=${glyph.viewportWidth / 2f}",
                glyph.viewportWidth / 2f,
                ink.center.x,
                CentreTolerance,
            )
        }
    }

    @Test
    fun `the read-aloud glyph paints as much of its cell as the copy glyph beside it`() {
        val readAloud = inkOf(ChatIcons.VolumeUp)
        val copy = inkOf(ChatIcons.ContentCopy)

        // CONTROL, and it is what gives the comparison below any power: two glyphs that both
        // measured zero would satisfy a `>=` between them perfectly. 10 units sits far above the
        // degenerate case and far below the 18 a real solid glyph paints here.
        assertTrue(
            "the copy glyph measured ${copy.width} units — too little ink to be a real glyph, so " +
                "the comparison below cannot fail",
            copy.width > MinRealInkUnits,
        )
        assertTrue(
            "the read-aloud glyph paints ${readAloud.width} units of ink against the copy glyph's " +
                "${copy.width}; it reads as an absent control, not a lighter one",
            readAloud.width >= copy.width - InkTolerance,
        )
    }

    /**
     * The refuted hypothesis, kept as a standing assertion so nobody re-chases it.
     *
     * The row is one `Row` holding a 4x48dp unweighted cluster, a weighted spacer, and the 48dp
     * read-aloud cell — so the trailing button is structurally the only child that can be pushed
     * out, which makes "it overflowed" the obvious diagnosis. It is not what happened. Measured,
     * the button is fully laid out down to a 288dp-wide surface and only degenerates below 240dp;
     * the narrowest Android phone is 320dp, asserted here. Font scale cannot move any of it either:
     * these cells are `Modifier.size(48.dp)`, dp-fixed, and `minimumInteractiveComponentSize()` is
     * deliberately not used (AssistantMessageRow's own note) — bounds at scale 2.0 came back
     * byte-identical to scale 1.0.
     *
     * This passed before the glyph fix and passes after. That is the point of it: it is the control
     * that says the defect was never in the layout.
     */
    @Test
    fun `all five controls are displayed on the narrowest phone at double font scale`() {
        rule.setContent {
            AuraTheme(reducedMotion = true) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    Box(modifier = Modifier.width(NarrowestPhoneWidth)) {
                        AssistantMessageRow(
                            item = ChatItem.AssistantMessage(
                                text = REPLY_TEXT,
                                isStreaming = false,
                                ts = TS,
                                key = "a1",
                            ),
                            showActionRow = true,
                            isSpeaking = false,
                            onNotice = {},
                            onReadAloudToggle = {},
                        )
                    }
                }
            }
        }

        // assertIsDisplayed, never assertExists: the failure this guards against is a node that is
        // in the tree and laid out past the trailing edge, which `assertExists` cannot see.
        for (description in listOf("Good response", "Bad response", "Copy", "More", "Read aloud")) {
            rule.onNodeWithContentDescription(description, useUnmergedTree = true).assertIsDisplayed()
        }
    }

    /**
     * Union of every path's bounds, in the vector's own viewport units.
     *
     * Bounds come from the platform `Path`, so a curve contributes its control points rather than a
     * tight hull — consistently for every glyph, which is all a comparison between two of them
     * needs.
     */
    private fun inkOf(vector: ImageVector): Rect {
        var union: Rect? = null
        fun walk(group: VectorGroup) {
            for (node in group) {
                when (node) {
                    is VectorPath -> {
                        val bounds = PathParser().addPathNodes(node.pathData).toPath().getBounds()
                        union = union?.expandToInclude(bounds) ?: bounds
                    }
                    is VectorGroup -> walk(node)
                }
            }
        }
        walk(vector.root)
        return requireNonNull(union)
    }

    private fun Rect.expandToInclude(other: Rect) = Rect(
        left = minOf(left, other.left),
        top = minOf(top, other.top),
        right = maxOf(right, other.right),
        bottom = maxOf(bottom, other.bottom),
    )

    private fun requireNonNull(rect: Rect?): Rect = requireNotNull(rect) { "the vector carried no paths at all" }

    private fun rowGlyphs(): List<Pair<String, ImageVector>> = listOf(
        "ThumbUp" to Icons.Filled.ThumbUp,
        "ContentCopy" to ChatIcons.ContentCopy,
        "MoreVert" to Icons.Filled.MoreVert,
        "VolumeUp" to ChatIcons.VolumeUp,
    )

    private companion object {
        /** Narrowest `smallestScreenWidthDp` any Android phone ships; the row needs 288dp. */
        val NarrowestPhoneWidth = 320.dp

        const val REPLY_TEXT = "It is clear."
        const val TS = "2026-01-01T00:00:00+00:00"

        /** Sub-unit, in a 24-unit viewport — a real off-centre defect displaced the ink by 2.25. */
        const val CentreTolerance = 0.05f
        const val InkTolerance = 0.5f

        /** Above any degenerate render, below the ~18 units a solid glyph paints in this row. */
        const val MinRealInkUnits = 10f
    }
}
