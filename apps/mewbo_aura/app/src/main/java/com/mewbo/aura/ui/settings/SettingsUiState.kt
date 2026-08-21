package com.mewbo.aura.ui.settings

import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection

/**
 * Settings screen state (§8.4) — mirrors [com.mewbo.aura.data.settings.SettingsStore] plus the
 * connection-probe's own transient state ([validating]/[connectionError] are never persisted;
 * [com.mewbo.aura.data.repo.ConnectionProbe] runs against caller-supplied draft text, not a store
 * flow, so the screen owns the draft and this state only reports the in-flight/last-failure status).
 */
data class SettingsUiState(
    val baseUrl: String = "",
    val apiKey: String? = null,
    val speakResponses: Boolean = true,
    val reducedMotion: Boolean = false,
    val voiceUseFakes: Boolean = false,
    /** [com.mewbo.aura.mock.MockBackendFlags] passthrough - debug-only (row hidden behind
     * `IS_DEBUG_BUILD` in `SettingsScreen`), always `false` in a release build since
     * `MockBackendFlags`'s release binding is a permanently-off no-op. */
    val mockBackendEnabled: Boolean = false,
    val displayName: String = "",
    val validating: Boolean = false,
    val connectionError: String? = null,
    /** Whether the STORED credentials reach a server, answered by a probe rather than assumed from
     * their presence. Nothing persists a validated flag, so this resets to
     * [ConnectionStatus.Unchecked] every time the screen is built. */
    val connectionStatus: ConnectionStatus = ConnectionStatus.Unchecked,
    /** Whether Mewbo holds Android's assistant role. [AssistantRole.Unknown] when the platform
     * gives no readable answer — see [AssistantRoleReader]. */
    val assistantRole: AssistantRole = AssistantRole.Unknown,
    /**
     * Whether this device is a television, read once via
     * [com.mewbo.aura.data.device.TelevisionChecker].
     *
     * When true the assist-role surfaces are HIDDEN, not disabled — why, in
     * `apps/mewbo_aura/CLAUDE.md` § "TV-shape facts". Defaults to `false` so a handheld is
     * unaffected by the field's existence.
     */
    val isTelevision: Boolean = false,
    /** `SettingsStore.selectedProject` passthrough — a bare name or `managed:<id>`
     * contextKey, empty for Temporary. */
    val selectedProject: String = "",
    /** Catalog for [selectedProject]'s display-name lookup, loaded lazily on the project picker
     * sheet's first open ([SettingsViewModel.loadProjectsIfNeeded]) - `null` until then/on a failed
     * fetch, never crashes ([resolveProjectDisplayName] degrades to the raw key). */
    val projects: List<ProjectSummary>? = null,
    /** READ_SMS + SEND_SMS granted state - a live OS read
     * ([SettingsViewModel.refreshSmsAccessStatus]), not a persisted preference; there is
     * deliberately no separate consent toggle (task brief - OS runtime grants are the sole gate). */
    val smsAccessGranted: Boolean = false,
    /** POST_NOTIFICATIONS. Surfaced so the user can grant it from Settings
     * rather than only at the one moment the app happens to ask. */
    val notificationsGranted: Boolean = false,
    /** SYSTEM_ALERT_WINDOW — whether the device-control overlay may appear at all. Nothing else in
     * the app asks for it, and without it an agent drives the phone with no visible sign: the glow,
     * the narration bubbles and the Stop pill all fail to raise, silently, exactly as designed
     * (`ui/control/DeviceControlOverlay.raise`). Read via [OverlayPermissionReader], never through
     * `checkSelfPermission` — it is a special, app-op-backed permission. */
    val overlayPermissionGranted: Boolean = false,
    /** Why device control is or is not available. Not a boolean: "off" has three
     * causes here and each needs a different action from the user. */
    val deviceControlStatus: DeviceControlStatus = DeviceControlStatus.NotInstalled,
    /** `SettingsStore.selectedModel` passthrough - the FULL-APP default model,
     * blank = server default. Edited from the "Default model — app" row (and, elsewhere, the chat
     * top-bar picker). */
    val appDefaultModel: String = "",
    /** `SettingsStore.overlayDefaultModel` passthrough - the ASSIST-OVERLAY
     * default model, independent of [appDefaultModel], blank = server default. */
    val overlayDefaultModel: String = "",
    /** Catalog for the two model rows' display-name lookup + the reused picker sheet, loaded lazily
     * on a model-picker's first open ([SettingsViewModel.loadModelsIfNeeded]) - `null` until then/on
     * a failed fetch; [resolveModelDisplayName] degrades to the raw id, never crashes. */
    val models: ModelCatalog? = null,
    /** `SettingsStore.speechToTextEngine` passthrough — blank = the on-device recognizer, which is
     * the default, so a user who never opens the row keeps the platform behaviour. */
    val speechToTextEngine: String = "",
    /** `SettingsStore.textToSpeechEngine` passthrough — blank = the on-device engine. Independent
     * of [speechToTextEngine]; the two directions are picked separately. */
    val textToSpeechEngine: String = "",
    /** Catalog for the two speech rows + their picker sheets, loaded lazily on a speech picker's
     * first open ([SettingsViewModel.loadSpeechEnginesIfNeeded]) — `null` until then/on a failed
     * fetch. `null` means "we have not asked", NOT "the server has none": the pickers still offer
     * On device, and [resolveSpeechEngineName] degrades a server selection to its raw id rather
     * than mislabelling it as on-device. */
    val speechEngines: SpeechCatalog? = null,
    /** `SettingsStore.speechVolumeBoostDecibels` passthrough — whole dB of amplification applied
     * ABOVE the device's own maximum, `0` = off and the untouched default. */
    val speechVolumeBoostDecibels: Int = 0,
    /**
     * Whether the platform REFUSED the boost effect at the level currently chosen — measured by an
     * actual attach, never assumed.
     *
     * A `Boolean` rather than the state object because only one of its three cases is a claim this
     * screen may make. A successful attach is deliberately NOT surfaced: the effect existing on a
     * session is not proof the selected engine's audio passes through it, and "Supported" over an
     * engine that ignores the session id is exactly the wrong-green this screen exists to prevent.
     * [com.mewbo.aura.voice.SpeechBoostState.refuses] owns the level comparison, so a refusal of a
     * level the user has since changed cannot leak through as a current one.
     */
    val speechVolumeBoostRefused: Boolean = false,
    /** Tool ids the user switched OFF. A device-tool switch is checked iff its id
     * is NOT in this set (empty default = all on). */
    val disabledDeviceToolIds: Set<String> = emptySet(),
    /** "Streamlit widgets" toggle (→ P5), default ON now the renderer exists. */
    val streamlitWidgetsEnabled: Boolean = true,
)

