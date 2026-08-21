package com.mewbo.aura.ui.composer

import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotFocused
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.pressKey
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.AuraTheme
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * A D-pad can get OUT of the real composer — driven through [AuraComposer] itself, not through a
 * bare `BasicTextField` carrying the same modifier.
 *
 * That distinction is the point of this suite existing alongside
 * [com.mewbo.aura.ui.common.TextFieldFocusEscapeTest]. The pure suite pins the decision table and
 * would stay green if `Modifier.dpadFocusEscape` were deleted from every call site in the app; only
 * a test that composes the production composer can witness the WIRING. Deleting the modifier from
 * `ComposerTextField` reddens this file and nothing else.
 *
 * **The failure it guards against is total, not cosmetic.** Measured on a 16:9 device before the
 * fix: the composer takes focus on the first frame, and all four arrows are consumed by the caret,
 * so the app opened onto a text field a remote could never leave — every other control on screen
 * unreachable for the life of the process.
 *
 * `ComposerState.Idle` throughout: the [RmsWaveform] branch runs a bare `withFrameNanos` loop that
 * never yields an idle frame, so a Compose test that reached it would HANG rather than fail (the
 * house trap recorded in the test-package CLAUDE.md).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ComposerDpadEscapeTest {

    @get:Rule
    val rule = createComposeRule()

    /**
     * The cold-launch case: an empty draft, which is at both the start and the end of its text, so
     * the escape table offers every arrow — and Up is the one that has somewhere to go.
     */
    @Test
    fun `pressing up in an empty composer moves focus to the control above it`() {
        setContent(TextFieldValue(""))

        val field = rule.onNode(hasSetTextAction())
        field.performClick()

        // THE CONTROL. Without it a run where the field never took focus at all would satisfy the
        // assertion below for entirely the wrong reason — "focus is not in the field" is true both
        // when the escape worked and when nothing was ever focused.
        field.assertIsFocused()
        rule.onNodeWithTag(Above).assertIsNotFocused()

        field.performKeyInput { pressKey(Key.DirectionUp) }

        rule.onNodeWithTag(Above).assertIsFocused()
        field.assertIsNotFocused()
    }

    /**
     * Down is deliberately REFUSED, and this pins that rather than the escape you might expect.
     *
     * The composer is the bottom-most control on every surface that hosts it, so downward has no
     * legitimate target. Compose does not merely fail the move: measured on a television, focus
     * left for a node the IME's reflow then destroyed, and the accessibility tree reported no
     * focused node at all from that point on — in every direction, permanently. A handheld recovers
     * because a finger grants focus again; a remote has no such gesture, so the app was
     * unrecoverable short of force-stopping it.
     *
     * `focusProperties { down = FocusRequester.Cancel }` on the field is what refuses the move, and
     * this asserts the consequence a user actually feels: the caret keeps the key, and focus is
     * still somewhere.
     */
    @Test
    fun `pressing down never strands focus outside the composer`() {
        setContent(TextFieldValue(""))

        val field = rule.onNode(hasSetTextAction())
        field.performClick()
        field.assertIsFocused()

        repeat(3) { field.performKeyInput { pressKey(Key.DirectionDown) } }

        field.assertIsFocused()
        rule.onNodeWithTag(Below).assertIsNotFocused()
    }

    /**
     * The other half of the trade, and the arm that stops "escape on every arrow" from passing.
     *
     * The composer is a CONTROLLED field here — `onDraftChange` is ignored — so the caret stays at
     * the offset this test set regardless of where the focusing click landed, which is what makes
     * a mid-text caret assertable at all.
     */
    @Test
    fun `pressing right with text still to the caret's right keeps focus in the field`() {
        setContent(TextFieldValue("hello", selection = TextRange(2)))

        val field = rule.onNode(hasSetTextAction())
        field.performClick()
        field.assertIsFocused()

        field.performKeyInput { pressKey(Key.DirectionRight) }

        field.assertIsFocused()
        rule.onNodeWithTag(Below).assertIsNotFocused()
    }

    /**
     * Two focusable neighbours so a vertical move has somewhere to land — `moveFocus` returns false
     * with nothing in the requested direction, and the modifier deliberately forwards the key to
     * the field in that case, so a composer with no neighbours could not distinguish "escaped" from
     * "nowhere to go".
     */
    private fun setContent(draft: TextFieldValue) {
        rule.setContent {
            AuraTheme(reducedMotion = true) {
                Column {
                    Neighbour(Above)
                    AuraComposer(
                        state = ComposerState.Idle,
                        draft = draft,
                        onDraftChange = {},
                        onSend = {},
                        onStop = {},
                        onMicTap = {},
                        onDictationStop = {},
                        onVoiceModeTap = {},
                        style = ComposerStyle.Docked,
                    )
                    Neighbour(Below)
                }
            }
        }
    }

    @androidx.compose.runtime.Composable
    private fun Neighbour(tag: String) {
        androidx.compose.foundation.layout.Box(
            // A real height: Compose's two-dimensional focus search compares BOUNDS, so a
            // zero-height neighbour is not a candidate in any direction.
            modifier = Modifier
                .fillMaxWidth()
                .height(NeighbourHeight)
                .testTag(tag)
                .focusable(),
        )
    }

    private companion object {
        const val Above = "above-the-composer"
        const val Below = "below-the-composer"
        val NeighbourHeight = 100.dp
    }
}
