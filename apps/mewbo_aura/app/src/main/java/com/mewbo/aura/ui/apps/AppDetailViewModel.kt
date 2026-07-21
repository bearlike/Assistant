package com.mewbo.aura.ui.apps

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.repo.AppRepository
import com.mewbo.aura.data.settings.SettingsStore
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * App detail view-state: full spec (for the WebView), a freshly-minted render token, the latest
 * pipeline run (freshness) and the app's triggers (pause/resume) — all fetched in parallel once
 * [detail] resolves, same fan-out shape [AppsViewModel.loadFreshness] uses for the gallery.
 */
@HiltViewModel
class AppDetailViewModel @Inject constructor(
    private val appRepository: AppRepository,
    private val settingsStore: SettingsStore,
    savedStateHandle: SavedStateHandle,
) : ViewModel() {

    private val appId: String =
        checkNotNull(savedStateHandle[APP_ID_ARG]) { "AppDetailViewModel requires a `$APP_ID_ARG` nav arg" }

    private val _uiState = MutableStateFlow<AppDetailUiState>(AppDetailUiState.Loading)
    val uiState: StateFlow<AppDetailUiState> = _uiState.asStateFlow()

    /** Resolved once and cached for [AppWebView]'s `app_context.api_base` — the SAME
     * [SettingsStore.baseUrl] `AuthInterceptor` reads, so the injected SDK talks to the identical
     * backend the WebView's own asset requests resolve against. */
    var apiBase: String = ""
        private set

    init {
        refresh()
    }

    fun refresh() {
        _uiState.update { current ->
            if (current is AppDetailUiState.Loaded) current.copy(isRefreshing = true) else AppDetailUiState.Loading
        }
        viewModelScope.launch {
            apiBase = settingsStore.baseUrl.first()
            val detail = appRepository.fetchDetail(appId)
            if (detail == null) {
                _uiState.value = AppDetailUiState.Error("Couldn't load this app")
                return@launch
            }
            coroutineScope {
                val tokenDeferred = async { appRepository.mintToken(appId) }
                val healthDeferred = async { appRepository.systemHealth(appId) }
                _uiState.value = AppDetailUiState.Loaded(
                    detail = detail,
                    token = tokenDeferred.await(),
                    health = healthDeferred.await(),
                )
            }
        }
    }

    /** Pause/resume a trigger from the health row — reuses the existing global trigger mutation
     * (see [AppRepository.setTriggerPaused]'s KDoc). Reconciles against server truth afterward
     * rather than optimistically patching the row, matching [AppRepository]'s degrade-then-refresh
     * idiom elsewhere in this feature. */
    fun setTriggerPaused(triggerId: String, paused: Boolean) {
        viewModelScope.launch {
            if (appRepository.setTriggerPaused(triggerId, paused)) refresh()
        }
    }

    companion object {
        const val APP_ID_ARG = "appId"
    }
}
