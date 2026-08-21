package com.mewbo.aura.ui.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Lock
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.ui.theme.AuraTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * What a permission row actually puts on screen.
 *
 * [SettingsStatusTest] pins the badge VALUES - which tone each state folds to, and that every tone
 * but `Value` owns a glyph. It cannot see whether the word ever reaches a pixel, and the
 * accessibility law is about the render: a state readout is a glyph AND a word AND a tint, so a row
 * that degenerated to tint alone would keep every existing assertion green while going invisible to
 * a colour-blind reader.
 *
 * The layout half is the settings header, which rendered "Connection and identity" one character
 * per line behind a long failure reason: a `Row` measures its unweighted children first, so a badge
 * sharing the line claimed the width it wanted and squeezed the weighted title toward zero. The fix
 * stacked the badge under the title, and the assertion for it is relative rather than a dp
 * threshold - the same title, rendered beside a short badge and beside a very long one, must lay
 * out identically. A threshold alone would need re-tuning on every type change; this states the law.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class SettingsRowRenderTest {

    @get:Rule
    val rule = createComposeRule()

    @Test
    fun `every permission state renders its word, never a tint alone`() {
        // The production badges themselves, both ends of every readable one, so a relabelled state
        // cannot drift out of this list silently.
        val badges = buildList {
            add(grantBadge(granted = true))
            add(grantBadge(granted = false))
            // Listed rather than iterated: `DeviceControlStatus` is a sealed interface, so a new
            // variant appearing here is a compile error in `shizukuBadge`'s own `when` first.
            listOf(
                DeviceControlStatus.Ready,
                DeviceControlStatus.PermissionDenied,
                DeviceControlStatus.NotRunning,
                DeviceControlStatus.NotInstalled,
            ).forEach { add(shizukuBadge(it)) }
            AssistantRole.entries.forEach { add(it.badge) }
            add(ConnectionStatus.Unchecked.badge)
            add(ConnectionStatus.Unconfigured.badge)
            add(ConnectionStatus.Checking.badge)
            add(ConnectionStatus.Connected(modelCount = 3).badge)
            add(ConnectionStatus.Failed(reason = "connect timed out after 5000ms").badge)
        }.distinct()

        // One row at a time, so `assertIsDisplayed` means displayed - a tall stack of rows would
        // push most of them off a Robolectric screen and turn the assertion into "exists".
        val current = mutableStateOf(badges.first())
        rule.setContent {
            AuraTheme(reducedMotion = true) {
                Column(modifier = Modifier.width(RowWidth)) {
                    SettingsRow(
                        label = RowLabel,
                        caption = "What this control reaches",
                        trailing = { StatusBadgeText(current.value) },
                    )
                }
            }
        }

        for (badge in badges) {
            current.value = badge
            rule.waitForIdle()
            rule.onNodeWithText(RowLabel).assertIsDisplayed()
            rule.onNodeWithText(badge.label).assertIsDisplayed()
        }
    }

    /**
     * **`@GraphicsMode(NATIVE)` is what gives this test any power at all, and it is not optional.**
     *
     * Robolectric's DEFAULT graphics mode is `LEGACY`, whose `ShadowPaint.measureText` returns
     * `applyTextScaleX(text.length())` — the character count, as the pixel width. No typeface is
     * consulted. Under it this 23-character title measures 23.0px beside BOTH badges, so the
     * comparison below is `23 == 23`: it passes, and it would keep passing however narrow the
     * allocation became. Measured, both modes, one probe:
     *
     * ```
     * LEGACY  title beside short badge = 23.0 x 35.0   title beside long badge = 23.0 x 35.0
     * NATIVE  title beside short badge = 150.0 x 17.0  title beside long badge = 150.0 x 17.0
     * ```
     *
     * 150px for 23 characters of 14sp type is a real advance width; 23px is `length()`. Only the
     * second pair can witness a squeeze, which is the entire subject of this test.
     *
     * The annotation targets `METHOD`, so native graphics is scoped to this one case rather than
     * imposed on every Robolectric suite in the module.
     */
    @Test
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun `a long badge does not squeeze the section title`() {
        rule.setContent {
            AuraTheme(reducedMotion = true) {
                Column(modifier = Modifier.width(RowWidth)) {
                    listOf(
                        StatusBadge("Connected", StatusTone.Granted),
                        StatusBadge(LongBadgeLabel, StatusTone.Problem),
                    ).forEach { badge ->
                        SettingsSection(
                            id = badge.label,
                            title = SectionTitle,
                            icon = Icons.Filled.Lock,
                            expansion = SectionExpansion(),
                            summary = badge,
                        ) {}
                    }
                }
            }
        }

        // Unmerged: the header `Row` merges its descendants for TalkBack, so a merged-tree lookup
        // hands back the row's own full-width bounds from both sections and passes no matter what
        // the title did.
        val titles = rule.onAllNodesWithText(SectionTitle, useUnmergedTree = true)
        titles.assertCountEquals(2)
        val besideShort = titles[0].fetchSemanticsNode().boundsInRoot
        val besideLong = titles[1].fetchSemanticsNode().boundsInRoot

        assertEquals("a longer badge changed the title's width", besideShort.width, besideLong.width, TolerancePx)
        assertEquals("a longer badge changed the title's height", besideShort.height, besideLong.height, TolerancePx)
        // A CONTROL check, and it is the one that makes the two comparisons above mean anything: a
        // squeeze that hit BOTH renders equally would satisfy them, and so would a harness measuring
        // every string at `length()` px. Under real metrics this title is 150px wide on one line;
        // the floor sits far below that and far above the ~23 either failure produces.
        assertTrue(
            "the title measured ${besideLong.width}px — too narrow to be real text, so the " +
                "comparisons above have no power to fail",
            besideLong.width > MinRealTitleWidthPx,
        )
        assertTrue(
            "the title wrapped into a column of characters (${besideLong.height}px tall)",
            besideLong.height < MaxTitleHeightPx,
        )
    }

    private companion object {
        val RowWidth = 360.dp
        const val RowLabel = "Display over other apps"
        const val SectionTitle = "Connection and identity"
        const val LongBadgeLabel = "Failed to connect to http://mewbo.local:5125 after 5000ms"
        const val TolerancePx = 0.5f

        /** Comfortably above two lines of `sectionHeader` type and far below the ~23 the bug produced. */
        const val MaxTitleHeightPx = 300f

        /**
         * Halfway between the two worlds: measured at 150px under real metrics and 23px under the
         * `length()` stub, so this catches a suite that silently loses native graphics as well as a
         * genuine squeeze.
         */
        const val MinRealTitleWidthPx = 80f
    }
}
