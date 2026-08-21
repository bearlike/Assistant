package com.mewbo.aura.data.api

import com.mewbo.aura.data.model.SessionSummary
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import retrofit2.Response

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
     * Drawer running-dot gate (spec §6.7). Verified against the live backend: `status`
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
    /**
     * Pin state. The server emits BOTH keys only for a pinned session — an unpinned row carries
     * NEITHER, so absent must read as not-pinned (hence the `false`/`null` defaults, same tolerant
     * shape as [terminated]). `pinned_at` is server-assigned; the client never mints one.
     */
    val pinned: Boolean = false,
    @SerialName("pinned_at") val pinnedAt: String? = null,
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
        pinned = pinned,
        pinnedAt = pinnedAt,
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

/**
 * Pin/unpin acknowledgement. **Every field defaults, `session_id` included** — unlike the
 * neighbouring response DTOs, whose shapes are verified against a shipped endpoint. This one is
 * written against a route that did not yet exist in `backend.py` when it was added, so a narrower
 * response body (`{"ok": true}`, a bare `{}`) must still decode rather than throwing a
 * `MissingFieldException` on a call the server actually honoured. The repository therefore treats
 * the REQUESTED state as the truth and reads [pinnedAt] only as a bonus; server truth arrives with
 * the next `listSessions` refresh either way.
 */
@Serializable
data class PinSessionResponseDto(
    @SerialName("session_id") val sessionId: String? = null,
    val pinned: Boolean = false,
    @SerialName("pinned_at") val pinnedAt: String? = null,
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

/**
 * `POST /sessions/{id}/interrupt`'s acknowledgement — `{"session_id", "interrupted"}`, verified
 * against the deployed API on both branches (`202` `interrupted: true`, `200` `interrupted: false`).
 *
 * Both fields default, so a narrower body still decodes; [interrupted] is decoded but deliberately
 * NOT the discriminator the caller reads. It only restates the HTTP status, and the status is the
 * shape every other two-success route in this interface already branches on
 * ([SendMessageResponseDto]'s `enqueued` carries the identical caveat). Note that
 * `interrupted: true` means "a live step was signalled", never "the run ended" — see
 * [AuraApi.interruptSession] for the measurement.
 */
@Serializable
data class SessionInterruptResponseDto(
    @SerialName("session_id") val sessionId: String? = null,
    val interrupted: Boolean = false,
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

/**
 * Answer body for `POST /api/sessions/{id}/questions/{callId}/answer` (ask-user wire contract):
 * the single-use `call_token` carried on the `user_question` event, one [QuestionAnswerItemDto] per
 * question, and an optional group-level [notes] (posted only when the user typed into the
 * `notes_placeholder` field — never an empty string). The backend FORBIDS extra keys
 * (`set(body) - {"call_token","answers","notes"}` ⇒ 400), which is safe here because the request
 * carries exactly these fields and the shared `Json` (`explicitNulls = false`,
 * [com.mewbo.aura.di.DataModule]) drops a `null` [notes] off the wire entirely.
 */
@Serializable
data class QuestionAnswerRequest(
    @SerialName("call_token") val callToken: String,
    val answers: List<QuestionAnswerItemDto>,
    val notes: String? = null,
)

/** One question's answer: `selected_indexes` XOR `text` — never both (`explicitNulls = false` drops
 * the null half on the wire). Single-select sends exactly one index, multi-select ≥1; free text is
 * always accepted. */
@Serializable
data class QuestionAnswerItemDto(
    @SerialName("selected_indexes") val selectedIndexes: List<Int>? = null,
    val text: String? = null,
)
