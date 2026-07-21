package com.mewbo.aura.mock

import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TranscriptReducer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The actual value of this test file (per the follow-up task brief): every scripted
 * [MockScenarios] frame round-trips through the REAL [SessionEvent.decode] - the same parser
 * `SessionStreamClient` uses on a genuine SSE connection. A scenario frame that silently decoded to
 * [SessionEvent.Unknown] (a typo'd `type`, a wrong `@SerialName`) would still "work" on-device (the
 * reducer just drops `Unknown` events, no crash) while testing NOTHING real - this suite is what
 * catches that class of drift, not the scenario content itself.
 */
class MockScenariosTest {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    private fun decodeAll(scenario: MockScenarios.Scenario): List<SessionEvent> =
        scenario.frames.map { (_, frame) -> SessionEvent.decode(json, frame.toString()) }

    @Test
    fun `happyPath frames all decode to known SessionEvent types, never Unknown`() {
        val decoded = decodeAll(MockScenarios.happyPath)
        assertTrue(decoded.isNotEmpty())
        decoded.forEach { event -> assertFalse("frame decoded to Unknown: $event", event is SessionEvent.Unknown) }
    }

    @Test
    fun `happyPath ends in a successful Completion, no error`() {
        val completion = decodeAll(MockScenarios.happyPath).last() as SessionEvent.Completion
        assertTrue(completion.payload.done)
        assertNull(completion.payload.error)
    }

    @Test
    fun `happyPath finalizes with an Assistant event carrying the full reply text`() {
        val decoded = decodeAll(MockScenarios.happyPath)
        val deltas = decoded.filterIsInstance<SessionEvent.AgentMessageDelta>()
        val assistant = decoded.filterIsInstance<SessionEvent.Assistant>().single()
        // The deltas' concatenated text (word-for-word, ignoring exact whitespace chunking) must be
        // a prefix of the finalized Assistant text - proves the scripted deltas and the finalized
        // text aren't drifted copies of each other.
        val deltaWords = deltas.joinToString("") { it.payload.text }.trim().split(Regex("\\s+"))
        val assistantWords = assistant.payload.text.trim().split(Regex("\\s+"))
        assertEquals(assistantWords, deltaWords)
    }

    @Test
    fun `longResponse carries multiple deltas and ends in a successful Completion`() {
        val decoded = decodeAll(MockScenarios.longResponse)
        val deltas = decoded.filterIsInstance<SessionEvent.AgentMessageDelta>()
        assertTrue("expected multiple deltas to exercise streaming/scroll UI, got ${deltas.size}", deltas.size > 5)
        val completion = decoded.last() as SessionEvent.Completion
        assertTrue(completion.payload.done)
        assertNull(completion.payload.error)
    }

    @Test
    fun `errorScenario surfaces failure through completion, never a synthetic error event type`() {
        // data/CLAUDE.md's own verified contract: there is NO "error" event type on the real
        // backend - failure only ever surfaces via completion.error/last_error. This scenario must
        // never decode to Unknown (which a fake "type":"error" frame WOULD, since the polymorphic
        // dispatcher has no branch for it) - that would mean the scenario tests a shape the real
        // backend can never actually send.
        val decoded = decodeAll(MockScenarios.errorScenario)
        assertTrue(decoded.none { it is SessionEvent.Unknown })
        val completion = decoded.last() as SessionEvent.Completion
        assertEquals("Mock backend: scripted failure for error-path testing", completion.payload.error)
        assertEquals("error", completion.payload.doneReason)
    }

    @Test
    fun `every root-agent delta and message has depth 0, so TranscriptReducer renders it`() {
        // TranscriptReducer drops anything with depth != 0 (sub-agent narration, data/CLAUDE.md) -
        // a scenario that accidentally scripted depth=1 would silently render as an empty reply
        // on-device, not a test failure here, so assert it directly.
        val all = MockScenarios.happyPath.frames + MockScenarios.longResponse.frames
        all.forEach { (_, frame) ->
            val type = frame["type"]?.jsonPrimitive?.content
            if (type == "agent_message_delta" || type == "agent_message") {
                val depth = frame["payload"]?.let { it as? kotlinx.serialization.json.JsonObject }?.get("depth")?.jsonPrimitive?.content
                assertEquals("$type frame must be depth 0: $frame", "0", depth)
            }
        }
    }

