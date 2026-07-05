package com.mewbo.aura.ui.sessions

import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.data.model.Timestamps
import java.time.Instant
import java.time.ZoneId

/** Drawer/recents section header, in fixed display order. */
enum class SessionSectionHeader(val label: String) {
    TODAY("Today"),
    PREVIOUS_7_DAYS("Previous 7 days"),
    OLDER("Older"),
}

/** One non-empty bucket of sessions under a [SessionSectionHeader], input order preserved. */
data class SessionSection(val header: SessionSectionHeader, val sessions: List<SessionSummary>)

/**
 * Buckets [SessionSummary] rows into calendar-day sections for the drawer/recents list.
 *
 * Recency timestamp per session is `updatedAt` if non-blank else `createdAt`, parsed via
 * [Timestamps.parseInstantOrNull] — the ONE shared ISO parser every other ts-comparison call site
 * in this codebase (`TranscriptReducer`, `ui/sessions/RelativeTime`, `AssistTurnMachine`) routes
 * through, precisely because a naive/local `Instant.parse` accepts numeric UTC offsets on desktop
 * JVMs but throws on Android's bundled `java.time` (see `data/model/Timestamps.kt`,
 * `app/src/test/.../CLAUDE.md`). An unparseable or blank timestamp must never crash bucketing — it
 * sorts as [SessionSectionHeader.OLDER].
 */
object SessionGrouping {
    fun group(sessions: List<SessionSummary>, now: Instant, zone: ZoneId): List<SessionSection> {
        val today = now.atZone(zone).toLocalDate()
        val previousWindowStart = today.minusDays(7)

        val buckets = linkedMapOf(
            SessionSectionHeader.TODAY to mutableListOf<SessionSummary>(),
            SessionSectionHeader.PREVIOUS_7_DAYS to mutableListOf(),
            SessionSectionHeader.OLDER to mutableListOf(),
        )

        for (session in sessions) {
            val recencyIso = session.updatedAt.ifBlank { session.createdAt }
            val instant = Timestamps.parseInstantOrNull(recencyIso)
            val header = when {
                instant == null -> SessionSectionHeader.OLDER
                else -> {
                    val date = instant.atZone(zone).toLocalDate()
                    when {
                        date == today -> SessionSectionHeader.TODAY
                        date >= previousWindowStart && date < today -> SessionSectionHeader.PREVIOUS_7_DAYS
                        else -> SessionSectionHeader.OLDER
                    }
                }
            }
            buckets.getValue(header).add(session)
        }

        return buckets
            .filterValues { it.isNotEmpty() }
            .map { (header, list) -> SessionSection(header, list) }
    }
}
