package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AttachmentRecordDto
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.SendMessageRequest
import com.mewbo.aura.data.api.SessionQueryRequest
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.sse.SessionStreamClient
import com.mewbo.aura.di.ApplicationScope
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.onCompletion
import kotlinx.coroutines.flow.shareIn
import retrofit2.HttpException

/** `200`/`202` outcomes of either send route. */
sealed interface SendResult {
    /** `/message`'s `200`, or `/query`'s `202` - a new run started. */
    data class RunStarted(val runId: String) : SendResult

    /** `/message`'s `202` - steered an already-active run. */
    data object Enqueued : SendResult

    /** `/query`'s `200` - a slash command (`/status`, `/terminate`) was handled inline; no run
     * started, nothing to stream. */
    data object SlashHandled : SendResult
}

/**
 * Sends a message into a session and follows its live event stream. Two distinct routes, per the
 * task brief (mirrors the web console): [send] is the steer path (`/message`, active-run only,
 * text-only - no attachments field exists on that endpoint); [sendQuery] is the fresh-turn path
 * (`/query`, carries `context` + `attachments`). [com.mewbo.aura.ui.chat.ChatViewModel] picks the
 * route from the CURRENT [com.mewbo.aura.ui.chat.RunPhase] before either network call, not from
 * either response.
 */
class RunRepository @Inject constructor(
    private val api: AuraApi,
    private val streamClient: SessionStreamClient,
    private val deviceToolCatalog: DeviceToolCatalog,
    @ApplicationScope private val scope: CoroutineScope,
) {
    /** Per-session multicast cache over [SessionStreamClient.stream] (a cold flow - every
     * individual `collect` would otherwise open its own SSE connection): both `ChatViewModel`'s
     * transcript collector and [com.mewbo.aura.data.device.DeviceToolExecutor] call [live] for the
     * SAME session id and must share one underlying connection, not open two (task brief). See
     * [buildMulticastLiveFlow]'s own doc for why entries are evicted on upstream completion
     * (review finding F1) - without that, a session's cached flow goes permanently dead the moment
     * its FIRST run's stream completes, silently breaking every later turn in that same session.
     */
    private val liveStreams = ConcurrentHashMap<String, SharedFlow<SessionEvent>>()

    /** `enqueued` is `true` on both `200` and `202` responses (api-contract.md section 3) - the
     * HTTP status code is the only reliable discriminator, never the body's `enqueued` flag. */
    suspend fun send(sessionId: String, text: String): SendResult {
        val response = api.sendMessage(sessionId, SendMessageRequest(text))
        if (!response.isSuccessful) throw HttpException(response)
        val body = response.body() ?: throw HttpException(response)
        return if (response.code() == 200) {
            SendResult.RunStarted(runId = body.runId ?: sessionId)
        } else {
            SendResult.Enqueued
        }
    }

    /**
     * `context` is rebuilt fresh on EVERY call (not just session creation) - see
     * [buildSessionContext]'s doc for why the backend requires that. `attachments`, when non-empty,
     * must already be uploaded records ([AttachmentRepository.upload]'s return value) - this method
     * does no upload of its own.
     */
    suspend fun sendQuery(
        sessionId: String,
        text: String,
        model: String?,
        project: String?,
        mcpTools: List<String>?,
        attachments: List<AttachmentRecordDto>,
    ): SendResult {
        val context = buildSessionContext(
            model = model,
            project = project,
            mcpTools = mcpTools,
            deviceTools = deviceToolCatalog.availableTools(),
        )
        val response = api.query(
            sessionId,
            SessionQueryRequest(query = text, mode = "act", context = context, attachments = attachments.ifEmpty { null }),
        )
        if (!response.isSuccessful) throw HttpException(response)
        return if (response.code() == 202) SendResult.RunStarted(runId = sessionId) else SendResult.SlashHandled
    }

    fun live(sessionId: String): Flow<SessionEvent> =
        buildMulticastLiveFlow(sessionId, streamClient.stream(sessionId), liveStreams, scope)
}

/**
 * The `shareIn` wrapping [RunRepository.live] applies to a session's raw upstream, pulled into its
 * own top-level function (taking the raw upstream `Flow` + the cache directly, rather than going
 * through a real [SessionStreamClient]/OkHttp) so [RunRepository]'s own test suite can drive it
 * with a synthetic upstream instead of a real SSE connection.
 *
 * Two review findings (F1, F2) both trace back to the SAME root cause: [SessionStreamClient.stream]
 * is a COLD flow that COMPLETES once it sees `stream_end` (its own terminal-frame handling), but
 * `shareIn`'s `WhileSubscribed` only restarts collection on a subscriber-count transition back to
 * 0 - which never happens here, because `ChatViewModel.subscribeLive`'s async cancel-old/attach-new
 * pattern keeps at least one subscriber (the transcript collector or the device-tool executor)
 * present across turns. Without eviction, a completed session's cached `SharedFlow` goes
 * permanently dead and every LATER turn in that same session silently receives nothing (frozen
 * transcript, eternal spinner, device calls never dispatched - F1). [onCompletion] evicts the cache
 * entry the moment the underlying upstream stops for ANY reason (`stream_end`, or `WhileSubscribed`'s
 * own no-subscribers stop), so the NEXT [RunRepository.live] call for this session id always builds
 * a fresh upstream connection instead of returning the dead one.
 *
 * [catch] materializes an upstream exception into a [SessionEvent.StreamError] VALUE rather than
 * letting it propagate as a thrown exception: once a flow is `shareIn`-wrapped, an upstream throw
 * executes inside the sharing coroutine (on [scope], an app-wide `SupervisorJob` with no installed
 * exception handler) and would otherwise surface as an UNCAUGHT exception there - process kill on
 * Android (F2) - instead of ever reaching a collector's own try/catch, which only ever wraps its
 * OWN `collect` call, not `shareIn`'s internal one. `catch` doesn't touch `CancellationException`
 * (Flow's own contract), so a genuine `WhileSubscribed` stop is never mistaken for a failure.
 */
internal fun buildMulticastLiveFlow(
    sessionId: String,
    rawUpstream: Flow<SessionEvent>,
    cache: MutableMap<String, SharedFlow<SessionEvent>>,
    scope: CoroutineScope,
): Flow<SessionEvent> = cache.getOrPut(sessionId) {
    rawUpstream
        .catch { e -> emit(SessionEvent.StreamError(message = e.message ?: e.toString())) }
        .onCompletion { cache.remove(sessionId) }
        .shareIn(scope = scope, started = SharingStarted.WhileSubscribed(), replay = 0)
}