/** [SettingsUiState.appDefaultModel]/[overlayDefaultModel] display-name resolution against
 * [SettingsUiState.models] - "Default" for a blank selection (the API's own
 * configured default), otherwise the catalog's pretty name ([ModelCatalog.displayName] prettifies
 * even an id it doesn't know), degrading to the normalized raw id only when the catalog hasn't loaded
 * (offline first-open), mirroring [resolveProjectDisplayName]'s posture. Pure so it's unit-testable
 * without Compose. */
internal fun resolveModelDisplayName(modelId: String, models: ModelCatalog?): String =
    if (modelId.isBlank()) "Default" else models?.displayName(modelId) ?: ModelCatalog.normalize(modelId)

/**
 * How a speech row reads: "On device" for the blank default, otherwise the catalog's label behind
 * [SpeechCatalog.CLOUD_MARK].
 *
 * A thin delegation rather than a `when` here, because the mark and the on-device wording are
 * intrinsic to the catalog and are also read by the picker sheet — spelled in two places they
 * would drift the first time the wording changed, and a row reading "On device" over a server
 * engine is precisely the wrong claim this screen exists to prevent. Pure, so it is unit-testable
 * without Compose, same as [resolveModelDisplayName].
 */
internal fun resolveSpeechEngineName(
    storedId: String,
    direction: SpeechDirection,
    catalog: SpeechCatalog?,
): String = catalog?.displayName(storedId, direction)
    ?: if (SpeechCatalog.isOnDevice(storedId)) SpeechCatalog.ON_DEVICE_LABEL else SpeechCatalog.cloudLabel(storedId)

/**
 * How the volume-boost row and its picker read: "Off" at zero, otherwise a SIGNED decibel figure.
 *
 * The `+` is load-bearing rather than decoration — this control only ever adds, and an unsigned
 * "6 dB" beside a volume label reads as an absolute level the device is being set to. Pure, so it
 * is unit-testable without Compose, same as [resolveModelDisplayName].
 */
internal fun resolveVolumeBoostLabel(decibels: Int): String =
    if (decibels <= 0) "Off" else "+$decibels dB"

/** [SettingsUiState.selectedProject]'s display-name resolution against [SettingsUiState.projects]
 * (task brief W1-A) - "Temporary" for an empty key, the raw stored key when the catalog hasn't
 * loaded or has no match (silent-degrade, keeps the row usable offline). Pure so it's unit-testable
 * without Compose - same motivation as [com.mewbo.aura.ui.chat.SessionBinding]'s extraction. */
internal fun resolveProjectDisplayName(selectedProject: String, projects: List<ProjectSummary>?): String =
    when {
        selectedProject.isBlank() -> "Temporary"
        // The sentinel is not a project and resolves against no catalog, so the lookup below would
        // degrade it to the raw stored "auto" — and the loop closes on itself: the picker offers a
        // row labelled "Auto", persists this key, and the settings row underneath then reads
        // "auto". Same branch, same reason, as `ComposerScope.projectDisplayName`.
        selectedProject == ComposerScope.AUTO_PROJECT_KEY -> "Auto"
        else -> projects?.firstOrNull { it.contextKey == selectedProject }?.name ?: selectedProject
    }
