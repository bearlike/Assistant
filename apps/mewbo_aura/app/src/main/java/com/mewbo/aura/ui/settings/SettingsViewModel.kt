package com.mewbo.aura.ui.settings

import android.Manifest
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.repo.ConnectionProbe
import com.mewbo.aura.data.repo.ModelRepository
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
    private val modelRepository: ModelRepository,
    private val devicePermissionChecker: DevicePermissionChecker,
    private val mockBackendFlags: MockBackendFlags,
) : ViewModel() {

    private val validating = MutableStateFlow(false)
    private val connectionError = MutableStateFlow<String?>(null)

    /** Default-project picker's catalog - `null` until [loadProjectsIfNeeded]
     * resolves it on the picker sheet's first open, mirroring [com.mewbo.aura.ui.chat.ChatViewModel
     * .loadModelsIfNeeded]'s no-cache-but-remember-success shape. Local state, not a store flow -
     * the catalog itself is never persisted. */
    private val projects = MutableStateFlow<List<ProjectSummary>?>(null)

    /** Model catalog for the two default-model rows, loaded the SAME lazy way as
     * [projects] - `null` until [loadModelsIfNeeded] on a model picker's first open. Local state,
     * never persisted (the SELECTIONS persist via [SettingsStore]; the catalog is fetched fresh). */
    private val models = MutableStateFlow<ModelCatalog?>(null)

    /** READ_SMS + SEND_SMS granted state - a plain permission read has no
     * natural Flow source, so this is manually refreshed ([refreshSmsAccessStatus]) rather than
     * derived from a [SettingsStore] flow like everything else here. */
    private val smsAccessGranted = MutableStateFlow(false)

    // 17 flows far exceed kotlinx.coroutines' named-arg combine() overloads (max 5), so the state is
    // assembled from four domain sub-flows (each an inner combine of <=5, folded into a typed holder)
    // combined once at the top - readable and testable, and it keeps every group under the arity cap
    // as settings grow. Holders are private, below. `models`/`projects`/local MutableStateFlows are
    // ordinary flows here, combined the same as the store's own.
    private val connectionState = combine(
        settingsStore.baseUrl, settingsStore.apiKey, validating, connectionError, ::ConnectionState,
    )
    private val preferenceState = combine(
        settingsStore.speakResponses, settingsStore.reducedMotion, settingsStore.voiceUseFakes,
        settingsStore.displayName, mockBackendFlags.enabled, ::PreferenceState,
    )
    private val scopeState = combine(
        settingsStore.selectedProject, projects, models, settingsStore.selectedModel,
        settingsStore.overlayDefaultModel, ::ScopeState,
    )
    private val capabilityState = combine(
        smsAccessGranted, settingsStore.disabledDeviceToolIds, settingsStore.streamlitWidgetsEnabled, ::CapabilityState,
    )

    val uiState: StateFlow<SettingsUiState> = combine(
        connectionState, preferenceState, scopeState, capabilityState,
    ) { conn, prefs, scope, caps ->
        SettingsUiState(
            baseUrl = conn.baseUrl,
            apiKey = conn.apiKey,
            validating = conn.validating,
            connectionError = conn.connectionError,
            speakResponses = prefs.speakResponses,
            reducedMotion = prefs.reducedMotion,
            voiceUseFakes = prefs.voiceUseFakes,
            displayName = prefs.displayName,
            mockBackendEnabled = prefs.mockBackendEnabled,
            selectedProject = scope.selectedProject,
            projects = scope.projects,
            models = scope.models,
            appDefaultModel = scope.appDefaultModel,
            overlayDefaultModel = scope.overlayDefaultModel,
            smsAccessGranted = caps.smsAccessGranted,
            disabledDeviceToolIds = caps.disabledDeviceToolIds,
            streamlitWidgetsEnabled = caps.streamlitWidgetsEnabled,
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

    /** Default-project picker's selection - persists immediately, NOT part of
     * "Validate & save" (that pill only guards the connection fields). */
    fun setSelectedProject(contextKey: String) = viewModelScope.launch { settingsStore.setSelectedProject(contextKey) }

    /** "Default model — app" selection - the SAME store key the chat top-bar
     * picker writes (`SettingsStore.selectedModel`), so the two editing surfaces stay in sync. */
    fun setAppDefaultModel(id: String?) = viewModelScope.launch { settingsStore.setSelectedModel(id ?: "") }

    /** "Default model — assistant overlay" selection - independent of the app
     * default; read at the overlay's own session-creation seam (`AuraSession`). */
    fun setOverlayDefaultModel(id: String?) = viewModelScope.launch { settingsStore.setOverlayDefaultModel(id ?: "") }

    /** One device-tool toggle - persists the disabled-set delta; the catalog and
     * executor pick the change up on the NEXT `/query` advertisement and dispatch (no other wiring). */
    fun setDeviceToolEnabled(toolId: String, enabled: Boolean) =
        viewModelScope.launch { settingsStore.setDeviceToolEnabled(toolId, enabled) }

    /** "Streamlit widgets" experimental toggle. */
    fun setStreamlitWidgetsEnabled(value: Boolean) = viewModelScope.launch { settingsStore.setStreamlitWidgetsEnabled(value) }

    /** Loads the model catalog once (retried on each model-picker open while still `null`), same
     * shape as [loadProjectsIfNeeded] - a failed/offline fetch calls [onNotice] and leaves the
     * catalog untouched (the rows degrade to the raw stored id via [resolveModelDisplayName]). */
    fun loadModelsIfNeeded(onNotice: (String) -> Unit = {}) {
        if (models.value != null) return
        viewModelScope.launch {
            val catalog = modelRepository.catalog()
            if (catalog != null) models.value = catalog else onNotice("Couldn't load models")
        }
    }

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

    // Typed carriers for the four domain sub-flows (above) - one per <=5-flow combine group, so the
    // top-level combine stays under the arity cap and each group destructures by name, not position.
    private data class ConnectionState(
        val baseUrl: String,
        val apiKey: String?,
        val validating: Boolean,
        val connectionError: String?,
    )

    private data class PreferenceState(
        val speakResponses: Boolean,
        val reducedMotion: Boolean,
        val voiceUseFakes: Boolean,
        val displayName: String,
        val mockBackendEnabled: Boolean,
    )

    private data class ScopeState(
        val selectedProject: String,
        val projects: List<ProjectSummary>?,
        val models: ModelCatalog?,
        val appDefaultModel: String,
        val overlayDefaultModel: String,
    )

    private data class CapabilityState(
        val smsAccessGranted: Boolean,
        val disabledDeviceToolIds: Set<String>,
        val streamlitWidgetsEnabled: Boolean,
    )
}
