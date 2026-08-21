package com.mewbo.aura.ui.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Security
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.text.TextLayoutResult
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
 * The settings surface at TELEVISION geometry, rather than at a phone's.
 *
 * Every other Compose suite in this module renders at Robolectric's default handset config, so no
 * existing test has ever measured this screen at 960dp × 540dp — the shape the app now admits
 * itself onto. The two things that can go wrong there are different from the phone's: text laid out
 * against a constraint nobody re-checked at 16:9, and the TV-only permission fold
 * ([systemPermissionTones] dropping the assistant row) never being seen to reach a pixel.
 *
 * **What this suite does NOT cover, and cannot.** The other half of the TV fold — that
 * `SettingsScreen` actually omits the "Default assistant" row when `isTelevision` is true — lives in
 * `SettingsScreen`, which takes a `@HiltViewModel` and reaches for `EntryPointAccessors`. This
 * module carries no `hilt-android-testing`, so the screen cannot be composed here, and rebuilding
 * its permissions section out of local `SettingsRow` calls would assert a copy of the condition
 * against itself: a test with no production mutation that can turn it red. [TelevisionSurfacesTest]
 * holds the half that IS observable — the count the header folds — and the render half is left
 * honestly uncovered rather than covered by a tautology.
 *
 * Qualifiers are the exact string the app-root CLAUDE.md records as accepted by Robolectric;
 * `xhdpi` puts density at 2.0, so every pixel figure below is twice its density-1.0 equivalent.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], qualifiers = "w960dp-h540dp-television-xhdpi")
class SettingsTvLayoutTest {

    @get:Rule
    val rule = createComposeRule()

    /**
     * **`@GraphicsMode(NATIVE)` is what gives this test any power at all.**
     *
     * Robolectric defaults to `LEGACY`, whose `ShadowPaint.measureText` returns the CHARACTER COUNT
     * as the pixel width — no typeface is consulted. Under it this title measures its own length in
     * pixels, fits on one line inside any container wider than ~23px, and the line-count assertion
     * below passes however badly the header is squeezed. The control assertion is the guard on the
     * guard: it fails if the suite ever silently loses native graphics, which is the failure that
     * would otherwise turn this whole test green and empty.
     *
     * The annotation targets `METHOD`, so native graphics stays scoped here rather than imposed on
     * every Robolectric suite in the module.
     *
     * The claim is about TEXT, so it reads [TextLayoutResult] off the node's own layout rather than
     * `boundsInRoot` — the latter reports the LAYOUT SLOT, which for a weighted title is the slot's
     * width and says nothing about the glyphs. Both facts are recorded in the test-package
     * CLAUDE.md; conflating them has cost hours here before.
     */
    @Test
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun `the permissions header lays out as real single-line text at television geometry`() {
        rule.setContent {
            AuraTheme(reducedMotion = true) {
                // Full width, no artificial cap: the point is to measure against the TELEVISION
                // container the qualifiers establish, not against a width this test chose.
                Column(modifier = Modifier.fillMaxWidth()) {
                    SettingsSection(
                        id = SectionId,
                        title = SectionTitle,
                        icon = Icons.Filled.Security,
                        // Collapsed, which is how every section on this screen actually opens.
                        expansion = SectionExpansion(),
                        summary = permissionSummary(systemPermissionTones(TelevisionState)),
                    ) {}
                }
            }
        }

        // Unmerged: the header `Row` merges its descendants for TalkBack, so a merged lookup hands
        // back the row's own full-width bounds and its combined text instead of the title's.
        val title = rule.onNodeWithText(SectionTitle, useUnmergedTree = true).fetchSemanticsNode()
        val layouts = mutableListOf<TextLayoutResult>()
        title.config[SemanticsActions.GetTextLayoutResult].action?.invoke(layouts)
        val layout = layouts.first()

        // THE CONTROL, and nothing below means anything without it. Real 14sp metrics put this
        // 18-character title in the hundreds of pixels at density 2.0; `measureText`-as-`length()`
        // puts it at 18. The floor sits far above the second and far below the first.
        assertTrue(
            "the title measured ${layout.size.width}px — too narrow to be real text, so the " +
                "line-count assertion below has no power to fail",
            layout.size.width > MinRealTitleWidthPx,
        )
        // The claim: at 960dp there is room for this title several times over, so anything that
        // constrains it — a TV branch capping the content width, or a badge moved back beside the
        // title, where a `Row` measures its unweighted children first and squeezes the weighted one
        // toward zero — shows up as the title wrapping.
        assertEquals("the section title wrapped at television width", 1, layout.lineCount)

        // The TV fold, seen on screen rather than only in the fold's own return value. With every
        // Android grant held, a television has four permission rows and a handheld has five, so a
        // build that counted the hidden assistant row again renders "4 of 5 granted" here.
        rule.onNodeWithText("All granted", useUnmergedTree = true).assertIsDisplayed()
    }

    private companion object {
        const val SectionId = "permissions"

        /** The production heading, verbatim — 19 characters. */
        const val SectionTitle = "System permissions"

        /**
         * A television with everything Android CAN grant granted, so the only variable left in the
         * header's summary is whether the hidden assistant row is still being counted.
         * [AssistantRole.Unknown] is the honest value on a TV: nothing ever reads the role there.
         */
        val TelevisionState = SettingsUiState(
            assistantRole = AssistantRole.Unknown,
            notificationsGranted = true,
            smsAccessGranted = true,
            deviceControlStatus = DeviceControlStatus.Ready,
            overlayPermissionGranted = true,
            isTelevision = true,
        )

        /**
         * Between the two worlds: ~250px under real metrics at `xhdpi`, 19px under the `length()`
         * stub. Deliberately not tuned close to the real figure — it is a degeneracy detector, not
         * a typography assertion, and a font or type-scale change must not redden it.
         */
        const val MinRealTitleWidthPx = 80
    }
}
