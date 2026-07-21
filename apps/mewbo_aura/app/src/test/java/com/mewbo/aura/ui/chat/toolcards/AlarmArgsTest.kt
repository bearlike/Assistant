package com.mewbo.aura.ui.chat.toolcards

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [AlarmArgs] is the whole non-Compose half of `AlarmToolCard`. Two contracts matter: the 12-hour
 * display line matches the reference app's own form exactly (including the 0/12 wrap, which is where
 * hand-rolled clock math always breaks), and [AlarmArgs.parse] is TOTAL over model output - every
 * malformed shape returns null so the card can degrade instead of crashing the transcript.
 */
class AlarmArgsTest {

    private fun parse(json: String) = AlarmArgs.parse(Json.parseToJsonElement(json))

    @Test
    fun `formats a morning time like the reference app`() {
        assertEquals("8:00 AM", AlarmArgs(hour = 8, minute = 0, message = null).displayTime)
    }

    @Test
    fun `pads the minutes but never the hour`() {
        assertEquals("9:05 AM", AlarmArgs(hour = 9, minute = 5, message = null).displayTime)
    }

    @Test
    fun `midnight is 12 AM, not 0 AM`() {
        assertEquals("12:00 AM", AlarmArgs(hour = 0, minute = 0, message = null).displayTime)
        assertEquals("12:30 AM", AlarmArgs(hour = 0, minute = 30, message = null).displayTime)
    }

    @Test
    fun `noon is 12 PM, not 0 PM`() {
        assertEquals("12:00 PM", AlarmArgs(hour = 12, minute = 0, message = null).displayTime)
    }

    @Test
    fun `afternoon hours wrap into the 12-hour clock`() {
        assertEquals("1:00 PM", AlarmArgs(hour = 13, minute = 0, message = null).displayTime)
        assertEquals("11:59 PM", AlarmArgs(hour = 23, minute = 59, message = null).displayTime)
    }

    @Test
    fun `parses the tool's documented argument shape`() {
        val args = parse("""{"hour":8,"minute":0,"message":"Gym"}""")

        assertEquals(AlarmArgs(hour = 8, minute = 0, message = "Gym"), args)
    }

    @Test
    fun `message is optional`() {
        assertEquals(AlarmArgs(hour = 7, minute = 15, message = null), parse("""{"hour":7,"minute":15}"""))
    }

    @Test
    fun `a blank message is no message - it would render an empty line under the time`() {
        assertNull(parse("""{"hour":7,"minute":15,"message":"   "}""")?.message)
    }

    @Test
    fun `a numeric field arriving as a string still parses - that is a shape difference, not a bad value`() {
        assertEquals(AlarmArgs(hour = 8, minute = 5, message = null), parse("""{"hour":"8","minute":"5"}"""))
    }

    @Test
    fun `null input degrades rather than throwing`() {
        assertNull(AlarmArgs.parse(null))
    }

    @Test
    fun `every malformed shape the model can emit returns null`() {
        assertNull(parse("""{"minute":30}"""))                        // no hour
        assertNull(parse("""{"hour":8}"""))                           // no minute
        assertNull(parse("""{"hour":24,"minute":0}"""))               // hour out of range
        assertNull(parse("""{"hour":-1,"minute":0}"""))               // hour out of range
        assertNull(parse("""{"hour":8,"minute":60}"""))               // minute out of range
        assertNull(parse("""{"hour":{"at":8},"minute":0}"""))         // nested object where a number belongs
        assertNull(parse("""{"hour":[8],"minute":0}"""))              // array where a number belongs
        assertNull(parse("""{"hour":null,"minute":0}"""))             // explicit JSON null
        assertNull(parse("""{"hour":"morning","minute":0}"""))        // unparseable string
        assertNull(parse(""""8:00 AM""""))                            // a bare string, not an object
        assertNull(parse("""[]"""))                                   // an array, not an object
    }
}
