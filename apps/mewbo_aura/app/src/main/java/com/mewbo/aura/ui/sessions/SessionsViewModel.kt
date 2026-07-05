package com.mewbo.aura.ui.sessions

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.repo.SessionRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Sessions list (§8.2): refreshes on start and on pull-to-refresh. [SessionRepository] already
 * caches the last-fetched list, so a failed refresh with a non-empty cache degrades to
 * [SessionsUiState.Loaded] with `offline = true` rather than [SessionsUiState.Error].
 */
@HiltViewModel
class SessionsViewModel @Inject constructor(
    private val sessionRepository: SessionRepository,
) : ViewModel() {

    private val _uiState = MutableStateFlow<SessionsUiState>(SessionsUiState.Loading)
    val uiState: StateFlow<SessionsUiState> = _uiState.asStateFlow()

    /**
     * Recents scope filter (user directive 2026-07-03) — defaults to [RecentsFilter.MOBILE_ONLY] so
     * the rail shows only mobile-created sessions, the analogue of the web console's
     * `DEFAULT_VISIBLE_ORIGINS`. Held here rather than in [SessionsUiState] so it survives a refresh
     * (which replaces the state) and resets to the mobile-only default whenever the host recreates
     * the ViewModel — the desired default either way. The drawer applies it client-side over the
     * full fetched list (`GET /api/sessions` returns everything, so the filter can't starve).
     */
    private val _filter = MutableStateFlow(RecentsFilter.MOBILE_ONLY)
    val filter: StateFlow<RecentsFilter> = _filter.asStateFlow()

    fun setFilter(filter: RecentsFilter) {
        _filter.value = filter
    }

    init {
        refresh()
    }

    fun refresh() {
        val cached = sessionRepository.sessions.value
        _uiState.update { current ->
            if (current is SessionsUiState.Loaded) current.copy(isRefreshing = true) else SessionsUiState.Loading
        }
        viewModelScope.launch {
            // Gitea #181 fix wave, finding 2: runCatching catches CancellationException too - this
            // ViewModel is created fresh per screen (drawer/search host) and can be torn down
            // mid-fetch, so a cancelled coroutine here must actually stop instead of writing a
            // "couldn't load" state past its own cancellation point.
            runCatching { sessionRepository.refreshSessions() }
                .onFailure { if (it is CancellationException) throw it }
                .onSuccess { sessions -> _uiState.value = SessionsUiState.Loaded(sessions, offline = false) }
                .onFailure { error ->
                    _uiState.value = if (cached.isNotEmpty()) {
                        SessionsUiState.Loaded(cached, offline = true)
                    } else {
                        SessionsUiState.Error(error.message ?: "Couldn't load sessions")
                    }
                }
        }
    }

    /**
     * Long-press session-actions sheet (drawer, `AuraDrawerContent`): [sessionRepository] already
     * mutates its cached list in place on success (rename updates the title, archive removes the
     * row), so the UI is instant — [refresh] afterward is server-truth reconciliation, not the
     * source of the immediate update.
     */
    fun renameSession(sessionId: String, title: String, onResult: (Boolean) -> Unit) {
        viewModelScope.launch {
            val success = sessionRepository.renameSession(sessionId, title)
            onResult(success)
            if (success) refresh()
        }
    }

    fun archiveSession(sessionId: String, onResult: (Boolean) -> Unit) {
        viewModelScope.launch {
            val success = sessionRepository.archiveSession(sessionId)
            onResult(success)
            if (success) refresh()
        }
    }
}
