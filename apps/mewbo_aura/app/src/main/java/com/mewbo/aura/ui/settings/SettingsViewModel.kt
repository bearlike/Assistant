package com.mewbo.aura.ui.settings

import android.Manifest
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.OverlayProvisioning
import com.mewbo.aura.data.device.TelevisionChecker
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.OverlayGrantOutcome
import com.mewbo.aura.data.device.shizuku.ShizukuDeviceControl
import com.mewbo.aura.data.device.shizuku.ShizukuOverlayGrant
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.repo.ConnectionProbe
import com.mewbo.aura.data.repo.ModelRepository
import com.mewbo.aura.data.repo.SessionScopeRepository
import com.mewbo.aura.data.repo.SpeechRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.mock.MockBackendFlags
import com.mewbo.aura.voice.SpeechBoostState
import com.mewbo.aura.voice.SpeechVolumeBoost
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import com.mewbo.aura.ui.control.DeviceControlOverlay

/** Settings (§8.4): one combined [SettingsUiState] over [SettingsStore]'s DataStore-backed flows. */
@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val settingsStore: SettingsStore,
    private val connectionProbe: ConnectionProbe,
    private val sessionScopeRepository: SessionScopeRepository,
    private val modelRepository: ModelRepository,
    private val speechRepository: SpeechRepository,
    private val devicePermissionChecker: DevicePermissionChecker,
    private val televisionChecker: TelevisionChecker,
    private val deviceControl: ShizukuDeviceControl,
    private val shizukuOverlayGrant: ShizukuOverlayGrant,
    private val mockBackendFlags: MockBackendFlags,
    private val deviceControlOverlay: DeviceControlOverlay,
    private val speechVolumeBoost: SpeechVolumeBoost,
    private val assistantRoleReader: AssistantRoleReader,
    private val overlayPermissionReader: OverlayPermissionReader,
    private val notificationPermissionReader: NotificationPermissionReader,
) : ViewModel() {

    private val validating = MutableStateFlow(false)
    private val connectionError = MutableStateFlow<String?>(null)

    /** Whether the STORED credentials reach a server. Nothing persists a validated flag, so the
     * only honest opening state is "not checked" — [checkStoredConnection] replaces it with a
     * measured one. */
    private val connectionStatus = MutableStateFlow<ConnectionStatus>(ConnectionStatus.Unchecked)

    /** Guards [checkStoredConnection] so a recomposition cannot re-probe. */
    private var connectionChecked = false

    /**
     * Whether this device is a television — read ONCE, not at [refreshSystemPermissions].
     *
     * The form factor is a per-boot fact and cannot change mid-process, so a single `O(1)` read at
     * construction is the honest answer rather than re-reading it with the grants.
     * [TelevisionChecker] points at why the assist-role surfaces hide on it.
     */
    private val isTelevision = televisionChecker.isTelevision()

    /**
     * Every state Android owns and pushes no updates for, read together and held as ONE value.
     *
     * All four are changed in another app — a permission dialog, the assistant picker, the
     * "Display over other apps" screen — so none of them has a Flow to observe and every one is a
     * manual re-read at the same moment: [refreshSystemPermissions] on resume. Holding them as one
     * value rather than four flows is what keeps both `combine` groups below the five-source
     * arity cap, and it makes a refresh a single emission instead of a burst of four.
     */
    private val systemPermissions = MutableStateFlow(SystemPermissions())

    /** Default-project picker's catalog - `null` until [loadProjectsIfNeeded]
     * resolves it on the picker sheet's first open, mirroring [com.mewbo.aura.ui.chat.ChatViewModel
     * .loadModelsIfNeeded]'s no-cache-but-remember-success shape. Local state, not a store flow -
     * the catalog itself is never persisted. */
    private val projects = MutableStateFlow<List<ProjectSummary>?>(null)

    /** Model catalog for the two default-model rows, loaded the SAME lazy way as
     * [projects] - `null` until [loadModelsIfNeeded] on a model picker's first open. Local state,
     * never persisted (the SELECTIONS persist via [SettingsStore]; the catalog is fetched fresh). */
    private val models = MutableStateFlow<ModelCatalog?>(null)

    /** Server speech engines for the two Voice & Motion rows, loaded the SAME lazy way as [models]
     * — `null` until [loadSpeechEnginesIfNeeded] on a speech picker's first open. Never persisted;
     * only the SELECTION is. */
    private val speechEngines = MutableStateFlow<SpeechCatalog?>(null)

    /** Whether device control is usable, and if not, WHY — observed, not polled.
     * The Shizuku binder arrives asynchronously after app start, so a one-shot
     * read reports "not running" for a service that is running and never
     * corrects itself. [ShizukuDeviceControl] pushes; this just forwards. */
    private val deviceControlStatus = deviceControl.status

    // 17 flows far exceed kotlinx.coroutines' named-arg combine() overloads (max 5), so the state is
    // assembled from four domain sub-flows (each an inner combine of <=5, folded into a typed holder)
    // combined once at the top - readable and testable, and it keeps every group under the arity cap
    // as settings grow. Holders are private, below. `models`/`projects`/local MutableStateFlows are
    // ordinary flows here, combined the same as the store's own.
    //
    // Both groups had reached exactly five when the overlay permission needed a home, and a sixth
    // source does not compile. The cure is to group by WHAT ASKS rather than by what is displayed:
    // the manual OS reads travel together in `systemPermissions` because one call refreshes them
    // all, which buys headroom in the capability group AND at the top level from one change.
    private val connectionState = combine(
        settingsStore.baseUrl, settingsStore.apiKey, validating, connectionError, connectionStatus,
        ::ConnectionState,
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
        systemPermissions, settingsStore.disabledDeviceToolIds, settingsStore.streamlitWidgetsEnabled,
        deviceControlStatus, ::CapabilityState,
    )

    // A FIFTH group rather than widening an existing one: `scopeState` was already at the five-source
    // cap, so the two engine selections plus their catalog had nowhere to go. Grouped by WHAT ASKS,
    // the same rule the comment above records — these three are the speech pickers' own state, one
    // lazy fetch feeds the catalog, and nothing else in the screen reads them. The top-level combine
    // below now sits AT five itself, so the next addition needs the same treatment again.
    // Now AT the five-source cap itself, with the boost's two sources added — its chosen level and
    // what the last attach actually established. They belong here by the same "group by WHAT ASKS"
    // rule: both are speech state, and only this section reads either.
    private val speechState = combine(
        settingsStore.speechToTextEngine, settingsStore.textToSpeechEngine, speechEngines,
        settingsStore.speechVolumeBoostDecibels, speechVolumeBoost.state, ::SpeechState,
    )

    /** Which permissions have been asked for before — the flag that makes a
     * PERMANENT denial distinguishable from a first ask. */
    val askedPermissions: StateFlow<Set<String>> = settingsStore.askedPermissions
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptySet())

    val uiState: StateFlow<SettingsUiState> = combine(
        connectionState, preferenceState, scopeState, capabilityState, speechState,
    ) { conn, prefs, scope, caps, speech ->
        SettingsUiState(
            baseUrl = conn.baseUrl,
            apiKey = conn.apiKey,
            validating = conn.validating,
            connectionError = conn.connectionError,
            connectionStatus = conn.connectionStatus,
            assistantRole = caps.permissions.assistantRole,
            isTelevision = isTelevision,
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
            speechToTextEngine = speech.speechToTextEngine,
            textToSpeechEngine = speech.textToSpeechEngine,
            speechEngines = speech.engines,
            speechVolumeBoostDecibels = speech.volumeBoostDecibels,
            // Asked of the STATE, not compared here: a refusal of a level the user has since
            // changed is not a refusal of the current one, and spelling that comparison at the
            // screen would put the rule somewhere it can drift from the state that owns it.
            speechVolumeBoostRefused = speech.volumeBoostState.refuses(speech.volumeBoostDecibels),
            smsAccessGranted = caps.permissions.smsAccessGranted,
            disabledDeviceToolIds = caps.disabledDeviceToolIds,
            streamlitWidgetsEnabled = caps.streamlitWidgetsEnabled,
            deviceControlStatus = caps.deviceControlStatus,
            notificationsGranted = caps.permissions.notificationsGranted,
            overlayPermissionGranted = caps.permissions.overlayGranted,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SettingsUiState())

    fun setSpeakResponses(value: Boolean) = viewModelScope.launch { settingsStore.setSpeakResponses(value) }

    fun setReducedMotion(value: Boolean) = viewModelScope.launch { settingsStore.setReducedMotion(value) }

    fun setVoiceUseFakes(value: Boolean) = viewModelScope.launch { settingsStore.setVoiceUseFakes(value) }

    fun setMockBackendEnabled(value: Boolean) = viewModelScope.launch { mockBackendFlags.setEnabled(value) }

    /**
     * The debug escape hatch: clear a device-control overlay that will not come down, and end the
     * grant behind it.
     *
     * Synchronous and NOT on [viewModelScope], deliberately — the overlay owns its own
     * application-scoped teardown, and a person pressing this needs the answer now rather than
     * whenever a coroutine settles. Returns whether there was anything to clear so the caller can
     * say which of the two things happened instead of reporting a flat success.
     *
     * Cost class: `O(1)`.
     */
    fun forceClearDeviceOverlay(): Boolean = deviceControlOverlay.forceTeardown()

    fun setDisplayName(value: String) = viewModelScope.launch { settingsStore.setDisplayName(value) }

    /**
     * Re-read every state Android owns, in one pass.
     *
     * Called on the Settings screen's own RESUME and again after a permission dialog returns.
     * There is no Flow to observe for any of these, and the overlay permission does not even have
     * a dialog to return from — it is granted on a system Settings screen, so the resume re-read
     * is the ONLY thing that can notice it. Each read is `O(1)`; the whole method is one binder
     * round trip per row, and it emits once.
     *
     * The reads are display-only. A grant reaches the tools through `DeviceToolCatalog`'s own
     * permission-gated enumeration on the next `/query`, and the overlay re-checks
     * `canDrawOverlays` on every raise — neither waits on this screen.
     */
    fun refreshSystemPermissions() {
        systemPermissions.value = SystemPermissions(
            smsAccessGranted = devicePermissionChecker.isGranted(Manifest.permission.READ_SMS) &&
                devicePermissionChecker.isGranted(Manifest.permission.SEND_SMS),
            notificationsGranted = notificationPermissionReader.isGranted(),
            overlayGranted = overlayPermissionReader.isGranted(),
            assistantRole = assistantRoleReader.read(),
        )
    }

    /**
     * Probe the STORED credentials once per screen, so a collapsed Connection card can report
     * whether it actually reaches a server.
     *
     * **Presence is not validity.** A saved URL and key say only that someone typed something; the
     * server may have moved, the key may have been rotated, and the row would still have rendered
     * as configured. One `GET /api/models` is what turns that into a fact.
     *
     * Cost: `O(1)` — exactly one request per screen entry, behind [ConnectionProbe]'s own 5s
     * timeout, and skipped entirely when no URL is stored. It never persists anything, so a probe
     * against stale credentials cannot overwrite them.
     */
    fun checkStoredConnection() {
        if (connectionChecked) return
        connectionChecked = true
        viewModelScope.launch {
            val storedUrl = settingsStore.baseUrl.first()
            if (storedUrl.isBlank()) {
                connectionStatus.value = ConnectionStatus.Unconfigured
                return@launch
            }
            connectionStatus.value = ConnectionStatus.Checking
            connectionStatus.value = statusFor(connectionProbe.validate(storedUrl, settingsStore.apiKey.first().orEmpty()))
        }
    }

    /** Record that the system dialog has been shown for these permissions, so a
     * later silent no-op can be recognised as a permanent denial. */
    fun markPermissionAsked(vararg permissions: String) = viewModelScope.launch {
        settingsStore.markPermissionAsked(*permissions)
    }

    /** Re-read on resume. The binder listener covers the live case; this covers
     * the user leaving to start Shizuku and coming back, where no binder event
     * necessarily reaches a process that was in the background. */
    fun refreshDeviceControlStatus() {
        deviceControl.refresh()
    }

    /**
     * Ask Shizuku for access. Returns `false` when its dialog can no longer
     * appear — denied with "don't ask again" — so the caller can send the user
     * to the Shizuku app instead of leaving a tap that does nothing.
     */
    fun requestDeviceControlPermission(): Boolean =
        deviceControl.requestPermission(DEVICE_CONTROL_PERMISSION_REQUEST)

    /**
     * Take one of the two "Display over other apps" routes, whichever the caller offers.
     *
     * **The screen picks a STRATEGY, never a branch.** [OverlayProvisioning.primaryFor] answers
     * which route this device leads with and the arm itself owns what that route does, so nothing
     * here or in the row asks whether this is a television. This method only binds the two I/O
     * legs the arms cannot import: the system deep link, which lives on the Activity-scoped
     * [PermissionRequest] the screen already holds, and the app-op write.
     *
     * [refreshSystemPermissions] runs before [onOutcome], so the row underneath has re-read the
     * real state by the time anything is said. It is worth running for the hand-off arm too: the
     * permission can already have been granted elsewhere while this screen sat open.
     */
    fun provisionOverlayPermission(
        strategy: OverlayProvisioning,
        openSystemOverlayScreen: () -> Unit,
        onOutcome: (OverlayGrantOutcome) -> Unit,
    ) = viewModelScope.launch {
        val routes = object : OverlayProvisioning.Routes {
            override fun openSystemOverlayScreen() = openSystemOverlayScreen()

            override suspend fun grantThroughShizuku(): OverlayGrantOutcome = shizukuOverlayGrant.grant()
        }
        val outcome = strategy.provision(routes)
        refreshSystemPermissions()
        onOutcome(outcome)
    }

    /**
     * The app-op route, reached directly.
     *
     * **A compatibility shim, and it should not survive the wiring change.** `SettingsScreen`
     * still branches on the device shape itself and calls this; once the row goes through
     * [OverlayProvisioning.primaryFor] and [provisionOverlayPermission], delete this — a second
     * entry point to one route is how a screen ends up choosing a mechanism again.
     */
    fun grantOverlayPermissionViaShizuku(onOutcome: (OverlayGrantOutcome) -> Unit) =
        provisionOverlayPermission(
            strategy = OverlayProvisioning.ShizukuAppOp,
            openSystemOverlayScreen = {},
            onOutcome = onOutcome,
        )

    /** Default-project picker's selection - persists immediately, NOT part of
     * "Validate & save" (that pill only guards the connection fields). */
    fun setSelectedProject(contextKey: String) = viewModelScope.launch { settingsStore.setSelectedProject(contextKey) }

    /** "Default model — app" selection - the SAME store key the chat top-bar
     * picker writes (`SettingsStore.selectedModel`), so the two editing surfaces stay in sync. */
    fun setAppDefaultModel(id: String?) = viewModelScope.launch { settingsStore.setSelectedModel(id ?: "") }

    /** "Default model — assistant overlay" selection - independent of the app
     * default; read at the overlay's own session-creation seam (`AuraSession`). */
    fun setOverlayDefaultModel(id: String?) = viewModelScope.launch { settingsStore.setOverlayDefaultModel(id ?: "") }

    /** "Speech to text" selection — blank restores the on-device recognizer. Nothing else needs
     * telling: [com.mewbo.aura.voice.SelectedTranscriber] re-reads this at the start of every
     * capture, so the next mic tap uses the new engine. */
    fun setSpeechToTextEngine(id: String) = viewModelScope.launch { settingsStore.setSpeechToTextEngine(id) }

    /** "Text to speech" selection — blank restores the on-device engine. Picked up by
     * [com.mewbo.aura.voice.SelectedSynthesizer] at the start of the next speech run, so a reply
     * already being read aloud finishes in the voice it started in. */
    fun setTextToSpeechEngine(id: String) = viewModelScope.launch { settingsStore.setTextToSpeechEngine(id) }

    /**
     * "Volume boost" selection, in whole dB; `0` turns it off.
     *
     * Nothing else needs telling, and nothing is applied now:
     * [com.mewbo.aura.voice.SpeechVolumeBoost] reads the level at the start of the next spoken
     * reply, so a reply already being read aloud finishes at the loudness it started at — the same
     * run-boundary rule [setTextToSpeechEngine] follows.
     */
    fun setSpeechVolumeBoostDecibels(decibels: Int) =
        viewModelScope.launch { settingsStore.setSpeechVolumeBoostDecibels(decibels) }

    /** Loads the server speech engines once (retried on each picker open while still `null`), the
     * same shape as [loadModelsIfNeeded] — a failed/offline fetch calls [onNotice] and leaves the
     * catalog `null`, so the picker still offers On device and the rows degrade to the raw id. */
    fun loadSpeechEnginesIfNeeded(onNotice: (String) -> Unit = {}) {
        if (speechEngines.value != null) return
        viewModelScope.launch {
            val catalog = speechRepository.catalog()
            if (catalog != null) speechEngines.value = catalog else onNotice("Couldn't load speech engines")
        }
    }

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
            val result = connectionProbe.validate(baseUrl, apiKey)
            // The probe's verdict drives BOTH the inline error row and the card's own status, from
            // one call — a second source for the header could disagree with the row beneath it.
            connectionStatus.value = statusFor(result)
            connectionChecked = true
            when (result) {
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

    /** §6.12 error row's "Save anyway" escape hatch: persist the draft as-is, skipping the probe.
     * The status drops back to unchecked, because saving past a failure proves nothing about the
     * server and the card must not keep showing the failed reason as though it were current. */
    fun saveAnyway(baseUrl: String, apiKey: String) = viewModelScope.launch {
        settingsStore.setBaseUrl(baseUrl)
        settingsStore.setApiKey(apiKey)
        connectionError.value = null
        connectionStatus.value = ConnectionStatus.Unchecked
    }

    /** Clears a stale failure once the user starts editing the fields again. */
    fun dismissConnectionError() {
        connectionError.value = null
    }

    private fun httpErrorReason(code: Int): String =
        if (code == 401 || code == 403) "Invalid API key" else "Server returned $code"

    private fun statusFor(result: ConnectionProbe.Result): ConnectionStatus = when (result) {
        is ConnectionProbe.Result.Ok -> ConnectionStatus.Connected(result.modelCount)
        is ConnectionProbe.Result.Http -> ConnectionStatus.Failed(httpErrorReason(result.code))
        is ConnectionProbe.Result.Unreachable -> ConnectionStatus.Failed(result.reason)
    }

    // Typed carriers for the four domain sub-flows (above) - one per <=5-flow combine group, so the
    // top-level combine stays under the arity cap and each group destructures by name, not position.
    private data class ConnectionState(
        val baseUrl: String,
        val apiKey: String?,
        val validating: Boolean,
        val connectionError: String?,
        val connectionStatus: ConnectionStatus,
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

    private data class SpeechState(
        val speechToTextEngine: String,
        val textToSpeechEngine: String,
        val engines: SpeechCatalog?,
        val volumeBoostDecibels: Int,
        val volumeBoostState: SpeechBoostState,
    )

    private data class CapabilityState(
        val permissions: SystemPermissions,
        val disabledDeviceToolIds: Set<String>,
        val streamlitWidgetsEnabled: Boolean,
        val deviceControlStatus: DeviceControlStatus,
    )

    /**
     * The states Android owns, refreshed together by [refreshSystemPermissions].
     *
     * Grouped by who answers rather than by where they render: none of them has a Flow, all of
     * them change while the user is in another app, and one resume re-reads the lot. The defaults
     * are the pre-read values a screen shows for the instant before its first resume — every one
     * of them the CONSERVATIVE answer, so a row can never flash "granted" for a permission nobody
     * has asked about yet.
     */
    private data class SystemPermissions(
        val smsAccessGranted: Boolean = false,
        val notificationsGranted: Boolean = false,
        val overlayGranted: Boolean = false,
        val assistantRole: AssistantRole = AssistantRole.Unknown,
    )

    private companion object {
        /** Shizuku returns this to `onRequestPermissionsResult`; nothing else
         * in the app requests a Shizuku permission, so one constant suffices. */
        const val DEVICE_CONTROL_PERMISSION_REQUEST = 4001
    }
}
