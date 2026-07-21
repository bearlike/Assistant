package com.mewbo.aura.ui.apps

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.repo.AppRepository
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.SessionScopeRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * The "New app" creation flow (design spec §5 flow 1): intent text + own/shared workspace choice →
 * `POST /api/apps` → follow the returned builder session live for the terminal `app_ready` event
 * ([SessionEvent.AppReady], mirroring how [SessionEvent.WidgetReady] terminates a widget build) →
 * navigate to the new app's detail screen.
 */
@HiltViewModel
class AppCreateViewModel @Inject constructor(
    private val appRepository: AppRepository,
    private val runRepository: RunRepository,
    private val sessionScopeRepository: SessionScopeRepository,
) : ViewModel() {

    private val _phase = MutableStateFlow<AppCreatePhase>(AppCreatePhase.Composing)
    val phase: StateFlow<AppCreatePhase> = _phase.asStateFlow()

    private val _projects = MutableStateFlow<List<ProjectSummary>?>(null)
    val projects: StateFlow<List<ProjectSummary>?> = _projects.asStateFlow()

    /** Lazy-loaded on first "Shared workspace" selection — mirrors `SettingsScreen`'s
     * `loadProjectsIfNeeded` gate (no point fetching the catalog for a form most users submit as
     * "own workspace"). */
    fun loadProjectsIfNeeded() {
        if (_projects.value != null) return
        viewModelScope.launch {
            _projects.value = sessionScopeRepository.projects() ?: emptyList()
        }
    }

    fun submit(intent: String, workspace: AppWorkspaceChoice) {
        if (intent.isBlank() || _phase.value !is AppCreatePhase.Composing) return
        _phase.value = AppCreatePhase.Submitting
        viewModelScope.launch {
            val (kind, key) = when (workspace) {
                is AppWorkspaceChoice.Own -> "own" to null
                is AppWorkspaceChoice.Shared -> "shared" to workspace.project.contextKey
            }
            val result = appRepository.createApp(intent = intent, workspaceKind = kind, workspaceKey = key)
            if (result == null) {
                _phase.value = AppCreatePhase.Failed("Couldn't start the build. Try again.")
                return@launch
            }
            val sessionId = result.sessionId
            if (sessionId == null) {
                // No builder session to follow (defensive — the backend always mints one). A
                // freshly created app is building; the gallery reflects it once the build lands.
                _phase.value = AppCreatePhase.Building(null)
                return@launch
            }
            followBuild(sessionId)
        }
    }

    /** Retry after a [AppCreatePhase.Failed] outcome — back to the editable form, keeping whatever
     * intent/workspace the caller re-submits. */
    fun resetToComposing() {
        _phase.value = AppCreatePhase.Composing
    }

    /**
     * Subscribes to the SAME multicast SSE seam chat/the overlay/the notification watcher already
     * share ([RunRepository.live] — never a second connection) and watches for the build's terminal
     * signal. [SessionEvent.SubAgent]/[SessionEvent.Todos] narration is best-effort UI polish, not a
     * contract this depends on — an app with neither still resolves correctly on
     * [SessionEvent.AppReady] or a failed [SessionEvent.Completion].
     */
    private suspend fun followBuild(sessionId: String) {
        _phase.value = AppCreatePhase.Building(null)
        runRepository.live(sessionId).collect { event ->
            when (event) {
                is SessionEvent.AppReady -> _phase.value = AppCreatePhase.Ready(event.payload.appId)
                is SessionEvent.SubAgent -> event.payload.detail
                    ?.takeIf { it.isNotBlank() }
                    ?.let { _phase.value = AppCreatePhase.Building(it) }
                is SessionEvent.Todos -> event.payload.items.lastOrNull { it.status != "done" }?.label
                    ?.let { _phase.value = AppCreatePhase.Building(it) }
                is SessionEvent.Completion -> event.payload.error?.let { _phase.value = AppCreatePhase.Failed(it) }
                is SessionEvent.StreamError -> _phase.value = AppCreatePhase.Failed(event.message)
                else -> Unit
            }
        }
        // The stream ended (stream_end) without ever seeing app_ready or an error — a safety net so
        // the screen never spins forever if the build finished silently.
        if (_phase.value is AppCreatePhase.Building) {
            _phase.value = AppCreatePhase.Failed("The build finished without producing an app.")
        }
    }
}
