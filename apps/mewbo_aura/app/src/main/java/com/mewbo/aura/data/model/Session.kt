package com.mewbo.aura.data.model

/** Domain-facing session summary (mapped from [com.mewbo.aura.data.api.SessionSummaryDto]). */
data class SessionSummary(
    val sessionId: String,
    val title: String?,
    val status: String,
    /** Drawer running-dot gate (spec §6.7) — see [com.mewbo.aura.data.api.SessionSummaryDto.running]. */
    val running: Boolean,
    val doneReason: String?,
    val origin: String?,
    val recoverable: Boolean,
    val createdAt: String,
    val updatedAt: String,
)

/** Domain-facing transcript + status snapshot (mapped from `GET /api/sessions/{id}/events`). */
data class SessionHistory(
    val sessionId: String,
    val events: List<SessionEvent>,
    val running: Boolean,
    val status: String?,
    val doneReason: String?,
    val title: String?,
    val recoverable: Boolean,
)
