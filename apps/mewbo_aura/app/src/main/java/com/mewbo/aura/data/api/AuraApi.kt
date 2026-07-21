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
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.SessionHistory
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.data.model.ToolSummary
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query

/**
 * REST surface consumed by the app (verified field-for-field against api-contract.md).
 * SSE (`GET /api/sessions/{id}/stream`) is intentionally NOT here - it's raw OkHttp EventSource,
 * owned entirely by [com.mewbo.aura.data.sse.SessionStreamClient].
 */
interface AuraApi {
    @POST("api/sessions")
    suspend fun createSession(@Body request: SessionCreateRequest): SessionCreateResponseDto

    @GET("api/sessions")
    suspend fun listSessions(
        @Query("include_archived") includeArchived: Boolean = false,
    ): SessionsListResponseDto

    /** Server trims + caps the title at 120 chars; `400` blank title / `404` unknown session both
     * surface as a [retrofit2.HttpException] to [com.mewbo.aura.data.repo.SessionRepository.renameSession]. */
    @PATCH("api/sessions/{id}/title")
    suspend fun renameSession(
        @Path("id") sessionId: String,
        @Body request: RenameSessionRequest,
    ): RenameSessionResponseDto

    /** `404` unknown session surfaces as a [retrofit2.HttpException]. Archived sessions are hidden
     * from [listSessions] unless `include_archived=true`. */
    @POST("api/sessions/{id}/archive")
    suspend fun archiveSession(@Path("id") sessionId: String): ArchiveSessionResponseDto

    /**
     * `200` = session was idle, a new run started (`run_id` present).
     * `202` = a run was already active, this steered it (no `run_id`).
     * `enqueued` is `true` in BOTH cases - it is NOT a discriminator (api-contract.md section 3).
     * Callers must branch on [Response.code], hence the raw [Response] wrapper here.
     */
    @POST("api/sessions/{id}/message")
    suspend fun sendMessage(
        @Path("id") sessionId: String,
        @Body request: SendMessageRequest,
    ): Response<SendMessageResponseDto>

    /** `after` is an ISO-8601 cursor, strictly-greater-than filtering server-side. */
    @GET("api/sessions/{id}/events")
    suspend fun getEvents(
        @Path("id") sessionId: String,
        @Query("after") after: String? = null,
    ): SessionEventsResponseDto

    /** Model names served by the configured LLM proxy, the default model, and a per-model
     * capability map (`supports_vision`). See [com.mewbo.aura.data.model.ModelCatalog]. */
    @GET("api/models")
    suspend fun getModels(): ModelsResponseDto

    /**
     * Starts an async run (`202`, [SessionQueryResponseDto.accepted] true) - see
     * [com.mewbo.aura.data.repo.RunRepository.sendQuery]. `200` means a slash command (`/status`,
     * `/terminate`) was handled inline instead - both land in the same 2xx range, so the raw
     * [Response] wrapper is what lets callers branch on [Response.code] the same way [sendMessage]
     * already does for its own 200-vs-202 split.
     */
    @POST("api/sessions/{id}/query")
    suspend fun query(
        @Path("id") sessionId: String,
        @Body request: SessionQueryRequest,
    ): Response<SessionQueryResponseDto>

    /**
     * Retry/rewind mutation. `action` is currently always `"retry"` - the backend TRUNCATES this
     * session's transcript at `from_ts` (when given) before re-running it, a destructive in-place
     * rewind of the SAME session (see [com.mewbo.aura.data.repo.RunRepository.retryFrom]). `409` =
     * a run is already active on the session; `410` = the session is permanently terminated
     * (`session_terminated` envelope) - both read via
     * [com.mewbo.aura.data.repo.errorFor], exactly like [sendMessage]/[query].
     */
    @POST("api/sessions/{id}/recover")
    suspend fun recoverSession(
        @Path("id") sessionId: String,
        @Body request: RecoverSessionRequest,
    ): Response<RecoverSessionResponseDto>

