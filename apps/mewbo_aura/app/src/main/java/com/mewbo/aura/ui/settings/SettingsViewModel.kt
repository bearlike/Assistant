package com.mewbo.aura.ui.settings

import android.Manifest
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.repo.ConnectionProbe
import com.mewbo.aura.data.repo.SessionScopeRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.mock.MockBackendFlags
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/** Settings (§8.4): one combined [SettingsUiState] over [SettingsStore]'s DataStore-backed flows. */
@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val settingsStore: SettingsStore,
    private val connectionProbe: ConnectionProbe,
    private val sessionScopeRepository: SessionScopeRepository,
    private val devicePermissionChecker: DevicePermissionChecker,
    private val mockBackendFlags: MockBackendFlags,
) : ViewModel() {

    private val validating = MutableStateFlow(false)
    private val connectionError = MutableStateFlow<String?>(null)

    /** Default-project picker's catalog (Gitea #178 W1-A) - `null` until [loadProjectsIfNeeded]
     * resolves it on the picker sheet's first open, mirroring [com.mewbo.aura.ui.chat.ChatViewModel
     * .loadModelsIfNeeded]'s no-cache-but-remember-success shape. Local state, not a store flow -
     * the catalog itself is never persisted. */
    private val projects = MutableStateFlow<List<ProjectSummary>?>(null)

    /** READ_SMS + SEND_SMS granted state (Gitea #179 Phase 4) - a plain permission read has no
     * natural Flow source, so this is manually refreshed ([refreshSmsAccessStatus]) rather than
     * derived from a [SettingsStore] flow like everything else here. */
    private val smsAccessGranted = MutableStateFlow(false)

    // 10 flows exceed kotlinx.coroutines' named-arg combine() overloads (max 5) - nested
    // Triple/Pair combines instead of the untyped vararg form, same pattern as the pre-design-v2
    // theme+motion combine this replaced. The third/fourth groups are local state, not store flows.
    val uiState: StateFlow<SettingsUiState> = combine(
        combine(settingsStore.baseUrl, settingsStore.apiKey, settingsStore.speakResponses, ::Triple),
        combine(settingsStore.reducedMotion, settingsStore.voiceUseFakes, settingsStore.displayName, ::Triple),
        combine(validating, connectionError, ::Pair),
        combine(settingsStore.selectedProject, projects, ::Pair),
        combine(smsAccessGranted, mockBackendFlags.enabled, ::Pair),
    ) { (baseUrl, apiKey, speakResponses), (reducedMotion, voiceUseFakes, displayName), (isValidating, error), (selectedProject, projects), (smsGranted, mockBackendEnabled) ->
        SettingsUiState(
            baseUrl = baseUrl,
            apiKey = apiKey,
            speakResponses = speakResponses,
            reducedMotion = reducedMotion,
            voiceUseFakes = voiceUseFakes,
            mockBackendEnabled = mockBackendEnabled,
            displayName = displayName,
            validating = isValidating,
            connectionError = error,
            selectedProject = selectedProject,
            projects = projects,
            smsAccessGranted = smsGranted,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SettingsUiState())

    fun setSpeakResponses(value: Boolean) = viewModelScope.launch { settingsStore.setSpeakResponses(value) }

    fun setReducedMotion(value: Boolean) = viewModelScope.launch { settingsStore.setReducedMotion(value) }

    fun setVoiceUseFakes(value: Boolean) = viewModelScope.launch { settingsStore.setVoiceUseFakes(value) }

    fun setMockBackendEnabled(value: Boolean) = viewModelScope.launch { mockBackendFlags.setEnabled(value) }

    fun setDisplayName(value: String) = viewModelScope.launch { settingsStore.setDisplayName(value) }

    /** Re-reads granted state off [devicePermissionChecker] - called on the Settings screen's own
     * composition and again after the permission-request dialog's own result, since there's no
     * Flow to observe (task brief: "the catalog's permission-gated enumeration picks the grant up
     * automatically on the next /query - no other wiring" - this is purely the row's own display). */
    fun refreshSmsAccessStatus() {
        smsAccessGranted.value = devicePermissionChecker.isGranted(Manifest.permission.READ_SMS) &&
            devicePermissionChecker.isGranted(Manifest.permission.SEND_SMS)
    }

    /** Default-project picker's selection (Gitea #178 W1-A) - persists immediately, NOT part of
     * "Validate & save" (that pill only guards the connection fields). */
    fun setSelectedProject(contextKey: String) = viewModelScope.launch { settingsStore.setSelectedProject(contextKey) }

    /** Loads [projects] once (retried on each picker-sheet open while still `null`) - a
     * failed/offline fetch calls [onNotice] and leaves the catalog untouched, same shape as
     * [com.mewbo.aura.ui.chat.ChatViewModel.loadModelsIfNeeded]. */
    fun loadProjectsIfNeeded(onNotice: (String) -> Unit = {}) {
        if (projects.value != null) return
        viewModelScope.launch {
            val catalog = sessionScopeRepository.projects()
            if (catalog != null) {
                projects.value = catalog
            } else {
                onNotice("Couldn't load projects")
            }
        }
    }

    /**
     * Probes [baseUrl]/[apiKey] live BEFORE persisting either. Success clears any prior error,
     * persists both via [SettingsStore], and calls [onConnected] with the model count so the
     * screen can fire its own [com.mewbo.aura.ui.common.NoticeController] toast (that controller
     * is a CompositionLocal owned by `ui/navigation`, out of this task's ownership - the screen,
     * not the ViewModel, is the one holding a reference to it). Failure leaves the store untouched
     * and surfaces a quiet [SettingsUiState.connectionError] reason instead.
     */
    fun validateAndSave(baseUrl: String, apiKey: String, onConnected: (modelCount: Int) -> Unit) {
        viewModelScope.launch {
            validating.value = true
            when (val result = connectionProbe.validate(baseUrl, apiKey)) {
                is ConnectionProbe.Result.Ok -> {
                    settingsStore.setBaseUrl(baseUrl)
                    settingsStore.setApiKey(apiKey)
                    connectionError.value = null
                    onConnected(result.modelCount)
                }
                is ConnectionProbe.Result.Http -> connectionError.value = httpErrorReason(result.code)
                is ConnectionProbe.Result.Unreachable -> connectionError.value = result.reason
            }
            validating.value = false
        }
    }

    /** §6.12 error row's "Save anyway" escape hatch: persist the draft as-is, skipping the probe. */
    fun saveAnyway(baseUrl: String, apiKey: String) = viewModelScope.launch {
        settingsStore.setBaseUrl(baseUrl)
        settingsStore.setApiKey(apiKey)
        connectionError.value = null
    }

    /** Clears a stale failure once the user starts editing the fields again. */
    fun dismissConnectionError() {
        connectionError.value = null
    }

    private fun httpErrorReason(code: Int): String =
        if (code == 401 || code == 403) "Invalid API key" else "Server returned $code"
}
