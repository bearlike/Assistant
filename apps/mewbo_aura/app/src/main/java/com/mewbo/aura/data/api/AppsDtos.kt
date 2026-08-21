package com.mewbo.aura.data.api

import com.mewbo.aura.data.model.AppCreateResult
import com.mewbo.aura.data.model.AppDetail
import com.mewbo.aura.data.model.AppFreshness
import com.mewbo.aura.data.model.AppFrontendFiles
import com.mewbo.aura.data.model.AppPipelineRun
import com.mewbo.aura.data.model.AppSummary
import com.mewbo.aura.data.model.AppSystemHealth
import com.mewbo.aura.data.model.AppTrigger
import com.mewbo.aura.data.model.AppWorkspaceRef
import com.mewbo.aura.data.model.PipelineLiveness
import com.mewbo.aura.data.model.PipelineSchedule
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

// ---- Mewbo Apps DTOs (see the `AuraApi` interface's own Mewbo Apps section for the "not yet
// field-verified" caveat shared by every type below) ----------------------------------------------

/** One `GET /api/apps` gallery row — the lighter cut of the full `AppSpec` (design spec §3) a
 * gallery list would plausibly serve (mirrors how [SessionSummaryDto] is a lighter cut of full
 * session state). Freshness is NOT embedded here — the gallery fetches it per-card via
 * [AuraApi.getAppSystem], same as the detail screen's health row. */
@Serializable
data class AppSummaryDto(
    @SerialName("app_id") val appId: String,
    val title: String,
    val summary: String = "",
    val icon: String = "",
    val status: String = "",
    val version: Int = 1,
    @SerialName("workspace_ref") val workspaceRef: AppWorkspaceRefDto = AppWorkspaceRefDto(),
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
) {
    fun toDomain() = AppSummary(
        appId = appId,
        title = title,
        summary = summary,
        icon = icon,
        status = status,
        version = version,
        workspaceRef = workspaceRef.toDomain(),
        createdAt = createdAt ?: "",
        updatedAt = updatedAt ?: createdAt ?: "",
    )
}

@Serializable
data class AppsListResponseDto(val apps: List<AppSummaryDto> = emptyList())

/** Wire shape of `AppFrontend` (spec §3): `entrypoint` defaults to `app.py` (the spec's own
 * Pydantic default), `files` is the whole multi-file bundle keyed by relative path, `requirements`
 * installs via micropip inside Pyodide (same as the widget flow). */
@Serializable
data class AppFrontendDto(
    val entrypoint: String = "app.py",
    val files: Map<String, String> = emptyMap(),
    val requirements: List<String> = emptyList(),
) {
    fun toDomain() = AppFrontendFiles(entrypoint = entrypoint, files = files, requirements = requirements)
}

/** Wire shape of `AppSpec.workspace_ref` (spec §3): `kind` is `"own"` or `"shared"`. For `shared`,
 * `key` is the resolved workspace/project identifier (see `SessionScopeRepository`/
 * `ProjectSummary.contextKey` for the same `context.project` shape sessions already anchor to); for
 * `own`, the backend sends the placeholder `key` `"default"` (NOT null) — the create flow posts
 * `{kind:"own", key:"default"}` and the gallery echoes it back. `key` stays nullable only for
 * decode tolerance. */
@Serializable
data class AppWorkspaceRefDto(val kind: String = "own", val key: String? = null) {
    fun toDomain() = AppWorkspaceRef(kind = kind, key = key)
}

/** The `AppSpec` fields needed to mount the WebView (spec §5 flow 2) plus the detail screen's
 * health row. Collections/pipelines/policies from the full `AppSpec` are deliberately NOT mirrored
 * — v1 Aura never edits them, and `ignoreUnknownKeys = true` means a backend that sends them anyway
 * costs nothing to decode past. Nested under [AppDetailResponseDto] — `GET /api/apps/{id}` wraps
 * this in `{spec, versions}`, verified against the console's own `AppDetail` type. */