    /**
     * Forks a NEW session shell from [sessionId] - never mutates the source session. `from_ts`
     * null copies the WHOLE transcript; set, copies up to and including that message. `409` = the
     * source session has a run active; `410` = the source session is permanently terminated. See
     * [com.mewbo.aura.data.repo.SessionRepository.forkSession].
     */
    @POST("api/sessions/{id}/fork")
    suspend fun forkSession(
        @Path("id") sessionId: String,
        @Body request: ForkSessionRequest,
    ): Response<ForkSessionResponseDto>

    /** `files` is the repeated multipart field the backend reads (`request.files.getlist("files")`);
     * `model` is an optional hint so the server can reject an image against a non-vision model
     * eagerly (a 400) rather than silently dropping it at inference time. */
    @Multipart
    @POST("api/sessions/{id}/attachments")
    suspend fun uploadAttachments(
        @Path("id") sessionId: String,
        @Part files: List<MultipartBody.Part>,
        @Part("model") model: RequestBody? = null,
    ): AttachmentsResponseDto

    @GET("api/projects")
    suspend fun getProjects(): ProjectsResponseDto

    /** MCP/builtin tool registry, optionally scoped to a project's own MCP config. */
    @GET("api/tools")
    suspend fun getTools(@Query("project") project: String? = null): ToolsResponseDto

    /**
     * Resolves a [SessionEvent.DeviceToolCall][com.mewbo.aura.data.model.SessionEvent.DeviceToolCall]
     * dispatched over SSE. `200` = resolved; `403` = bad token; `404` =
     * unknown call id; `409` = already consumed - all treated as terminal by
     * [com.mewbo.aura.data.device.DeviceToolExecutor], never retried. The response type is the raw
     * [ResponseBody] (not a converted DTO) deliberately: a `200`'s body may legitimately be empty,
     * and running it through the kotlinx.serialization converter would risk a decode failure on
     * exactly the success path that matters most.
     */
    @POST("api/sessions/{sessionId}/device_tools/{callId}/result")
    suspend fun postDeviceToolResult(
        @Path("sessionId") sessionId: String,
        @Path("callId") callId: String,
        @Body request: DeviceToolResultRequest,
    ): Response<ResponseBody>

    // ---- Mewbo Apps (design spec docs/superpowers/specs/2026-07-17-mewbo-apps-design.md §4A/§4D) ----
    // The backend routes (`apps.routes.AppsRoutesController`) had not landed at the time this client
    // was written — every DTO below was FIELD-VERIFIED against the web console's own mirror
    // (`apps/mewbo_console/src/types.ts` + `src/api/apps.ts`, landed concurrently by the console
    // workstream) rather than re-derived independently, since that module is itself a direct
    // transcription of the spec's Pydantic contracts (§3). `getApp`/`getAppSystem` in particular
    // replaced this client's own first-pass guesses (a flat detail DTO; a narrower
    // `/system/runs?limit=1` endpoint) once the console's shape was available — see each DTO's KDoc
    // for the specific correction. Every field still decodes tolerantly (defaults, nullable) since
    // neither client has verified against a LIVE response yet (backend routes not landed).

    /** `GET /api/apps` — the gallery list. */
    @GET("api/apps")
    suspend fun getApps(): AppsListResponseDto

    /** `GET /api/apps/{id}` — the manifest + version history (console's `AppDetail = {spec,
     * versions}`; [versions] is decoded but unused — v1 Aura has no rollback UI, that's console-only
     * per spec §4C). */
    @GET("api/apps/{id}")
    suspend fun getApp(@Path("id") appId: String): AppDetailResponseDto

    /** `POST /api/apps/{id}/token` — mints the short-lived render-scoped read token (spec §2.7)
     * injected into the WebView payload's `app_context`. */
    @POST("api/apps/{id}/token")
    suspend fun mintAppToken(@Path("id") appId: String): AppTokenDto

    /** `GET /api/apps/{id}/system` — the combined read-only introspection payload (spec §2.6: "the
     * platform's app-detail health pane uses the SAME system endpoints (DRY)") — freshness +
     * triggers + recent runs + maintainer status in ONE call, backing both the gallery card's
     * freshness badge and the detail screen's health row. Console's `AppSystemHealth` shape. */
    @GET("api/apps/{id}/system")
    suspend fun getAppSystem(@Path("id") appId: String): AppSystemHealthDto

