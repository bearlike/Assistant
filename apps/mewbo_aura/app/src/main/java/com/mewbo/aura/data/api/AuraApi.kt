package com.mewbo.aura.data.api

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
     * dispatched over SSE (Gitea #179 wire contract). `200` = resolved; `403` = bad token; `404` =
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
}

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
    // Gitea #182 P1: `global`/`project`/`plugin` — the backend has always sent this, but nothing
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

/** Wire shape verbatim (Gitea #179): `call_token` proves the caller is the device the call was
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
