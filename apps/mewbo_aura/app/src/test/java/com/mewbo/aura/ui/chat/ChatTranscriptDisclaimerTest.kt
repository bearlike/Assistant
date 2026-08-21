package com.mewbo.aura.ui.chat

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
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
 * The disclaimer's presence rules, asserted on the RENDERED transcript.
 *
 * [shouldShowDisclaimer] is already pure and already tested. That covers half the law and it is the
 * half that was never in doubt: the untested half is whether the caption the predicate governs
 * actually appears, once, at the bottom. A gate returning `true` over an item nobody declares, or
 * declared twice, or declared above the reply it follows, satisfies every existing assertion.
 *
 * The mutual exclusion with the thinking spark is asserted as the disclaimer being absent for every
 * in-flight [RunPhase], which is the same fact from the side that can be observed: `showThinking`
 * is `runPhase.isRunInFlight` verbatim, so "the disclaimer never renders while the spark does" and
 * "the disclaimer never renders while the run is in flight" are one statement. The spark itself
 * carries no semantics to query - it is a shader - and adding a test tag to production code to
 * observe it would be a change this suite is not allowed to make.
 *
 * Every [ChatItem.AssistantMessage] here is settled. A streaming one drives
 * `rememberStreamedText`'s unbounded `delay` sampler, which has no idle frame for a Compose test to
 * land on; "no settled reply" is expressed as a transcript with no assistant message at all, which
 * is the same input to the gate.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ChatTranscriptDisclaimerTest {

    @get:Rule
    val rule = createComposeRule()

    @Test
    fun `disclaimer renders exactly once, only for a settled reply on an idle run`() {
        // Spelled out rather than computed from [shouldShowDisclaimer]. Calling the predicate to
        // derive the expectation moves BOTH sides when the predicate changes, which is a test that
        // cannot fail - the exact disease this suite exists to treat. The cost of restating the
        // table is that a deliberate rule change has to be re-stated here too, which is the point.
        val cases = listOf(
            Scenario(settledReply = true, phase = RunPhase.Idle, visible = true),
            Scenario(settledReply = true, phase = RunPhase.Sending, visible = false),
            Scenario(settledReply = true, phase = RunPhase.Streaming, visible = false),
            Scenario(settledReply = true, phase = RunPhase.Done, visible = true),
            Scenario(settledReply = true, phase = RunPhase.Error, visible = true),
            Scenario(settledReply = false, phase = RunPhase.Idle, visible = false),
            Scenario(settledReply = false, phase = RunPhase.Sending, visible = false),
            Scenario(settledReply = false, phase = RunPhase.Streaming, visible = false),
            Scenario(settledReply = false, phase = RunPhase.Done, visible = false),
            Scenario(settledReply = false, phase = RunPhase.Error, visible = false),
        )
        val current = mutableStateOf(cases.first())
        setTranscript(current)

        for (case in cases) {
            current.value = case
            rule.waitForIdle()
            val rendered = rule.onAllNodesWithText(DISCLAIMER).fetchSemanticsNodes().size
            assertEquals("$case rendered the disclaimer $rendered time(s)", if (case.visible) 1 else 0, rendered)
        }
    }

    @Test
    fun `disclaimer renders below the newest turn`() {
        val current = mutableStateOf(Scenario(settledReply = true, phase = RunPhase.Done, visible = true))
        setTranscript(current)

        // Pixels throughout (`boundsInRoot`), never a mix of that and the Dp-typed
        // `getUnclippedBoundsInRoot` - comparing the two silently compares different units.
        val disclaimerTop = rule.onNodeWithText(DISCLAIMER).fetchSemanticsNode().boundsInRoot.top

        // `reverseLayout` renders DSL position 0 at the visual BOTTOM, so every other row - the
        // user bubble AND the reply that landed after it - must end above the disclaimer's top
        // edge. Asserting against both is what makes this fail if the item is ever declared
        // anywhere but position 0.
        for (text in listOf(USER_TEXT, REPLY_TEXT)) {
            val nodes = rule.onAllNodesWithText(text, substring = true, useUnmergedTree = true)
                .fetchSemanticsNodes()
            assertEquals("expected exactly one node rendering \"$text\"", 1, nodes.size)
            val bottom = nodes.single().boundsInRoot.bottom
            assertTrue(
                "disclaimer top $disclaimerTop must sit at or below \"$text\" bottom $bottom",
                disclaimerTop >= bottom,
            )
        }
    }

    private fun setTranscript(current: MutableState<Scenario>) {
        rule.setContent {
            val scenario = current.value
            AuraTheme(reducedMotion = true) {
                ChatTranscript(
                    items = scenario.items(),
                    runPhase = scenario.phase,
                    onRetry = {},
                    sessionEnded = false,
                    speakingKey = null,
                    onNotice = {},
                    onReadAloudToggle = {},
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
    }

    private data class Scenario(val settledReply: Boolean, val phase: RunPhase, val visible: Boolean) {
        fun items(): List<ChatItem> = buildList {
            add(ChatItem.UserBubble(text = USER_TEXT, ts = TS, key = "u1"))
            if (settledReply) {
                add(ChatItem.AssistantMessage(text = REPLY_TEXT, isStreaming = false, ts = TS, key = "a1"))
            }
        }
    }

    private companion object {
        const val DISCLAIMER = "Mewbo is an AI tool and can make mistakes."
        const val USER_TEXT = "what is the weather"
        const val REPLY_TEXT = "It is clear."
        const val TS = "2026-01-01T00:00:00.000000+00:00"
    }
}
