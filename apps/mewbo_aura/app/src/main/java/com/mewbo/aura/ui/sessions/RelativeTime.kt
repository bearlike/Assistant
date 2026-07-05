package com.mewbo.aura.ui.sessions

import android.text.format.DateUtils
import com.mewbo.aura.data.model.Timestamps
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * ISO-8601 timestamp → short relative string ("5 min ago"). Falls back to the raw string.
 *
 * Parsing itself lives in [Timestamps] (shared with [com.mewbo.aura.data.model.TranscriptReducer]
 * and `voice/AssistTurnMachine` - the backend's numeric-offset ISO format broke `Instant.parse` on
 * Android in all three places before being unified there); this file only adds the
 * epoch-millis/relative-string presentation on top.
 */
internal object RelativeTime {
    fun format(iso: String, nowMillis: Long = System.currentTimeMillis()): String =
        parseEpochMillis(iso)
            ?.let { DateUtils.getRelativeTimeSpanString(it, nowMillis, DateUtils.MINUTE_IN_MILLIS).toString() }
            ?: iso

    /**
     * Split out from [format] so parsing is unit-testable without going through `DateUtils` -
     * that call isn't mocked outside Robolectric/instrumented tests, and this module may not add
     * either (apps/mewbo_aura/CLAUDE.md: build entry point is Gradle only).
     */
    internal fun parseEpochMillis(iso: String): Long? = Timestamps.parseInstantOrNull(iso)?.toEpochMilli()

    /**
     * Search rows' `metaTrailing` date (spec §6.8): exactly "Today" / "Yesterday" / an abbreviated
     * date ("Jun 29"). Built on `java.time` rather than `android.text.format.DateFormat` so the
     * calendar-day logic stays plain-JUnit testable (same "not mocked outside Robolectric"
     * constraint as [format] above) - only the final glyph rendering would need the platform.
     */
    fun formatShort(iso: String, now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault()): String {
        val instant = Timestamps.parseInstantOrNull(iso) ?: return iso
        val date = instant.atZone(zone).toLocalDate()
        val today = now.atZone(zone).toLocalDate()
        return when (date) {
            today -> "Today"
            today.minusDays(1) -> "Yesterday"
            else -> date.format(SHORT_DATE_FORMATTER)
        }
    }

    // Locale.US, not getDefault(): "Today"/"Yesterday" above are already hardcoded English (no
    // i18n anywhere else in the app), and a fixed locale keeps this deterministic across CI hosts.
    private val SHORT_DATE_FORMATTER: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d", Locale.US)
}
