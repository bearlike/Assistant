package com.mewbo.aura.ui.apps

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.model.AppFreshness
import com.mewbo.aura.data.repo.AppRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Apps gallery view-state — mirrors [com.mewbo.aura.ui.sessions.SessionsViewModel]'s
 * stale-while-revalidate shape: seed from the `@Singleton` [AppRepository]'s cache so re-entering
 * the gallery renders instantly, refresh in the background.
 */
@HiltViewModel
class AppsViewModel @Inject constructor(
    private val appRepository: AppRepository,
) : ViewModel() {

    private val _uiState = MutableStateFlow<AppsUiState>(
        appRepository.apps.value
            .takeIf { it.isNotEmpty() }
            ?.let { AppsUiState.Loaded(apps = it) }
            ?: AppsUiState.Loading,
    )
    val uiState: StateFlow<AppsUiState> = _uiState.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        val cached = appRepository.apps.value
        _uiState.update { current ->
            when {
                current is AppsUiState.Loaded -> current.copy(isRefreshing = true)
                cached.isNotEmpty() -> AppsUiState.Loaded(cached, isRefreshing = true)
                else -> AppsUiState.Loading
            }
        }
        viewModelScope.launch {
            runCatching { appRepository.refreshApps() }
                .onFailure { if (it is CancellationException) throw it }
                .onSuccess { apps ->
                    _uiState.value = AppsUiState.Loaded(apps)
                    loadFreshness(apps.map { it.appId })
                }
                .onFailure { error ->
                    _uiState.value = if (cached.isNotEmpty()) {
                        AppsUiState.Loaded(cached, offline = true)
                    } else {
                        AppsUiState.Error(error.message ?: "Couldn't load apps")
                    }
                }
        }
    }

    /**
     * Fans `GET /api/apps/{id}/system` out per gallery card (task brief: gallery cards show
     * freshness; spec §2.6 has the SAME endpoint back both this and the detail health row) — a
     * separate pass AFTER the list lands so the gallery paints immediately and freshness fills in as
     * each app's own request resolves, rather than blocking the whole list on the slowest app's
     * round-trip.
     */
    private suspend fun loadFreshness(appIds: List<String>) {
        val freshness: Map<String, AppFreshness?> = coroutineScope {
            appIds.map { id -> async { id to appRepository.systemHealth(id)?.freshness } }.awaitAll()
        }.toMap()
        _uiState.update { current ->
            (current as? AppsUiState.Loaded)?.copy(freshness = current.freshness + freshness) ?: current
        }
    }
}
