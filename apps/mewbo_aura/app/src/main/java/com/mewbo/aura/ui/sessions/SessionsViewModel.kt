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
 * Recents rail view-state (§8.2). The `@Singleton` [SessionRepository] holds the last-loaded list
 * across this ViewModel's recreation (it is recreated per chat back-stack entry — open-session and
 * new-chat replace the CHAT entry), so BOTH the initial state and every [refresh] render that cache
 * IMMEDIATELY and re-fetch in the background (stale-while-revalidate). The skeleton
 * ([SessionsUiState.Loading]) shows only when the cache is genuinely empty (first launch); a failed
 * background refresh keeps the cached list ([SessionsUiState.Loaded] with `offline = true`), never
 * blanks to [SessionsUiState.Error].
 */
@HiltViewModel
class SessionsViewModel @Inject constructor(
    private val sessionRepository: SessionRepository,
) : ViewModel() {

    // Seed from the shared cache so a freshly-recreated drawer VM renders instantly instead of
    // flashing the skeleton; init's refresh() reconciles against server truth right after.
    private val _uiState = MutableStateFlow<SessionsUiState>(
        sessionRepository.sessions.value
            .takeIf { it.isNotEmpty() }
            ?.let { SessionsUiState.Loaded(it, offline = false) }
            ?: SessionsUiState.Loading,
    )
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
            when {
                // Already rendering a list — keep it on screen, just flag the background refresh.
                current is SessionsUiState.Loaded -> current.copy(isRefreshing = true)
                // Fresh VM but the shared cache still holds the last list: render it immediately
                // (stale-while-revalidate) rather than blanking while the GET round-trips.
                cached.isNotEmpty() -> SessionsUiState.Loaded(cached, offline = false, isRefreshing = true)
                // Genuinely nothing to show yet (first launch) — the skeleton is correct here.
                else -> SessionsUiState.Loading
            }
        }
        viewModelScope.launch {
            // runCatching catches CancellationException too - this
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
