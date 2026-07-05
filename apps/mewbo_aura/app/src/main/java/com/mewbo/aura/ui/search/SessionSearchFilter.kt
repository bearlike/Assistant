package com.mewbo.aura.ui.search

import com.mewbo.aura.data.model.SessionSummary

/**
 * Client-side filter over the already-cached session list (spec §6.8: "backed by E2 list filtered
 * client-side in v1"). Pure so it's unit-testable without Compose/Android - the screen just calls
 * [filter] on every keystroke.
 */
internal object SessionSearchFilter {
    fun filter(sessions: List<SessionSummary>, query: String): List<SessionSummary> {
        val trimmed = query.trim()
        if (trimmed.isEmpty()) return sessions
        return sessions.filter { it.title?.contains(trimmed, ignoreCase = true) == true }
    }
}
