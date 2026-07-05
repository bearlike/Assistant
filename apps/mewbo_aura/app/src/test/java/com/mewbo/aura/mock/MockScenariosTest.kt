package com.mewbo.aura.mock

import com.mewbo.aura.data.model.SessionEvent
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The actual value of this test file (per the #181-follow-up task brief): every scripted
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
    fun `forQuery selects by keyword - error and long override the happyPath default`() {
        assertEquals(MockScenarios.errorScenario, MockScenarios.forQuery("please trigger an ERROR"))
        assertEquals(MockScenarios.longResponse, MockScenarios.forQuery("give me a LONG reply"))
        assertEquals(MockScenarios.happyPath, MockScenarios.forQuery("hello there"))
        assertEquals(MockScenarios.happyPath, MockScenarios.forQuery(""))
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
