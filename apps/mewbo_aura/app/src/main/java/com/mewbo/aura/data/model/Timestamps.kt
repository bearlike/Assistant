package com.mewbo.aura.data.model

import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * ONE atomic home for offset-aware ISO-8601 parsing, shared by every ts-comparison call site
 * ([TranscriptReducer]'s optimistic-echo window, `ui/sessions/RelativeTime`'s session-list
 * display, `voice/AssistTurnMachine`'s recent-session lookup). The backend emits Python
 * `datetime.isoformat()`: microsecond precision plus an explicit numeric offset, e.g.
 * `2026-07-02T03:34:42.633140+00:00`. `Instant.parse` (`ISO_INSTANT`) only accepts a literal `Z`
 * and throws on that numeric-offset form on Android's bundled `java.time` - desktop JVMs are more
 * lenient about it, which is exactly why this shipped as three separate, silently-broken copies
 * before being unified here (see data/CLAUDE.md and the final-review history on #175).
 *
 * `OffsetDateTime.parse` accepts both `Z` and a numeric offset via the same
 * `ISO_OFFSET_DATE_TIME` formatter, so it's tried first; `Instant.parse` is a fallback for a bare
 * instant string with no offset at all. Anything that parses as neither returns `null` - callers
 * must treat that as "can't compare", never throw.
 */
object Timestamps {
    fun parseInstantOrNull(ts: String?): Instant? {
        if (ts.isNullOrEmpty()) return null
        return try {
            OffsetDateTime.parse(ts).toInstant()
        } catch (e: DateTimeParseException) {
            try {
                Instant.parse(ts)
            } catch (e2: DateTimeParseException) {
                null
            }
        }
    }
}
