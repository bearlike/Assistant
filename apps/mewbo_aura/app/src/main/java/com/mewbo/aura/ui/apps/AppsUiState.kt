package com.mewbo.aura.ui.apps

import com.mewbo.aura.data.model.AppFreshness
import com.mewbo.aura.data.model.AppSummary

/** Apps gallery screen state (design spec §4D) — mirrors [com.mewbo.aura.ui.sessions.SessionsUiState]. */
sealed interface AppsUiState {
    data object Loading : AppsUiState

    /** [freshness] maps `appId -> its freshness signal` (`null` = never run yet, or the per-app
     * `GET /api/apps/{id}/system` fetch failed — both read the same to a card: nothing to show yet).
     * [offline] = the last refresh failed but a previous fetch is cached — show it + a banner, same
     * idiom as `SessionsUiState.Loaded`. */
    data class Loaded(
        val apps: List<AppSummary>,
        val freshness: Map<String, AppFreshness?> = emptyMap(),
        val offline: Boolean = false,
        val isRefreshing: Boolean = false,
    ) : AppsUiState

    data class Error(val message: String) : AppsUiState
}
