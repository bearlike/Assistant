package com.mewbo.aura.data.model

/**
 * Domain models for the Mewbo Apps sub-product (design spec
 * `docs/superpowers/specs/2026-07-17-mewbo-apps-design.md` §3), mapped from their `data/api`
 * DTO counterparts ([com.mewbo.aura.data.api.AppSummaryDto] etc.) the same way [SessionSummary] is
 * mapped from [com.mewbo.aura.data.api.SessionSummaryDto] — UI-facing shapes with no wire
 * annotations.
 */

/** A gallery card (`GET /api/apps` row). */
data class AppSummary(
    val appId: String,
    val title: String,
    val summary: String,
    val icon: String,
    val status: String,
    val version: Int,
    val workspaceRef: AppWorkspaceRef,
    val createdAt: String,
    val updatedAt: String,
)

/** `AppSpec.workspace_ref` (spec §3): `kind` is `"own"` or `"shared"`; `key` is the resolved
 * workspace/project identifier when shared (parallels [ProjectSummary.contextKey]), null for own. */
data class AppWorkspaceRef(val kind: String, val key: String?)

/** The multi-file stlite frontend bundle mounted into the WebView (spec §3 `AppFrontend`). */
data class AppFrontendFiles(
    val entrypoint: String,
    val files: Map<String, String>,
    val requirements: List<String>,
)

/** `GET /api/apps/{id}` — everything the detail screen needs to render the WebView + health row. */
data class AppDetail(
    val appId: String,
    val title: String,
    val summary: String,
    val icon: String,
    val status: String,
    val version: Int,
    val frontend: AppFrontendFiles,
    val workspaceRef: AppWorkspaceRef,
    val ownerSessionId: String?,
    val maintainerSessionId: String?,
    val createdAt: String,
    val updatedAt: String,
)

/** The short-lived render-scoped token minted for a WebView open (spec §2.7 `AppReadToken`).
 * [tokenId] IS the bearer credential (see [com.mewbo.aura.data.api.AppTokenDto]'s KDoc for why
 * there's no separate `token` field). */
data class AppToken(val tokenId: String, val appId: String, val expiresAt: String?)

/** A `PipelineRun` ledger row (spec §3) — one entry in [AppSystemHealth.runs]. */
data class AppPipelineRun(
    val runKey: String,
    val appId: String,
    val pipelineName: String,
    val triggerId: String?,
    val startedAt: String,
    val endedAt: String?,
    val status: String,
    val docsWritten: Map<String, Int>,
    val error: String?,
)

/** The derived "how fresh is the data" signal (spec §2.8) — a gallery card or the detail health row
 * reads this directly rather than re-deriving it from [AppSystemHealth.runs] itself. [stale] is the
 * honest boolean: true for a data-bearing app whose last run failed or is overdue, so a card never
 * paints a false-green. */
data class AppFreshness(
    val lastSuccessAt: String?,
    val lastRunStatus: String?,
    val nextFireAt: String?,
    val stale: Boolean,
)

/** A pipeline's declared schedule — a discriminated union keyed on [kind]
 * (`"time.cron"` → a cron expression, `"time.at"` → an ISO instant), both carried in [expr] since
 * Aura only displays it and never branches product logic on which kind it is beyond picking the
 * right phrasing. `null` on [PipelineLiveness.schedule] means no schedule at all. */
data class PipelineSchedule(val kind: String, val expr: String)

/** Per-pipeline liveness row on `/system` (additive) — the read-only counterpart
 * to `AppSpec.pipelines` the health payload carries so the detail screen can show WHEN each pipeline
 * refreshes without mirroring the full agent-authored `PipelineSpec` (wake_prompt/tools_allowlist/
 * cursor stay console-only, same reasoning as [com.mewbo.aura.data.api.AppSpecDto]'s KDoc). */
data class PipelineLiveness(
    val name: String,
    val schedule: PipelineSchedule?,
    val onDemand: Boolean,
    val triggerRef: String?,
    /** Whether [triggerRef] currently resolves to an armed trigger. */
    val armed: Boolean,
)

/** `GET /api/apps/{id}/system` — the combined read-only introspection payload (spec §2.6): the SAME
 * fetch backs both a gallery card's freshness badge and the detail screen's health row + trigger
 * list (DRY — one call, not three). */
data class AppSystemHealth(
    val appId: String,
    val status: String,
    val freshness: AppFreshness,
    val triggers: List<AppTrigger>,
    val runs: List<AppPipelineRun>,
    val maintainerSessionId: String?,
    val maintainerStatus: String?,
    /** Optional/additive: per-pipeline schedule/on-demand/armed detail. Empty
     * on a server that hasn't shipped phase 1 yet — the detail screen falls back to the
     * freshness-only view in that case. */
    val pipelines: List<PipelineLiveness> = emptyList(),
)

/** An app-scoped trigger row (same shape as the web console's `TriggerDTO` — see
 * [com.mewbo.aura.data.api.TriggerDto]'s KDoc). */
data class AppTrigger(
    val id: String,
    val sessionId: String,
    val kind: String,
    val status: String,
    val wakePrompt: String,
    val fires: Int,
    val nextFireAt: String?,
    val lastFiredAt: String?,
    val lastError: String?,
) {
    /** Pause/resume only act on a trigger that still fires — mirrors the console's
     * `isActiveTrigger` (`ACTIVE_TRIGGER_STATUSES = {armed, paused}`). */
    val isActive: Boolean get() = status == "armed" || status == "paused"
}

/** `POST /api/apps` result — see [com.mewbo.aura.data.api.AppCreateResponseDto]'s KDoc for why
 * [sessionId] is nullable. A freshly created app is always building, so there is no status here;
 * the creation screen follows the builder session for the terminal `app_ready` event. */
data class AppCreateResult(val appId: String, val sessionId: String?)