@Serializable
data class AppSpecDto(
    @SerialName("app_id") val appId: String,
    val title: String,
    val summary: String = "",
    val icon: String = "",
    val status: String = "",
    val version: Int = 1,
    val frontend: AppFrontendDto = AppFrontendDto(),
    @SerialName("workspace_ref") val workspaceRef: AppWorkspaceRefDto = AppWorkspaceRefDto(),
    @SerialName("owner_session_id") val ownerSessionId: String? = null,
    @SerialName("maintainer_session_id") val maintainerSessionId: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
) {
    fun toDomain() = AppDetail(
        appId = appId,
        title = title,
        summary = summary,
        icon = icon,
        status = status,
        version = version,
        frontend = frontend.toDomain(),
        workspaceRef = workspaceRef.toDomain(),
        ownerSessionId = ownerSessionId,
        maintainerSessionId = maintainerSessionId,
        createdAt = createdAt ?: "",
        updatedAt = updatedAt ?: createdAt ?: "",
    )
}

/** `GET /api/apps/{id}` response envelope (console's `AppDetail = {spec, versions}`). [versions] is
 * decoded as raw elements and dropped — v1 Aura has no rollback UI (console-only, spec §4C). */
@Serializable
data class AppDetailResponseDto(val spec: AppSpecDto, val versions: List<JsonElement> = emptyList())

/** `POST /api/apps/{id}/token` response (spec §3 `AppReadToken`: `token_id, app_id, scope,
 * expires_at`). [tokenId] IS the bearer credential injected into the WebView's `app_context.token`
 * (an HMAC-signed opaque string, spec §2.7 — there is no separate lookup step), verified against the
 * console's `AppReadToken` type, which has no distinct `token` field either. */
@Serializable
data class AppTokenDto(
    @SerialName("token_id") val tokenId: String,
    @SerialName("app_id") val appId: String,
    val scope: String = "read",
    @SerialName("expires_at") val expiresAt: String? = null,
)

/** Wire shape of `PipelineRun` (spec §3) — one entry in the app-detail health row's recent-run
 * ledger. `cursor_before`/`cursor_after` are agent-owned opaque state (spec: "agent-owned opaque
 * state"), so they're not mirrored — the client has no use for them beyond display. */
@Serializable
data class PipelineRunDto(
    @SerialName("run_key") val runKey: String,
    @SerialName("app_id") val appId: String,
    @SerialName("pipeline_name") val pipelineName: String,
    @SerialName("trigger_id") val triggerId: String? = null,
    @SerialName("session_run_id") val sessionRunId: String? = null,
    @SerialName("started_at") val startedAt: String,
    @SerialName("ended_at") val endedAt: String? = null,
    val status: String = "running",
    @SerialName("docs_written") val docsWritten: Map<String, Int> = emptyMap(),
    val error: String? = null,
) {
    fun toDomain() = AppPipelineRun(
        runKey = runKey,
        appId = appId,
        pipelineName = pipelineName,
        triggerId = triggerId,
        startedAt = startedAt,
        endedAt = endedAt,
        status = status,
        docsWritten = docsWritten,
        error = error,
    )
}

/** Wire shape of the console's `AppFreshnessWire` — the derived "how fresh is the data" signal
 * (spec §2.8, computed server-side from the `PipelineRun` ledger). [stale] is the honest boolean so
 * a gallery card never paints a false-green for an app whose last run failed or is overdue. */
@Serializable
data class AppFreshnessDto(
    @SerialName("last_success_at") val lastSuccessAt: String? = null,
    @SerialName("last_run_status") val lastRunStatus: String? = null,
    @SerialName("next_fire_at") val nextFireAt: String? = null,
    val stale: Boolean = false,
) {
    fun toDomain() = AppFreshness(
        lastSuccessAt = lastSuccessAt,
        lastRunStatus = lastRunStatus,
        nextFireAt = nextFireAt,
        stale = stale,
    )
}

@Serializable
data class AppMaintainerDto(@SerialName("session_id") val sessionId: String? = null, val status: String? = null)

/** Wire shape of a pipeline's schedule — a discriminated union keyed on [kind]
 * (`"time.cron"` → [cron], `"time.at"` → [at]), or absent for an unscheduled/on-demand pipeline.
 * Decoded loosely (both expression fields nullable) rather than as a sealed polymorphic type — Aura
 * only displays it, never branches product logic beyond picking which field to read. */
@Serializable
data class PipelineScheduleDto(
    val kind: String = "",
    val cron: String? = null,
    val at: String? = null,
) {
    fun toDomain(): PipelineSchedule? {
        val expr = cron ?: at ?: return null
        return PipelineSchedule(kind = kind, expr = expr)
    }
}

/** Wire shape of the console's per-pipeline liveness row (`PipelineLiveness`,
 * additive on `/system`). */