    /** `GET /api/apps/{id}/triggers` — app-scoped trigger list (spec §4A: "resolving through the
     * maintainer session"), same [TriggerDto] shape the global `/api/triggers` list already uses.
     * Kept as its own call (console's `listAppTriggers`) alongside [getAppSystem] — a caller that
     * only needs triggers (this task's "triggers list/pause" deliverable) shouldn't have to also
     * pull runs/freshness. */
    @GET("api/apps/{id}/triggers")
    suspend fun getAppTriggers(@Path("id") appId: String): AppTriggersResponseDto

    /** `PATCH /api/triggers/{id}` — pause (`paused`)/resume (`armed`). Reuses the EXISTING global
     * trigger contract (spec §2.10: "triggers stay owned by the trigger subsystem") rather than a
     * new app-scoped mutation route — the same endpoint the web console's `updateTriggerStatus`
     * already calls. */
    @PATCH("api/triggers/{id}")
    suspend fun updateTriggerStatus(
        @Path("id") triggerId: String,
        @Body request: TriggerStatusUpdateRequest,
    ): TriggerDto

    /** `POST /api/apps` — starts the build (spec §5 flow 1: intent + workspace choice → a draft app
     * + a builder session the client follows live for `app_ready`). */
    @POST("api/apps")
    suspend fun createApp(@Body request: AppCreateRequest): AppCreateResponseDto

    /**
     * Delivers a human answer for a pending `user_question` event (the blocked `ask_user_question`
     * tool call resolves with it). `200` = delivered (`{"resolved":true}`); `403` bad token / `404`
     * unknown-or-already-read / `409` already delivered / `422` answers don't fit / `410` session
     * terminated. The authoritative card settle is the `user_question_answered` SSE event, so `404`/
     * `409` mean it already fired (settle silently, answered-elsewhere); everything else is a genuine
     * failure. Raw [ResponseBody] like [postDeviceToolResult] — the caller branches on
     * [Response.code], and a `200` body may be empty. The `X-Mewbo-Surface` header (stamped globally
     * by [com.mewbo.aura.di.AuthInterceptor]) is recorded server-side as `answered_via`.
     */
    @POST("api/sessions/{sessionId}/questions/{callId}/answer")
    suspend fun answerQuestion(
        @Path("sessionId") sessionId: String,
        @Path("callId") callId: String,
        @Body request: QuestionAnswerRequest,
    ): Response<ResponseBody>
}

/**
 * The backend's structured error envelope — `{"error":{"code","reason","retryable"}}` (verified
 * against `apps/mewbo_api/.../responses.py` `ApiResponseKit`). Read off a NON-2xx response body at
 * the ONE send seam ([com.mewbo.aura.data.repo.RunRepository]) so a terminal condition like a
 * permanently terminated session (410, `code == "session_terminated"`) is
 * distinguishable from a generic transport failure. Every field defaults so a body that ISN'T this
 * shape decodes to an empty envelope rather than throwing during error handling.
 */
@Serializable
data class ApiErrorEnvelope(val error: ApiErrorBody? = null)

@Serializable
data class ApiErrorBody(
    val code: String? = null,
    val reason: String? = null,
    val retryable: Boolean = false,
)

@Serializable
data class SessionCreateRequest(
    @SerialName("session_tag") val sessionTag: String? = null,
    val project: String? = null,
    val mode: String? = null,
    val context: JsonElement? = null,
)

@Serializable
data class SessionCreateResponseDto(@SerialName("session_id") val sessionId: String)

/**
 * Every field except `session_id` (the true identity) defaults tolerantly: live-E2E testing found
 * the DEPLOYED API never sends `updated_at` on any session, even though the source
 * api-contract.md was verified against always emits it - the deployed backend lags the source, so
 * DTOs must not assume a field's presence just because the contract documents it.
 */
