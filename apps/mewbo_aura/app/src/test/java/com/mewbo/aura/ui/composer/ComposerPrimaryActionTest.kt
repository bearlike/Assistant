package com.mewbo.aura.ui.composer

import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.text.input.TextFieldValue
import com.mewbo.aura.ui.theme.AuraTheme
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The composer's primary affordance, asserted on the RENDERED tree rather than on
 * [ComposerState.resolve]'s return value.
 *
 * [ComposerStateTest] already pins which state each input combination resolves to; nothing pinned
 * that the resolved state actually PUTS an action on screen. The two failures that gap allows are
 * both silent: a state rendering NO primary action strands a live run with no way to stop it, and a
 * state rendering BOTH reads as two competing primary buttons. Neither shows up in a state
 * assertion, and neither throws.
 *
 * The invariant is one line: across every state and both styles, exactly one of
 * [PrimaryActionDescriptions] is on screen. C3's left-slot "Stop dictation" tile is deliberately
 * NOT in that set - it ends a capture, it never touches the run - and exact-match content
 * descriptions keep it from colliding with "Stop".
 *
 * **[ComposerState.Dictation] is asserted with a non-null `partialText` only, and that is not
 * incidental.** The null-partial branch renders [RmsWaveform], whose `withFrameNanos` loop never
 * yields an idle frame, so a Compose test over it would hang rather than fail. The
 * waveform-vs-transcript split is a center-content concern and cannot reach the trailing cluster
 * this test is about.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ComposerPrimaryActionTest {

    @get:Rule
    val rule = createComposeRule()

    @Test
    fun `every composer state renders exactly one primary action`() {
        val cases = ComposerStyle.entries.flatMap { style ->
            listOf(
                Case(ComposerState.Idle, style, draft = "", expected = "Voice mode"),
                Case(ComposerState.Typing, style, draft = "hi", expected = "Send"),
                Case(ComposerState.Dictation(rmsDb = 0f, partialText = "hello"), style, draft = "", expected = "Send"),
                Case(ComposerState.Streaming(hasDraft = true), style, draft = "queued", expected = "Send"),
                Case(ComposerState.Streaming(hasDraft = false), style, draft = "", expected = "Stop"),
            )
        }
        val current = mutableStateOf(cases.first())

        rule.setContent {
            val case = current.value
            AuraTheme(reducedMotion = true) {
                AuraComposer(
                    state = case.state,
                    draft = TextFieldValue(case.draft),
                    onDraftChange = {},
                    onSend = {},
                    onStop = {},
                    onMicTap = {},
                    onDictationStop = {},
                    onVoiceModeTap = {},
                    style = case.style,
                )
            }
        }

        for (case in cases) {
            current.value = case
            rule.waitForIdle()
            val present = PrimaryActionDescriptions.filter { description ->
                rule.onAllNodesWithContentDescription(description, useUnmergedTree = true)
                    .fetchSemanticsNodes().isNotEmpty()
            }
            assertEquals("$case renders the wrong primary action set", listOf(case.expected), present)
        }
    }

    private data class Case(
        val state: ComposerState,
        val style: ComposerStyle,
        val draft: String,
        val expected: String,
    )

    private companion object {
        /** The trailing cluster's primary slot, in every form it can take. */
        val PrimaryActionDescriptions = listOf("Send", "Stop", "Voice mode")
    }
}
