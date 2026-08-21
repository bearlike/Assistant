package com.mewbo.aura.ui.control

import com.mewbo.aura.data.model.AgentMessageDeltaPayload
import com.mewbo.aura.data.model.AgentMessagePayload
import com.mewbo.aura.data.model.CompletionPayload
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TextPayload
import com.mewbo.aura.ui.theme.AuraMotion
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The overlay's whole decision surface: what appears over the user's screen, what it says, and
 * when it leaves. Every input is a typed event plus a caller-supplied `nowMs`, so expiry is
 * asserted at exact times instead of slept through, and nothing here needs a device.
 */
class DeviceControlNarrationTest {

    private val empty = DeviceControlNarration()

    // --- what narrates at all (the visibility decision) ---

    @Test
    fun `an event the overlay does not draw returns the SAME instance`() {
        // Identity, not merely equality: the fold runs on every frame of a busy stream and a
        // MutableStateFlow.update returning an equal-but-new value would still churn.
        val started = empty.fold(agentMessage("working on it"), nowMs = 0)

        val after = started
            .fold(SessionEvent.Completion("t", CompletionPayload(done = true)), nowMs = 10)
            .fold(SessionEvent.User("t", TextPayload("open settings")), nowMs = 10)
            .fold(SessionEvent.StreamEnd, nowMs = 10)

        assertSame(started, after)
    }

    @Test
    fun `a blank authoritative message neither blanks a drawn line nor adds an empty one`() {
        val drawn = empty.fold(agentMessage("tapping through"), nowMs = 0)

        val after = drawn.fold(agentMessage("   "), nowMs = 10)

        assertSame(drawn, after)
        assertEquals(listOf("tapping through"), after.bubbles.map { it.text })
    }

    // --- the streaming fold ---

    @Test
    fun `deltas append into ONE line and an agent_message replaces what they built`() {
        val streamed = empty
            .fold(delta("Opening "), nowMs = 0)
            .fold(delta("the settings "), nowMs = 10)
            .fold(delta("app"), nowMs = 20)

        assertEquals(listOf("Opening the settings app"), streamed.bubbles.map { it.text })

        val corrected = streamed.fold(agentMessage("Opening the display settings"), nowMs = 30)

        assertEquals(1, corrected.bubbles.size)
        assertEquals("Opening the display settings", corrected.bubbles.single().text)
    }

    @Test
    fun `a sub-agent narrates on its own line instead of into the root agent's sentence`() {
        val both = empty
            .fold(delta("root says", agentId = "root"), nowMs = 0)
            .fold(delta("child says", agentId = "child"), nowMs = 1)

        assertEquals(listOf("root says", "child says"), both.bubbles.map { it.text })
    }

    @Test
    fun `a refreshed line keeps its position rather than jumping to the newest slot`() {
        val stack = empty
            .fold(delta("narrating"), nowMs = 0)
            .fold(toolCall("call-1", "device_ui", buildJsonObject { put("action", "elements") }), nowMs = 1)
            .fold(delta(" some more"), nowMs = 2)

        assertEquals(
            listOf("narrating some more", "Reading the screen"),
            stack.bubbles.map { it.text },
        )
    }

    @Test
    fun `the stack is capped and the oldest line falls off`() {
        var stack = empty
        repeat(DeviceControlNarration.MAX_VISIBLE + 2) { i ->
            stack = stack.fold(
                toolCall("call-$i", "device_action", buildJsonObject { put("action", "tap") }),
                nowMs = i.toLong(),
            )
        }

        assertEquals(DeviceControlNarration.MAX_VISIBLE, stack.bubbles.size)
        assertEquals("call-2", stack.bubbles.first().id.removePrefix("call:"))
    }

    // --- truncation ---

    @Test
    fun `a long line keeps its TAIL, opens at a word boundary, and stays within the cap`() {
        val long = "the quick brown fox jumps over the lazy dog ".repeat(6) + "and then it stopped"

        val text = empty.fold(agentMessage(long), nowMs = 0).bubbles.single().text

        val drawn = text.removePrefix("…")
        assertTrue("expected an elision marker, got: $text", text.startsWith("…"))
        assertTrue("expected the newest words, got: $text", text.endsWith("and then it stopped"))
        assertTrue(
            "expected <= ${ControlBubble.MAX_CHARS} chars, got ${text.length}",
            text.length <= ControlBubble.MAX_CHARS,
        )
        // A genuine suffix of the source, opened at a word boundary — not a mid-word slice.
        assertTrue("expected a suffix of the source, got: $drawn", long.endsWith(drawn))
        assertTrue("expected a whole opening word, got: $drawn", long.contains(" $drawn"))
    }

