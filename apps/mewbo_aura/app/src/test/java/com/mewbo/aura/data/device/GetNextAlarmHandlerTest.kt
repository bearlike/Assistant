package com.mewbo.aura.data.device

import java.time.Instant
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** [NextAlarmReader] is the injectable seam over `AlarmManager.getNextAlarmClock()`
 * (apps/mewbo_aura/CLAUDE.md - `AlarmClockInfo`'s `showIntent: PendingIntent` field can't be
 * hand-doubled in a plain-JVM test), so [GetNextAlarmHandler]'s exists/none formatting is tested
 * against a plain in-memory fake here, no real `AlarmManager` involved. */
class GetNextAlarmHandlerTest {

    @Test
    fun `no scheduled alarm formats as exists=false with no other fields`() = runTest {
        val handler = GetNextAlarmHandler(NextAlarmReader { null })

        val result = handler.execute(buildJsonObject {})

        assertFalse(result["exists"]!!.jsonPrimitive.boolean)
        assertNull(result["trigger_at"])
        assertNull(result["owner_package"])
    }

    @Test
    fun `a scheduled alarm formats exists=true with an ISO-8601 trigger_at and the owner package`() = runTest {
        val triggerAt = Instant.parse("2026-07-04T06:30:00Z")
        val handler = GetNextAlarmHandler(
            NextAlarmReader { NextAlarmInfo(triggerAtEpochMillis = triggerAt.toEpochMilli(), ownerPackage = "com.google.android.deskclock") },
        )

        val result = handler.execute(buildJsonObject {})

        assertTrue(result["exists"]!!.jsonPrimitive.boolean)
        assertEquals(triggerAt, Instant.parse(result["trigger_at"]!!.jsonPrimitive.content))
        assertEquals("com.google.android.deskclock", result["owner_package"]!!.jsonPrimitive.content)
    }

    @Test
    fun `a scheduled alarm with no resolvable owner package omits that field rather than emitting null`() = runTest {
        val handler = GetNextAlarmHandler(NextAlarmReader { NextAlarmInfo(triggerAtEpochMillis = 0L, ownerPackage = null) })

        val result = handler.execute(buildJsonObject {})

        assertTrue(result["exists"]!!.jsonPrimitive.boolean)
        assertFalse(result.containsKey("owner_package"))
    }
}
