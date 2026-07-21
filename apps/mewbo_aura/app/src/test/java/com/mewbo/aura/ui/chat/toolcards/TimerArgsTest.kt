package com.mewbo.aura.ui.chat.toolcards

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [TimerArgs] is the whole non-Compose half of `TimerToolCard`. Two contracts matter: the countdown
 * display line matches the reference clock app's own timer face (`M:SS` under an hour, `H:MM:SS`
 * once one is crossed), and [TimerArgs.parse] is TOTAL over model output - every malformed shape
 * returns null so the card can degrade instead of crashing the transcript.
 */
class TimerArgsTest {

    private fun parse(json: String) = TimerArgs.parse(Json.parseToJsonElement(json))

    @Test
    fun `formats seconds under a minute as 0-SS`() {
        assertEquals("0:05", TimerArgs(seconds = 5, label = null).displayDuration)
        assertEquals("0:59", TimerArgs(seconds = 59, label = null).displayDuration)
    }

    @Test
    fun `formats minutes and seconds once a minute is crossed`() {
        assertEquals("1:00", TimerArgs(seconds = 60, label = null).displayDuration)
        assertEquals("1:30", TimerArgs(seconds = 90, label = null).displayDuration)
        assertEquals("5:00", TimerArgs(seconds = 300, label = null).displayDuration)
        assertEquals("59:59", TimerArgs(seconds = 3599, label = null).displayDuration)
    }

    @Test
    fun `switches to H-MM-SS once an hour is crossed`() {
        assertEquals("1:00:00", TimerArgs(seconds = 3600, label = null).displayDuration)
        assertEquals("1:01:01", TimerArgs(seconds = 3661, label = null).displayDuration)
        assertEquals("2:00:05", TimerArgs(seconds = 7205, label = null).displayDuration)
    }

    @Test
    fun `parses the tool's documented argument shape`() {
        val args = parse("""{"seconds":300,"label":"Pasta"}""")

        assertEquals(TimerArgs(seconds = 300, label = "Pasta"), args)
    }

    @Test
    fun `label is optional`() {
        assertEquals(TimerArgs(seconds = 60, label = null), parse("""{"seconds":60}"""))
    }

    @Test
    fun `a blank label is no label - it would render an empty line under the duration`() {
        assertNull(parse("""{"seconds":60,"label":"   "}""")?.label)
    }

    @Test
    fun `a numeric field arriving as a string still parses - that is a shape difference, not a bad value`() {
        assertEquals(TimerArgs(seconds = 45, label = null), parse("""{"seconds":"45"}"""))
    }

    @Test
    fun `null input degrades rather than throwing`() {
        assertNull(TimerArgs.parse(null))
    }

    @Test
    fun `every malformed shape the model can emit returns null`() {
        assertNull(parse("""{"label":"Pasta"}"""))                     // no seconds
        assertNull(parse("""{"seconds":0}"""))                         // must be >= 1
        assertNull(parse("""{"seconds":-5}"""))                        // negative
        assertNull(parse("""{"seconds":{"value":60}}"""))              // nested object where a number belongs
        assertNull(parse("""{"seconds":[60]}"""))                      // array where a number belongs
        assertNull(parse("""{"seconds":null}"""))                      // explicit JSON null
        assertNull(parse("""{"seconds":"soon"}"""))                    // unparseable string
        assertNull(parse(""""5:00""""))                                // a bare string, not an object
        assertNull(parse("""[]"""))                                    // an array, not an object
    }
}