    @Test
    fun `forQuery selects by keyword - error, long, alarm and widget override the happyPath default`() {
        assertEquals(MockScenarios.errorScenario, MockScenarios.forQuery("please trigger an ERROR"))
        assertEquals(MockScenarios.longResponse, MockScenarios.forQuery("give me a LONG reply"))
        assertEquals(MockScenarios.alarmScenario, MockScenarios.forQuery("set an ALARM for 8am"))
        assertEquals(MockScenarios.widgetScenario, MockScenarios.forQuery("show me a WIDGET"))
        assertEquals(MockScenarios.happyPath, MockScenarios.forQuery("hello there"))
        assertEquals(MockScenarios.happyPath, MockScenarios.forQuery(""))
    }

    /**
     * The widget scenario is the only scripted path to the stlite WebView card -
     * pinned end-to-end through the REAL decoder AND reducer so a wire-field or `WidgetFiles` drift
     * fails here rather than silently rendering nothing on-device. Round-tripping the two-file bundle
     * is exactly what catches a `files` shape the real backend never sends.
     */
    @Test
    fun `widgetScenario decodes to a WidgetReady and folds to a Widget item above the narration`() {
        val decoded = decodeAll(MockScenarios.widgetScenario)
        decoded.forEach { event -> assertFalse("frame decoded to Unknown: $event", event is SessionEvent.Unknown) }

        val items = TranscriptReducer.reduce(decoded)
        val widget = items.filterIsInstance<ChatItem.Widget>().single()
        assertTrue("app.py must carry the streamlit script", widget.appPy.contains("import streamlit"))
        assertTrue("data.json must be present", widget.dataJson.isNotBlank())
        val widgetIndex = items.indexOf(widget)
        val replyIndex = items.indexOfFirst { it is ChatItem.AssistantMessage }
        assertTrue("widget must render above the narration", widgetIndex < replyIndex)
    }

    /**
     * The alarm scenario is the only scripted path to a promoted-tool action card, so it is pinned
     * end-to-end through the REAL decoder AND the REAL reducer - not just "does it decode". This is
     * what makes the action card verifiable without a live backend (and without spending tokens):
     * if the promotion gate, the wire field names, or `ToolCall.success` ever drift, this fails here
     * rather than silently rendering the generic fold on-device.
     */
    @Test
    fun `alarmScenario promotes to a ToolCard and still renders above the narration`() {
        val decoded = decodeAll(MockScenarios.alarmScenario)
        decoded.forEach { event -> assertFalse("frame decoded to Unknown: $event", event is SessionEvent.Unknown) }

        val items = TranscriptReducer.reduce(decoded)
        val card = items.filterIsInstance<ChatItem.ToolCard>().single()
        assertEquals("device_set_alarm", card.call.toolId)
        assertTrue("a promoted call must have succeeded", card.call.success)

        // tool_input must survive as a JSON OBJECT - a stringified input would parse to null and
        // silently degrade the alarm to the generic fallback card (see MockScenarios.alarmScenario).
        val args = card.call.inputJson as JsonObject
        assertEquals("8", args["hour"]?.jsonPrimitive?.content)
        assertEquals("0", args["minute"]?.jsonPrimitive?.content)

        // Never folded into the generic group, and never below the reply (activity-precedes-narration).
        assertTrue("promoted call must not also fold into a group", items.none { it is ChatItem.ToolCallGroup })
        val cardIndex = items.indexOf(card)
        val replyIndex = items.indexOfFirst { it is ChatItem.AssistantMessage }
        assertTrue("action card must render above the narration", cardIndex < replyIndex)
    }

    @Test
    fun `every frame's ts is a numeric offset, never bare Z`() {
        // Backend-timestamp trap (app/src/test/java/com/mewbo/aura/CLAUDE.md): OffsetDateTime.parse
        // accepts a bare Z too, so a regression back to Instant.toString() wouldn't fail decode -
        // assert the wire SHAPE directly instead of relying on a parse failure to catch it.
        val all = MockScenarios.happyPath.frames + MockScenarios.longResponse.frames + MockScenarios.errorScenario.frames
        all.forEach { (_, frame) ->
            val ts = frame["ts"]?.jsonPrimitive?.content
            assertTrue("frame ts '$ts' must carry a numeric offset, not bare Z", ts != null && ts.isNotBlank() && !ts.endsWith("Z"))
        }
    }
}