@Serializable
data class SessionSummaryDto(
    @SerialName("session_id") val sessionId: String,
    val title: String? = null,
    val status: String = "",
    /**
     * Drawer running-dot gate (spec §6.7). Verified against the live backend 2026-07-02: `status`
     * values in the wild are only {completed, failed, awaiting_approval, canceled, idle} - it never
     * reads literally "running" - this separate boolean on the sessions-list payload is the actual
     * liveness signal.
     */
    val running: Boolean = false,
    @SerialName("done_reason") val doneReason: String? = null,
    val origin: String? = null,
    val recoverable: Boolean = false,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
    /**
     * Hard-termination signal. `summarize_session` sets `status == "terminated"`,
     * `terminated == true`, and `recoverable == false` for a permanently terminated session — a
     * dead-end that beats even a still-unwinding live run. Both default tolerantly: an older
     * backend, or any live session, simply omits them.
     */
    val terminated: Boolean = false,
    @SerialName("terminated_at") val terminatedAt: String? = null,
) {
    fun toDomain() = SessionSummary(
        sessionId = sessionId,
        title = title,
        status = status,
        running = running,
        doneReason = doneReason,
        origin = origin,
        recoverable = recoverable,
        createdAt = createdAt ?: "",
        // Deployed API omits updated_at entirely - fall back to createdAt so list-sorting-by-recency
        // still has a usable value instead of an empty string when only one of the two is present.
        updatedAt = updatedAt ?: createdAt ?: "",
        terminated = terminated,
        terminatedAt = terminatedAt,
    )
}

@Serializable
data class SessionsListResponseDto(val sessions: List<SessionSummaryDto> = emptyList())

@Serializable
data class RenameSessionRequest(val title: String)

@Serializable
data class RenameSessionResponseDto(
    @SerialName("session_id") val sessionId: String,
    val title: String? = null,
)

@Serializable
data class ArchiveSessionResponseDto(
    @SerialName("session_id") val sessionId: String,
    val archived: Boolean = false,
)

@Serializable
data class SendMessageRequest(val text: String)

@Serializable
data class SendMessageResponseDto(
    @SerialName("session_id") val sessionId: String,
    val enqueued: Boolean = true,
    @SerialName("run_id") val runId: String? = null,
)

/**
 * `events` is decoded as raw [JsonElement]s, not `List<SessionEvent>` directly, so a malformed
 * individual frame can never fail the whole HTTP response decode - each element is mapped through
 * [com.mewbo.aura.data.model.SessionEvent.decode]'s resilient try/catch at the repo seam instead.
 */
@Serializable
data class SessionEventsResponseDto(
    @SerialName("session_id") val sessionId: String,
    val events: List<JsonElement> = emptyList(),
    val running: Boolean = false,
    val status: String? = null,
    @SerialName("done_reason") val doneReason: String? = null,
    val title: String? = null,
    val recoverable: Boolean = false,
    /**
     * Hard-termination signal. The events endpoint carries the authoritative terminal
     * state (`backend.py` `SessionEvents.get` emits `terminated`/`terminated_at` from
     * `summarize_session`). Drives the chat surface's terminal state when a terminated session is
     * OPENED (composer disabled, no Retry) — the pre-mutation half of the 410 path.
     */
    val terminated: Boolean = false,
    @SerialName("terminated_at") val terminatedAt: String? = null,
)

@Serializable
data class ModelsResponseDto(
    val models: List<String> = emptyList(),
    val default: String = "",
    val capabilities: Map<String, ModelCapabilityDto> = emptyMap(),
)

@Serializable
data class ModelCapabilityDto(@SerialName("supports_vision") val supportsVision: Boolean = false)

@Serializable
data class SessionQueryRequest(
    val query: String,
    val mode: String? = null,
    val context: JsonElement? = null,
    val attachments: List<AttachmentRecordDto>? = null,
)

/** Covers both response shapes `/query` can return (`SessionQueryAccepted` on 202,
 * `SessionStatusResponse` on 200) - only [accepted] is read, keyed off [Response.code] instead
 * (see [AuraApi.query]'s doc), so the unread fields of the 200 shape decode harmlessly via
 * `ignoreUnknownKeys`. */
@Serializable
data class SessionQueryResponseDto(
    @SerialName("session_id") val sessionId: String? = null,
    val accepted: Boolean = false,
)

@Serializable
data class RecoverSessionRequest(
    val action: String,
    @SerialName("from_ts") val fromTs: String? = null,
    val model: String? = null,
)

@Serializable
data class RecoverSessionResponseDto(
    @SerialName("session_id") val sessionId: String? = null,
    val accepted: Boolean = false,
    @SerialName("run_id") val runId: String? = null,
)

