package com.mewbo.aura.ui.common

import androidx.compose.foundation.text.BasicTextField
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.SoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.pressKey
import com.mewbo.aura.data.device.DeviceShape
import com.mewbo.aura.ui.theme.AuraTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * [imeOnConfirmOnly] separates focus from intent-to-type where the two differ, and is absent
 * entirely where they do not.
 *
 * **What this suite can and cannot witness, stated up front.** It provides its OWN
 * [SoftwareKeyboardController] through [LocalSoftwareKeyboardController] and counts the calls the
 * modifier makes. That is deliberate and it is also the limit: `BasicTextField` reaches the platform
 * IME through the text input session, NOT through this composition local, so nothing here proves the
 * real keyboard stayed down on a real television — only that the modifier asks for what it should, when
 * it should. A fake that also stood in for the field's own request would have been a fake of the
 * thing under test. The device-side claim ("navigating past a field does not raise the IME") is a
 * remote-in-hand check, not a JVM one.
 *
 * What it does have real power over is the part that regresses silently: the handheld arm. A
 * modifier that hid the keyboard on focus for everyone would be invisible in review, correct in
 * every TV test, and would break typing on every phone — so both directions are asserted, and the
 * handheld arm asserts an exact ZERO rather than a difference.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ImeOnConfirmOnlyTest {

    @get:Rule
    val rule = createComposeRule()

    /** The defect itself: on a remote, focus arrives from merely traversing past the field. */
    @Test
    fun `on a television focusing the field hides the keyboard instead of raising it`() {
        val keyboard = RecordingKeyboard()
        setContent(shape = DeviceShape.Television, keyboard = keyboard)

        val field = rule.onNode(hasSetTextAction())
        field.performClick()
        // THE CONTROL: a run where the field never took focus would satisfy "show was never called"
        // for entirely the wrong reason.
        field.assertIsFocused()

        assertEquals("focus must ask for the keyboard to go away", 1, keyboard.hides)
        assertEquals("focus alone must never raise the keyboard", 0, keyboard.shows)
    }

    /** The other half: the keyboard is reachable, so the field is not merely read-only by remote. */
    @Test
    fun `on a television the remote OK button raises the keyboard`() {
        val keyboard = RecordingKeyboard()
        setContent(shape = DeviceShape.Television, keyboard = keyboard)

        val field = rule.onNode(hasSetTextAction())
        field.performClick()
        field.assertIsFocused()

        field.performKeyInput { pressKey(Key.DirectionCenter) }

        assertEquals(1, keyboard.shows)
        // Still focused, so the next arrow press navigates rather than landing nowhere.
        field.assertIsFocused()
    }

    /**
     * The arm with the most to lose. A handheld gets the receiver back UNCHANGED — no focus
     * observer, no key handler — so the touch path cannot regress into "the keyboard will not come
     * up", which is a field that does nothing at all.
     */
    @Test
    fun `off a television the gate does nothing whatsoever`() {
        val keyboard = RecordingKeyboard()
        setContent(shape = DeviceShape.Handheld, keyboard = keyboard)

        val field = rule.onNode(hasSetTextAction())
        field.performClick()
        field.assertIsFocused()
        field.performKeyInput { pressKey(Key.DirectionCenter) }

        assertEquals(0, keyboard.hides)
        assertEquals(0, keyboard.shows)
    }

    /**
     * The handheld guarantee at its strongest reading: the SAME object back, not an equivalent
     * chain. Stated this way it also fails if someone "harmlessly" appends an always-false key
     * handler off television — a change the three behavioural arms above would not notice.
     */
    @Test
    fun `off a television the gate returns its receiver unchanged`() {
        var bare: Modifier? = null
        var gated: Modifier? = null
        rule.setContent {
            CompositionLocalProvider(LocalDeviceShape provides DeviceShape.Handheld) {
                // A non-empty receiver, so an implementation returning `Modifier` would not pass.
                bare = Modifier.testTag("gate-receiver")
                gated = bare!!.imeOnConfirmOnly()
            }
        }

        assertSame("the handheld path must not add a single modifier node", bare, gated)
    }

    private fun setContent(shape: DeviceShape, keyboard: SoftwareKeyboardController) {
        rule.setContent {
            CompositionLocalProvider(
                LocalDeviceShape provides shape,
                LocalSoftwareKeyboardController provides keyboard,
            ) {
                AuraTheme(reducedMotion = true) {
                    BasicTextField(value = "", onValueChange = {}, modifier = Modifier.imeOnConfirmOnly())
                }
            }
        }
    }

    private class RecordingKeyboard : SoftwareKeyboardController {
        var shows = 0
            private set
        var hides = 0
            private set

        override fun show() {
            shows++
        }

        override fun hide() {
            hides++
        }
    }
}
