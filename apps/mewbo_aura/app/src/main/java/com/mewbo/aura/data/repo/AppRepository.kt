package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AppCreateRequest
import com.mewbo.aura.data.api.AppWorkspaceRefDto
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.TriggerStatusUpdateRequest
import com.mewbo.aura.data.model.AppCreateResult
import com.mewbo.aura.data.model.AppDetail
import com.mewbo.aura.data.model.AppSummary
import com.mewbo.aura.data.model.AppSystemHealth
import com.mewbo.aura.data.model.AppToken
import com.mewbo.aura.data.model.AppTrigger
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * List/detail/create for the Mewbo Apps sub-product (design spec §4A/§4D). Mirrors
 * [SessionRepository]'s shape: the backend is the source of truth, this only caches the
 * last-fetched gallery list so the gallery screen renders immediately on re-entry
 * (same `@Singleton` stale-while-revalidate reasoning as [SessionRepository]).
 * Mutations (`createApp`, `updateTriggerStatus`) degrade to `null`/`false` on failure rather than a
 * typed exception, matching [SessionRepository]'s idiom, not [RunRepository]'s — none of these are a
 * "send into a session" call that needs `errorFor`'s 410 handling.
 */
@Singleton
class AppRepository @Inject constructor(
    private val api: AuraApi,
) {
    private val _apps = MutableStateFlow<List<AppSummary>>(emptyList())
    val apps: StateFlow<List<AppSummary>> = _apps.asStateFlow()

    suspend fun refreshApps(): List<AppSummary> {
        val summaries = api.getApps().apps.map { it.toDomain() }
        _apps.value = summaries
        return summaries
    }

    /** `.spec` is the only half of the `{spec, versions}` envelope v1 Aura reads — see
     * [AuraApi.getApp]'s KDoc. */
    suspend fun fetchDetail(appId: String): AppDetail? =
        runCatching { api.getApp(appId).spec.toDomain() }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()

    suspend fun mintToken(appId: String): AppToken? =
        runCatching { api.mintAppToken(appId) }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()
            ?.let { AppToken(tokenId = it.tokenId, appId = it.appId, expiresAt = it.expiresAt) }

    /** The combined freshness/triggers/runs/maintainer payload (spec §2.6 DRY: one endpoint backs
     * both a gallery card's freshness badge and the detail screen's health row). `null` on failure —
     * a card with no freshness to show reads the same as one whose fetch failed. */
    suspend fun systemHealth(appId: String): AppSystemHealth? =
        runCatching { api.getAppSystem(appId).toDomain() }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()

    /** Kept as its own call (task brief: "triggers list/pause") for a caller that only needs
     * triggers — [systemHealth] already carries the SAME list for the detail screen, which fetches
     * that instead of calling this too. */
    suspend fun triggers(appId: String): List<AppTrigger>? =
        runCatching { api.getAppTriggers(appId).triggers.map { it.toDomain() } }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()

    /** `true` on HTTP success — see [AuraApi.updateTriggerStatus]'s KDoc for why this reuses the
     * existing global trigger mutation rather than an app-scoped one. */
    suspend fun setTriggerPaused(triggerId: String, paused: Boolean): Boolean =
        runCatching { api.updateTriggerStatus(triggerId, TriggerStatusUpdateRequest(if (paused) "paused" else "armed")) }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() != null

    /**
     * Starts a build (spec §5 flow 1). [workspaceKey] is the chosen shared project's
     * [com.mewbo.aura.data.model.ProjectSummary.contextKey], null for an own/dedicated workspace.
     * On success also refreshes the cached gallery list (best-effort, isolated — same pattern as
     * [SessionRepository.forkSession]) so a freshly created draft shows up without a manual refresh.
     */
    suspend fun createApp(intent: String, workspaceKind: String, workspaceKey: String?): AppCreateResult? {
        val response = runCatching {
            api.createApp(AppCreateRequest(intent = intent, workspace = AppWorkspaceRefDto(kind = workspaceKind, key = workspaceKey)))
        }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() ?: return null
        runCatching { refreshApps() }.onFailure { if (it is CancellationException) throw it }
        return response.toDomain()
    }
}
