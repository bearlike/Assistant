package com.mewbo.aura.data.api

import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.DELETE
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

    /**
     * Recents listing. **Cost: `O(collection)`, bounded by [limit].**
     *
     * [limit] is REQUIRED and deliberately carries NO default: a default of "everything" is the
     * fail-open written once in the callee, while a required parameter turns every missed call site
     * into a compile error. Measured against the deployed API — unbounded: 721 rows, 3,071,030
     * bytes, 1.77 s; `limit=50`: 46 rows, 413,708 bytes, 0.17 s.
     *
     * Two server-side semantics the caller depends on. [limit] bounds the CANDIDATES examined, not
     * the rows returned — a candidate carrying no visible turn is examined and yields no row, which
     * is why 50 candidates return 46 rows. And the page is cut AFTER `include_archived` narrows the
     * candidate set, over a pinned-first-then-newest-first ordering, so **a pinned session can never
     * be evicted by the bound** (which is what lets
     * [com.mewbo.aura.ui.sessions.SessionGrouping]'s pinned bucket stay correct under it).
     */
    @GET("api/sessions")
    suspend fun listSessions(
        @Query("include_archived") includeArchived: Boolean = false,
        @Query("limit") limit: Int,
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
     * Pin / unpin, ONE path mirroring [archiveSession]'s own POST-then-DELETE shape exactly (the
     * server's pin route is deliberately the same Resource pattern, not a second `/unpin` path).
     * Pinning is an ORDERING signal, never a filter bypass — the server sorts pinned-first, and
     * every active filter still applies to a pinned session (this client narrows to
     * [com.mewbo.aura.ui.sessions.RecentsFilter] rows entirely client-side, so a pinned session
     * outside the active scope simply never reaches the ordering step).
     *
     * Plain-DTO return, so any non-2xx surfaces as a [retrofit2.HttpException] and degrades to
     * `false` in [com.mewbo.aura.data.repo.SessionRepository.setPinned] — the same error path
     * [archiveSession] uses.
     */
    @POST("api/sessions/{id}/pin")
    suspend fun pinSession(@Path("id") sessionId: String): PinSessionResponseDto

    @DELETE("api/sessions/{id}/pin")
    suspend fun unpinSession(@Path("id") sessionId: String): PinSessionResponseDto

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
     * Asks the backend to interrupt the currently executing step of an active run.
     *
     * **MEASURED, and it contradicts the endpoint's name: this does NOT end the run.** Against the
     * deployed API, a session running five sequential `sleep 20` shell calls answered `202`
     * `{"interrupted": true}`, then executed THREE more of them and finished normally
     * (`done_reason: "completed"`, all five iterations) 91 seconds later. The engine explains why:
     * `SessionRuntime.interrupt_step` only sets a `threading.Event`, and `ToolUseLoop` reads it at
     * the TOP OF THE NEXT TURN, clears it, and appends the one-line marker
     * `[System: Current step interrupted by user.]` as an ordinary `HumanMessage`. `state.done` is
     * never touched, so the loop continues. It is a STEER that asks the model to stop, and the model
     * is free to ignore it — measurably, it does.
     *
     * Two things it genuinely delivers, which is why it is still worth calling: the marker reaches
     * the model, and a run blocked on an `ask_user_question` is released (`ask_user.py` polls the
     * same event). Callers must not report it to a person as "the run stopped".
     *
     * `202` a live step was interrupted / `200` the session was idle, an idempotent no-op
     * (`interrupted: false`) / `410` the session is permanently terminated. All three measured. The
     * [Response] wrapper is load-bearing for the same reason [sendMessage]'s is — 200 and 202 are
     * both successes carrying different facts, and the body's `interrupted` flag merely restates the
     * code. Interpreted by the testable top-level
     * [com.mewbo.aura.data.repo.interpretInterruptResponse].
     */
    @POST("api/sessions/{id}/interrupt")
    suspend fun interruptSession(@Path("id") sessionId: String): Response<SessionInterruptResponseDto>

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

    // Speech (`/api/speech/*`) is deliberately NOT here — it lives on [SpeechApi], because its
    // calls need a longer `callTimeout` than this interface's shared client provides, and a
    // timeout belongs to a client rather than to a method. Its own KDoc has the reasoning.

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

    // ---- Mewbo Apps (design: apps/mewbo_api/src/mewbo_api/apps/CLAUDE.md) ----
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
     * Delivers a human answer for a pending `user_question` event. `200` = delivered
     * (`{"resolved":true,"delivery":"run"|"message"}` — `"run"` resolved the still-blocked tool call,
     * `"message"` means the run had already moved on so the answer landed as a new chat message
     * instead; this is the SAME endpoint whether the card is still pending or the run already
     * timed out/declined/etc., since either way it's the one place a human answer can land). `403` bad
     * token / `404` unknown-or-already-read / `409` already delivered / `422` answers don't fit / `410`
     * session terminated. The authoritative card settle is the `user_question_answered` SSE event
     * (which now also carries `delivery`), so `404`/`409` mean it already fired (settle silently,
     * answered-elsewhere); `422` is user-actionable, everything else a genuine failure. Raw
     * [ResponseBody] like [postDeviceToolResult] — the caller branches on [Response.code], and a `200`
     * body isn't decoded here (the SSE event is the source of truth, not this response). The
     * `X-Mewbo-Surface` header (stamped globally by [com.mewbo.aura.di.AuthInterceptor]) is recorded
     * server-side as `answered_via`.
     */
    @POST("api/sessions/{sessionId}/questions/{callId}/answer")
    suspend fun answerQuestion(
        @Path("sessionId") sessionId: String,
        @Path("callId") callId: String,
        @Body request: QuestionAnswerRequest,
    ): Response<ResponseBody>
}
