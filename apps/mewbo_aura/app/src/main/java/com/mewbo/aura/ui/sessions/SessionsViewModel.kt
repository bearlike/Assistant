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
 *
 * **The list this VM exposes is a bounded window, not the whole store** (see [filter], and the
 * fetch-limit constant in [com.mewbo.aura.data.repo.SessionRepository]). `SearchChatsScreen` reuses
 * this same VM and filters over the same `sessions` list, so chat search searches that window too —
 * it is a client-side title filter over what the drawer already holds, never its own query.
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
     * Recents scope filter (user directive) — defaults to [RecentsFilter.MOBILE_ONLY] so
     * the rail shows only mobile-created sessions, the analogue of the web console's
     * `DEFAULT_VISIBLE_ORIGINS`. Held here rather than in [SessionsUiState] so it survives a refresh
     * (which replaces the state) and resets to the mobile-only default whenever the host recreates
     * the ViewModel — the desired default either way.
     *
     * **The drawer applies it client-side over a BOUNDED fetch**, and that is the real contract now:
     * [com.mewbo.aura.data.repo.SessionRepository.refreshSessions] caps how many candidates the
     * server examines, so this filter narrows a window rather than the whole store. It can
     * therefore STARVE — if every session in that window came from another surface, the rail reads
     * "No mobile chats yet" while older mobile sessions exist beyond it.
     *
     * That is accepted, for two reasons that are properties of the ordering rather than luck. A
     * session Aura creates is mobile-origin and lands at the HEAD of the server's newest-first
     * ordering, so the window keeps precisely this device's own history; starving needs a whole
     * window's worth of non-mobile sessions all newer than this device's newest. And the bound was
     * sized at the point where mobile yield saturates, so the rail was measured full, not assumed
     * full ([com.mewbo.aura.data.repo.SessionRepository]'s fetch-limit constant carries the
     * numbers). The in-menu escape hatch is [RecentsFilter.ALL], which shows the same fetched
     * window unfiltered — never a second, wider fetch.
     *
     * **Narrowing further belongs on the server, not here.** `GET /api/sessions` filters on
     * `include_archived`/`pinned`/`project` only; there is no `origin` parameter, so a mobile-scoped
     * page would be a backend change. Fetching more and filtering harder client-side is the
     * opposite move — it re-opens the unbounded transfer this bound exists to close.
     */
    private val _filter = MutableStateFlow(RecentsFilter.MOBILE_ONLY)
    val filter: StateFlow<RecentsFilter> = _filter.asStateFlow()

    fun setFilter(filter: RecentsFilter) {
        _filter.value = filter
    }

    init {
        refresh()
    }

    /**
     * Re-fetches the recents window. **Cost: `O(collection)`, bounded** — the drawer calls this on
     * EVERY open (`AuraDrawerContent`'s `LaunchedEffect(isOpen)`), so an unbounded read here is
     * paid per gesture and grows with the store forever.
     */
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

    /**
     * Pin/unpin, same immediate-then-reconcile shape as [archiveSession]:
     * [SessionRepository.setPinned] updates the cached row in place (instant re-section into/out of
     * [SessionSectionHeader.PINNED]), and this [refresh] afterward is server-truth reconciliation.
     */
    fun setPinned(sessionId: String, pinned: Boolean, onResult: (Boolean) -> Unit) {
        viewModelScope.launch {
            val success = sessionRepository.setPinned(sessionId, pinned)
            onResult(success)
            if (success) refresh()
        }
    }
}