    @Test
    fun `newlines collapse so a markdown list cannot turn one line into three`() {
        val text = empty.fold(agentMessage("Step one\n- two\n- three"), nowMs = 0).bubbles.single().text

        assertEquals("Step one - two - three", text)
    }

    @Test
    fun `a line at the cap is left alone`() {
        val exact = "a".repeat(ControlBubble.MAX_CHARS)

        val text = empty.fold(agentMessage(exact), nowMs = 0).bubbles.single().text

        assertEquals(exact, text)
    }

    // --- expiry ---

    @Test
    fun `expire drops only what has outlived its window`() {
        val stack = empty
            .fold(toolCall("old", "device_shell"), nowMs = 0)
            .fold(toolCall("new", "device_shell"), nowMs = AuraMotion.transientDismissMs / 2)

        val live = stack.expire(nowMs = AuraMotion.transientDismissMs + 1)

        assertEquals(listOf("call:new"), live.bubbles.map { it.id })
    }

    @Test
    fun `expire with nothing to drop returns the SAME instance`() {
        val stack = empty.fold(agentMessage("still going"), nowMs = 0)

        assertSame(stack, stack.expire(nowMs = AuraMotion.transientDismissMs - 1))
    }

    @Test
    fun `a line still being written does not expire underneath itself`() {
        val late = AuraMotion.transientDismissMs + 100

        val stack = empty
            .fold(delta("half a "), nowMs = 0)
            // The delta that lands after the original window would have closed restarts the clock.
            .fold(delta("sentence"), nowMs = late)

        val live = stack.expire(nowMs = late + 1)

        assertEquals(listOf("half a sentence"), live.bubbles.map { it.text })
    }

    // --- tool-call labels ---

    @Test
    fun `each device action reads as what it is doing to the phone`() {
        assertEquals("Tapping", label("device_action", buildJsonObject { put("action", "tap") }))
        assertEquals("Typing", label("device_action", buildJsonObject { put("action", "type") }))
        assertEquals(
            "Swiping up",
            label("device_action", buildJsonObject { put("action", "swipe"); put("direction", "up") }),
        )
        assertEquals(
            "Pressing back",
            label("device_action", buildJsonObject { put("action", "key"); put("key", "back") }),
        )
        assertEquals(
            "Opening com.android.settings",
            label(
                "device_action",
                buildJsonObject { put("action", "launch"); put("package_name", "com.android.settings") },
            ),
        )
        assertEquals("Reading the screen", label("device_ui", buildJsonObject { put("action", "elements") }))
        assertEquals("Looking at the screen", label("device_ui", buildJsonObject { put("action", "screenshot") }))
        assertEquals("Running a command", label("device_shell"))
    }

    @Test
    fun `a tool this surface has never heard of still says something happened`() {
        assertEquals("teleport", label("device_teleport"))
    }

    @Test
    fun `args are model output, so junk parses instead of throwing`() {
        // A nested object where a string was expected is the exact shape that makes the
        // `jsonPrimitive` accessor throw — on the live event path that would take the collector
        // down with it, so the parse degrades to the generic arm instead.
        val nested = buildJsonObject { put("action", buildJsonObject { put("nope", 1) }) }

        assertEquals("Acting on the screen", label("device_action", nested))
        assertEquals("Reading the screen", label("device_ui", nested))
        assertEquals("Swiping", label("device_action", buildJsonObject { put("action", "swipe") }))
    }

    // --- fixtures ---

    private fun agentMessage(text: String, agentId: String = "root") =
        SessionEvent.AgentMessage("t", AgentMessagePayload(text = text, agentId = agentId))

    private fun delta(text: String, agentId: String = "root") =
        SessionEvent.AgentMessageDelta("t", AgentMessageDeltaPayload(text = text, agentId = agentId))

    private fun toolCall(callId: String, toolId: String, args: JsonObject = JsonObject(emptyMap())) =
        SessionEvent.DeviceToolCall("t", payload(callId, toolId, args))

    private fun label(toolId: String, args: JsonObject = JsonObject(emptyMap())): String =
        DeviceControlNarration.label(payload("call", toolId, args))

    private fun payload(callId: String, toolId: String, args: JsonObject) = DeviceToolCallPayload(
        callId = callId,
        callToken = "token",
        toolId = toolId,
        args = args,
        expiresAt = 0.0,
    )
}
