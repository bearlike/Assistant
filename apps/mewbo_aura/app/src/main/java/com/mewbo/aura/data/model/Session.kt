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
    /** Hard-termination signal — a permanently terminated session is a dead-end
     * (`recoverable` is always false alongside it). See [com.mewbo.aura.data.api.SessionSummaryDto.terminated]. */
    val terminated: Boolean = false,
    val terminatedAt: String? = null,
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
    /** Hard-termination signal — when true, [com.mewbo.aura.ui.chat.ChatViewModel.bind]
     * opens the session into its terminal state (composer disabled, no Retry). */
    val terminated: Boolean = false,
    val terminatedAt: String? = null,
)
