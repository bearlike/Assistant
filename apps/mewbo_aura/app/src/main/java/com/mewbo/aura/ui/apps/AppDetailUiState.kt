package com.mewbo.aura.ui.apps

import com.mewbo.aura.data.model.AppDetail
import com.mewbo.aura.data.model.AppSystemHealth
import com.mewbo.aura.data.model.AppToken

/** App detail screen state (design spec §4D: WebView render + minimal health row). */
sealed interface AppDetailUiState {
    data object Loading : AppDetailUiState

    /** [token] is nullable — a mint failure degrades to an unauthenticated WebView post rather than
     * blocking the whole screen; the injected SDK surfaces its own "refresh the app" state on an
     * invalid/expired token (spec §6), so this mirrors that same non-fatal contract. [health] is
     * likewise nullable on its own fetch failure — the row degrades to just the status dot with no
     * freshness/trigger detail rather than blocking the WebView render, which is the actual point of
     * this screen. */
    data class Loaded(
        val detail: AppDetail,
        val token: AppToken?,
        val health: AppSystemHealth?,
        val isRefreshing: Boolean = false,
    ) : AppDetailUiState

    data class Error(val message: String) : AppDetailUiState
}
