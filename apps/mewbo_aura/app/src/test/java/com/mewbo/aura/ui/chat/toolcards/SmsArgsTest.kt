package com.mewbo.aura.ui.chat.toolcards

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [SmsArgs] is the whole non-Compose half of `SmsToolCard`. [SmsArgs.parse] is TOTAL over model
 * output, same contract as [AlarmArgs.parse]/[TimerArgs.parse]: every malformed shape - missing
 * fields, a blank recipient/body, a nested shape where a string belongs - returns null so the card
 * can degrade instead of crashing the transcript or rendering half-empty.
 */
class SmsArgsTest {

    private fun parse(json: String) = SmsArgs.parse(Json.parseToJsonElement(json))

    @Test
    fun `parses the tool's documented argument shape`() {
        val args = parse("""{"to":"+15551234567","body":"On my way"}""")

        assertEquals(SmsArgs(to = "+15551234567", body = "On my way"), args)
    }

    @Test
    fun `a bare numeric recipient still parses - that is a shape difference, not a bad value`() {
        assertEquals(SmsArgs(to = "15551234567", body = "hi"), parse("""{"to":15551234567,"body":"hi"}"""))
    }

    @Test
    fun `every malformed shape the model can emit returns null`() {
        assertNull(parse("""{"body":"hi"}"""))                              // no recipient
        assertNull(parse("""{"to":"+15551234567"}"""))                      // no body
        assertNull(parse("""{"to":"","body":"hi"}"""))                      // blank recipient
        assertNull(parse("""{"to":"+15551234567","body":"   "}"""))         // blank body
        assertNull(parse("""{"to":{"number":"+15551234567"},"body":"hi"}""")) // nested object where a string belongs
        assertNull(parse("""{"to":["+15551234567"],"body":"hi"}"""))        // array where a string belongs
        assertNull(parse("""{"to":null,"body":"hi"}"""))                    // explicit JSON null
        assertNull(parse(""""hi""""))                                       // a bare string, not an object
        assertNull(parse("""[]"""))                                         // an array, not an object
    }

    @Test
    fun `null input degrades rather than throwing`() {
        assertNull(SmsArgs.parse(null))
    }
}
