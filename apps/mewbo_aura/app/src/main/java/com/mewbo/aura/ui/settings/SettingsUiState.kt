package com.mewbo.aura.ui.settings

import com.mewbo.aura.data.model.ProjectSummary

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
    /** `SettingsStore.selectedProject` passthrough (Gitea #178 W1-A) — a bare name or `managed:<id>`
     * contextKey, empty for Temporary. */
    val selectedProject: String = "",
    /** Catalog for [selectedProject]'s display-name lookup, loaded lazily on the project picker
     * sheet's first open ([SettingsViewModel.loadProjectsIfNeeded]) - `null` until then/on a failed
     * fetch, never crashes ([resolveProjectDisplayName] degrades to the raw key). */
    val projects: List<ProjectSummary>? = null,
    /** READ_SMS + SEND_SMS granted state (Gitea #179 Phase 4) - a live OS read
     * ([SettingsViewModel.refreshSmsAccessStatus]), not a persisted preference; there is
     * deliberately no separate consent toggle (task brief - OS runtime grants are the sole gate). */
    val smsAccessGranted: Boolean = false,
)

/** [SettingsUiState.selectedProject]'s display-name resolution against [SettingsUiState.projects]
 * (task brief W1-A) - "Temporary" for an empty key, the raw stored key when the catalog hasn't
 * loaded or has no match (silent-degrade, keeps the row usable offline). Pure so it's unit-testable
 * without Compose - same motivation as [com.mewbo.aura.ui.chat.SessionBinding]'s extraction. */
internal fun resolveProjectDisplayName(selectedProject: String, projects: List<ProjectSummary>?): String =
    if (selectedProject.isBlank()) {
        "Temporary"
    } else {
        projects?.firstOrNull { it.contextKey == selectedProject }?.name ?: selectedProject
    }