@Serializable
data class ForkSessionRequest(
    @SerialName("from_ts") val fromTs: String? = null,
    val model: String? = null,
)

@Serializable
data class ForkSessionResponseDto(
    @SerialName("session_id") val sessionId: String,
    @SerialName("forked_from") val forkedFrom: String? = null,
    @SerialName("forked_at") val forkedAt: String? = null,
)

/** `attachment_descriptor_model` (backend.py) - field-for-field, returned by the upload endpoint
 * and echoed verbatim into `SessionQueryRequest.attachments`. */
@Serializable
data class AttachmentRecordDto(
    val id: String,
    val filename: String,
    @SerialName("stored_name") val storedName: String,
    @SerialName("content_type") val contentType: String,
    @SerialName("size_bytes") val sizeBytes: Long,
    @SerialName("uploaded_at") val uploadedAt: String,
    val parsed: Boolean = false,
)

@Serializable
data class AttachmentsResponseDto(val attachments: List<AttachmentRecordDto> = emptyList())

@Serializable
data class ProjectDto(
    val name: String,
    val available: Boolean = true,
    val source: String = "config",
    @SerialName("project_id") val projectId: String? = null,
    @SerialName("is_worktree") val isWorktree: Boolean = false,
    val branch: String? = null,
) {
    fun toDomain() = ProjectSummary(
        name = name,
        available = available,
        source = source,
        projectId = projectId,
        isWorktree = isWorktree,
        branch = branch,
    )
}

@Serializable
data class ProjectsResponseDto(val projects: List<ProjectDto> = emptyList())

@Serializable
data class ToolDto(
    @SerialName("tool_id") val toolId: String,
    val name: String,
    val kind: String = "builtin",
    val enabled: Boolean = true,
    @SerialName("disabled_reason") val disabledReason: String? = null,
    val server: String? = null,
    // `global`/`project`/`plugin` — the backend has always sent this, but nothing
    // client-side read it until now. `scope == "plugin"` is what distinguishes a capability-gated
    // product tool (wiki_*, scg_*, agentic_search) from a plain core builtin (both are
    // `kind == "builtin"`) at the repository filter (SessionScopeRepository.tools()).
    val scope: String? = null,
) {
    fun toDomain() = ToolSummary(
        toolId = toolId,
        name = name,
        kind = kind,
        enabled = enabled,
        server = server,
        disabledReason = disabledReason,
        scope = scope,
    )
}

@Serializable
data class ToolsResponseDto(val tools: List<ToolDto> = emptyList())

/** Wire shape verbatim: `call_token` proves the caller is the device the call was
 * actually dispatched to; exactly one of [result]/[error] is populated depending on [status]. */
@Serializable
data class DeviceToolResultRequest(
    @SerialName("call_token") val callToken: String,
    val status: String,
    val result: JsonElement? = null,
    val error: DeviceToolErrorDto? = null,
)

@Serializable
data class DeviceToolErrorDto(val code: String, val message: String)

// ---- Mewbo Apps DTOs (see the `AuraApi` interface section above for the "not yet field-verified"
// caveat shared by every type below) --------------------------------------------------------------

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

/**
 * Answer body for `POST /api/sessions/{id}/questions/{callId}/answer` (ask-user wire contract):
 * the single-use `call_token` carried on the `user_question` event, plus one [QuestionAnswerItemDto]
 * per question. The backend FORBIDS extra keys (`set(body) - {"call_token","answers"}` ⇒ 400), which
 * is safe here because the request carries exactly these two fields and the shared `Json`
 * (`explicitNulls = false`, [com.mewbo.aura.di.DataModule]) drops the unused half of each item.
 */
@Serializable
data class QuestionAnswerRequest(
    @SerialName("call_token") val callToken: String,
    val answers: List<QuestionAnswerItemDto>,
)

/** One question's answer: `selected_indexes` XOR `text` — never both (`explicitNulls = false` drops
 * the null half on the wire). Single-select sends exactly one index, multi-select ≥1; free text is
 * always accepted. */
@Serializable
data class QuestionAnswerItemDto(
    @SerialName("selected_indexes") val selectedIndexes: List<Int>? = null,
    val text: String? = null,
)
