package com.mewbo.aura.data.device

import android.provider.AlarmClock
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Test

/** [resolveDismissAlarmExtras] is [DismissAlarmHandler.execute]'s validate-and-map step, split out
 * specifically so this suite never needs a real `Intent` (apps/mewbo_aura/CLAUDE.md - not
 * hand-doubleable in a plain-JVM test any more than `android.net.Uri`). `AlarmClock.ALARM_SEARCH_MODE_*`
 * are plain `public static final String` constants, safe to read directly off the stub android.jar. */
class DismissAlarmHandlerTest {

    @Test
    fun `search_mode 'time' with valid hour and minute maps to ALARM_SEARCH_MODE_TIME`() {
        val extras = resolveDismissAlarmExtras(argsOf { put("search_mode", "time"); put("hour", 7); put("minute", 30) })

        assertEquals(AlarmClock.ALARM_SEARCH_MODE_TIME, extras.searchMode)
        assertEquals(7, extras.hour)
        assertEquals(30, extras.minute)
        assertNull(extras.label)
    }

    @Test
    fun `search_mode 'time' missing hour throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "time"); put("minute", 30) }) }
    }

    @Test
    fun `search_mode 'time' missing minute throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "time"); put("hour", 7) }) }
    }

    @Test
    fun `search_mode 'time' with an out-of-range hour throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "time"); put("hour", 24); put("minute", 0) }) }
    }

    @Test
    fun `search_mode 'time' with an out-of-range minute throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "time"); put("hour", 0); put("minute", 60) }) }
    }

    @Test
    fun `search_mode 'label' with a label maps to ALARM_SEARCH_MODE_LABEL`() {
        val extras = resolveDismissAlarmExtras(argsOf { put("search_mode", "label"); put("label", "Wake up") })

        assertEquals(AlarmClock.ALARM_SEARCH_MODE_LABEL, extras.searchMode)
        assertEquals("Wake up", extras.label)
        assertNull(extras.hour)
    }

    @Test
    fun `search_mode 'label' missing label throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "label") }) }
    }

    @Test
    fun `search_mode 'label' with a blank label throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "label"); put("label", "   ") }) }
    }

    @Test
    fun `search_mode 'next' maps to ALARM_SEARCH_MODE_NEXT with no other extras`() {
        val extras = resolveDismissAlarmExtras(argsOf { put("search_mode", "next") })

        assertEquals(AlarmClock.ALARM_SEARCH_MODE_NEXT, extras.searchMode)
        assertNull(extras.hour)
        assertNull(extras.minute)
        assertNull(extras.label)
    }

    @Test
    fun `search_mode 'all' maps to ALARM_SEARCH_MODE_ALL with no other extras`() {
        val extras = resolveDismissAlarmExtras(argsOf { put("search_mode", "all") })

        assertEquals(AlarmClock.ALARM_SEARCH_MODE_ALL, extras.searchMode)
        assertNull(extras.hour)
        assertNull(extras.minute)
        assertNull(extras.label)
    }

    @Test
    fun `an unrecognized search_mode throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(argsOf { put("search_mode", "yesterday") }) }
    }

    @Test
    fun `a missing search_mode throws DeviceToolArgsException`() {
        assertThrowsArgsException { resolveDismissAlarmExtras(buildJsonObject {}) }
    }

    private fun argsOf(build: JsonObjectBuilder.() -> Unit) = buildJsonObject(build)

    private fun assertThrowsArgsException(block: () -> Unit) {
        try {
            block()
            fail("expected DeviceToolArgsException")
        } catch (e: DeviceToolArgsException) {
            // expected
        }
    }
}