@Serializable
data class PipelineLivenessDto(
    val name: String = "",
    val schedule: PipelineScheduleDto? = null,
    @SerialName("on_demand") val onDemand: Boolean = false,
    @SerialName("trigger_ref") val triggerRef: String? = null,
    val armed: Boolean = false,
) {
    fun toDomain() = PipelineLiveness(
        name = name,
        schedule = schedule?.toDomain(),
        onDemand = onDemand,
        triggerRef = triggerRef,
        armed = armed,
    )
}

/** `GET /api/apps/{id}/system` — the combined health payload (see [AuraApi.getAppSystem]'s KDoc for
 * why this replaced a narrower `/system/runs` guess). Mirrors the console's `AppSystemHealth`. */
@Serializable
data class AppSystemHealthDto(
    @SerialName("app_id") val appId: String,
    val status: String = "",
    val freshness: AppFreshnessDto = AppFreshnessDto(),
    val triggers: List<TriggerDto> = emptyList(),
    val runs: List<PipelineRunDto> = emptyList(),
    val maintainer: AppMaintainerDto = AppMaintainerDto(),
    val pipelines: List<PipelineLivenessDto> = emptyList(),
) {
    fun toDomain() = AppSystemHealth(
        appId = appId,
        status = status,
        freshness = freshness.toDomain(),
        triggers = triggers.map { it.toDomain() },
        runs = runs.map { it.toDomain() },
        maintainerSessionId = maintainer.sessionId,
        maintainerStatus = maintainer.status,
        pipelines = pipelines.map { it.toDomain() },
    )
}

/**
 * The reverse-invocation trigger record — field-for-field the SAME shape the web console's
 * `TriggerDTO` already consumes against the frozen global `/api/triggers` contract.
 * Reused verbatim (not redefined per-product) since an app's triggers ride the SAME trigger
 * subsystem (spec §2.10) — `kind`/`status`/`action` decode as plain strings rather than closed
 * enums; Aura only lists + pauses/resumes, it never branches product logic on a specific kind.
 */
@Serializable
data class TriggerDto(
    val id: String,
    @SerialName("session_id") val sessionId: String,
    val kind: String,
    val status: String,
    @SerialName("wake_prompt") val wakePrompt: String = "",
    val action: String = "message",
    // Kind-specific fields the server folds into `args` (e.g. `{"cron": "0 9 * * *"}`, the webhook
    // `secret` stripped) — mirrors the console's `TriggerDTO.args`. Decoded for field-for-field
    // parity; Aura lists/pauses only, so `toDomain()` doesn't yet read it.
    val args: Map<String, JsonElement> = emptyMap(),
    val fires: Int = 0,
    @SerialName("max_fires") val maxFires: Int? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("next_fire_at") val nextFireAt: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("last_fired_at") val lastFiredAt: String? = null,
    @SerialName("last_error") val lastError: String? = null,
) {
    fun toDomain() = AppTrigger(
        id = id,
        sessionId = sessionId,
        kind = kind,
        status = status,
        wakePrompt = wakePrompt,
        fires = fires,
        nextFireAt = nextFireAt,
        lastFiredAt = lastFiredAt,
        lastError = lastError,
    )
}

@Serializable
data class AppTriggersResponseDto(val triggers: List<TriggerDto> = emptyList())

/** Body for `PATCH /api/triggers/{id}` — pause (`paused`) or resume (`armed`), mirroring the
 * console's `updateTriggerStatus`. */
@Serializable
data class TriggerStatusUpdateRequest(val status: String)

/** Body for `POST /api/apps` (spec §5 flow 1: "intent + workspace choice"). */
@Serializable
data class AppCreateRequest(val intent: String, val workspace: AppWorkspaceRefDto)

/**
 * `POST /api/apps` response — the backend returns exactly `{app_id, session_id}` (201). [sessionId]
 * is the builder session the client follows via [com.mewbo.aura.data.repo.RunRepository.live] for the
 * terminal `app_ready` event (spec §5 flow 1); it is nullable only for decode tolerance (the current
 * backend always mints one). There is NO `status` on this response — a freshly created app is always
 * `building`, so the creation screen follows the session rather than reading a status here.
 */
@Serializable
data class AppCreateResponseDto(
    @SerialName("app_id") val appId: String,
    @SerialName("session_id") val sessionId: String? = null,
) {
    fun toDomain() = AppCreateResult(appId = appId, sessionId = sessionId)
}
