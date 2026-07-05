package com.mewbo.aura.ui.sessions

import com.mewbo.aura.data.model.SessionSummary

/** Sessions-list screen state (§8.2). */
sealed interface SessionsUiState {
    data object Loading : SessionsUiState

    /** [offline] = the last refresh failed but a previous fetch is cached — show it + a banner. */
    data class Loaded(
        val sessions: List<SessionSummary>,
        val offline: Boolean,
        val isRefreshing: Boolean = false,
    ) : SessionsUiState

    data class Error(val message: String) : SessionsUiState
}
