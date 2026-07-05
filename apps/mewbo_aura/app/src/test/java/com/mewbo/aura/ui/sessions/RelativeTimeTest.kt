package com.mewbo.aura.ui.sessions

import java.time.Instant
import java.time.ZoneOffset
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [RelativeTime.format] itself needs `DateUtils.getRelativeTimeSpanString`, which throws "not
 * mocked" outside Robolectric/instrumented tests - this module may not add either (build files
 * are off-limits here), so the success paths are asserted against [RelativeTime.parseEpochMillis]
 * directly. The fallback paths never reach `DateUtils` and go through [RelativeTime.format].
 */
class RelativeTimeTest {

    @Test
    fun `live backend format with microseconds and numeric offset parses`() {
        // Exact string observed in a live E2E screenshot - this is what regressed under
        // Instant.parse (it only accepts the literal Z zone designator, not a numeric offset).
        val iso = "2026-07-02T03:34:42.633140+00:00"

        val result = RelativeTime.parseEpochMillis(iso)

        assertEquals(Instant.parse("2026-07-02T03:34:42.633140Z").toEpochMilli(), result)
    }

    @Test
    fun `zulu offset still parses`() {
        val iso = "2026-07-02T03:34:42.633140Z"

        val result = RelativeTime.parseEpochMillis(iso)

        assertEquals(Instant.parse(iso).toEpochMilli(), result)
    }

    @Test
    fun `malformed timestamp fails to parse`() {
        assertNull(RelativeTime.parseEpochMillis("not-a-timestamp"))
    }

    @Test
    fun `empty timestamp fails to parse`() {
        assertNull(RelativeTime.parseEpochMillis(""))
    }

    @Test
    fun `malformed timestamp falls back to the raw string in format`() {
        val garbage = "not-a-timestamp"
        assertEquals(garbage, RelativeTime.format(garbage))
    }

    @Test
    fun `empty timestamp falls back to the empty string in format`() {
        assertEquals("", RelativeTime.format(""))
    }

    @Test
    fun `formatShort renders today`() {
        val now = Instant.parse("2026-07-02T12:00:00Z")
        val iso = "2026-07-02T03:34:42Z"

        assertEquals("Today", RelativeTime.formatShort(iso, now = now, zone = ZoneOffset.UTC))
    }

    @Test
    fun `formatShort renders yesterday`() {
        val now = Instant.parse("2026-07-02T12:00:00Z")
        val iso = "2026-07-01T23:59:00Z"

        assertEquals("Yesterday", RelativeTime.formatShort(iso, now = now, zone = ZoneOffset.UTC))
    }

    @Test
    fun `formatShort renders an abbreviated date for anything older`() {
        val now = Instant.parse("2026-07-02T12:00:00Z")
        val iso = "2026-06-29T08:00:00Z"

        assertEquals("Jun 29", RelativeTime.formatShort(iso, now = now, zone = ZoneOffset.UTC))
    }

    @Test
    fun `formatShort falls back to the raw string on malformed input`() {
        assertEquals("not-a-timestamp", RelativeTime.formatShort("not-a-timestamp"))
    }
}
