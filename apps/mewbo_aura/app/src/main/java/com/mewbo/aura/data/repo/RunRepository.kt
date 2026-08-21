package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.ApiErrorEnvelope
import com.mewbo.aura.data.api.AttachmentRecordDto
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.QuestionAnswerItemDto
import com.mewbo.aura.data.api.QuestionAnswerRequest
import com.mewbo.aura.data.api.RecoverSessionRequest
import com.mewbo.aura.data.api.SendMessageRequest
import com.mewbo.aura.data.api.SessionQueryRequest
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.device.DeviceToolDispatch
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.sse.SessionStreamClient
import com.mewbo.aura.di.ApplicationScope
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.onCompletion
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.shareIn
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import okhttp3.ResponseBody
import retrofit2.HttpException
import retrofit2.Response

/** `200`/`202` outcomes of either send route. */
sealed interface SendResult {
    /** `/message`'s `200`, or `/query`'s `202` - a new run started. */
    data class RunStarted(val runId: String) : SendResult

    /** `/message`'s `202` - steered an already-active run. Also what [RunRepository.sendQuery]
     * returns once it has re-routed a `409`-refused fresh turn onto the steer path. */
    data object Enqueued : SendResult

    /** `/query`'s `200` - a slash command (`/status`, `/terminate`) was handled inline; no run
     * started, nothing to stream. */
    data object SlashHandled : SendResult
}

/**
 * Outcome of answering a pending ask-user question ([RunRepository.answerQuestion]). The card's
 * authoritative settle is the `user_question_answered` SSE event, so this only steers the answer UX:
 * both [Resolved] and [AlreadyResolved] settle with NO error residue (a 404/409 means the event has
 * already fired or is about to), while [Failed] keeps the card interactive and surfaces a transient
 * notice.
 */
sealed interface QuestionAnswerResult {
    /** `200` — the backend accepted the answer; the blocked tool call resolves. */
    data object Resolved : QuestionAnswerResult

    /** `404`/`409` — already answered on another surface (or the question is gone). Settle silently. */
    data object AlreadyResolved : QuestionAnswerResult

    /** `403` bad token / `422` arity / `410` terminated / transport — a genuine failure. */
    data object Failed : QuestionAnswerResult
}

/**
 * Outcome of [RunRepository.interrupt].
 *
 * **None of these arms means "the run stopped", including [Interrupted].** The endpoint signals a
 * marker into the loop at its next turn boundary; the loop continues either way (the measurement is
 * on [com.mewbo.aura.data.api.AuraApi.interruptSession]). The arms exist so the ONE caller can tell
 * a delivered signal from an undelivered one for logging and for the terminated case — never so a
 * surface can report a run as ended.
 */
sealed interface InterruptResult {
    /** `202` — a live step was signalled. The run keeps going. */
    data object Interrupted : InterruptResult

    /** `200` — the session was idle; the documented idempotent no-op. Not an error: the client's
     * phase is a guess about what the server is doing, so aiming an interrupt at an already-finished
     * run is the ORDINARY case, not a bug (same reasoning as [RunRepository.sendQuery]'s `409`). */
    data object NoActiveRun : InterruptResult

    /** `410` — the session is permanently terminated, so there is nothing to interrupt and never
     * will be. Kept distinct from [Failed] because it is a settled fact rather than a transient one;
     * a caller that renders terminal state has everything it needs without a retry. */
    data object SessionTerminated : InterruptResult

    /** Transport failure or an unexpected status. Nothing was delivered. */
    data object Failed : InterruptResult
}

/**
 * A mutation was refused because the session is PERMANENTLY TERMINATED (HTTP 410, envelope
 * `code == "session_terminated"`). A DISTINCT type — not a bare [HttpException] — so
 * [com.mewbo.aura.ui.chat.ChatViewModel] can flip the pre-wired
 * [com.mewbo.aura.ui.chat.ChatUiState.sessionEnded] terminal state (composer disabled, Retry
 * dropped) instead of surfacing a retryable error that would 410 forever. The envelope is parsed
 * ONCE, at the send seam ([errorFor]), never per-screen. [reason] is the server's human-readable
 * message, carried for logging/diagnostics.
 */
class SessionTerminatedException(val reason: String) : Exception(reason)

/**
 * Data-layer seam fired the instant a run STARTS, so backgrounding the app mid-run still surfaces the
 * turn's completion. Kept a narrow `fun interface` HERE (not in `notify/`) so the dependency flows
 * strictly DOWN — `RunRepository` never imports the notification/service/`MainActivity` code that
 * implements it (`com.mewbo.aura.notify.RunNotificationLauncher`, bound in `di/NotifyModule`), the
 * same shape [DeviceToolDispatch] uses. [preview] is the query text, carried to the completion
 * notification's body so the user knows which request finished; `null` for a retry (no fresh text).
 */
fun interface RunNotifications {
    /**
     * [deviceControl] says this run may drive the phone, so the watch must hold
     * its subscription past the run's terminal event. Without it the channel
     * that delivers device tools dies ~5s after the UI stops collecting — and
     * launching another app is what stops the UI collecting.
     *
     * Asked of [com.mewbo.aura.data.device.DeviceControlSession], which owns the
     * fact: a grant already held, or one this run could still take. Both need
     * the channel, and neither is knowable from a per-request toggle read.
     */
    fun onRunStarted(sessionId: String, preview: String?, deviceControl: Boolean)
}

/**
 * `/query`'s refusal when a run is already active for the session. NOT an error condition on this
 * client: it is the only signal the server gives that a turn the caller believed was fresh is
 * actually a steer, so [RunRepository.sendQuery] routes on it rather than raising. `/message` has
 * no equivalent - it services both states - which is why the constant is read at exactly one site.
 */
private const val HTTP_RUN_ALREADY_ACTIVE = 409

/**
 * Sends a message into a session and follows its live event stream. Two distinct routes, per the
 * task brief (mirrors the web console): [send] is the steer path (`/message`, text-only - no
 * attachments field exists on that endpoint); [sendQuery] is the fresh-turn path (`/query`, carries
 * `context` + `attachments`). [com.mewbo.aura.ui.chat.ChatViewModel] picks the route from the
 * CURRENT [com.mewbo.aura.ui.chat.RunPhase] before either network call.
 *
 * **That phase is a guess, and [sendQuery] corrects it from the response.** `RunPhase` describes
 * what this client is WATCHING, not what the server is DOING, and the two diverge by design every
 * time the client stops following a run that is still going. Only the server knows, so a `409`
 * ("Session is already running.") is treated as the authoritative answer and the turn is re-routed
 * onto [send] - see [sendQuery]'s own doc. The asymmetry is deliberate: `/message` is correct in
 * BOTH states (`202` enqueue while running, `200` re-engage when idle), so only the `/query` leg
 * can be wrong about liveness and only it needs the correction.
 *
 * **`@Singleton` (added with the turn-completion notification feature).** [live] hands out a
 * per-session multicast so every follower shares ONE SSE connection ([liveStreams]); that only holds
 * across DIFFERENT callers if they share the repository. It was previously unscoped because its only
 * followers were the chat `ViewModel` and the overlay, and the `@Singleton` `DeviceToolExecutor`'s
 * ledger already deduped device-tool answers across instances. The notification watcher
 * ([com.mewbo.aura.notify.RunNotificationController]) is a THIRD follower whose whole point is to
 * ride the connection chat already has open — so the repository is now a genuine singleton, which is
 * also the "share one connection" intent [liveStreams] documents. Safe: its only state is that
 * session-keyed, self-evicting cache; nothing per-injector lived here.
 */
@Singleton
class RunRepository @Inject constructor(
    private val api: AuraApi,
    private val streamClient: SessionStreamClient,
    private val deviceToolCatalog: DeviceToolCatalog,
    private val deviceControlSession: DeviceControlSession,
    private val deviceToolDispatch: DeviceToolDispatch,
    private val json: Json,
    private val runNotifications: RunNotifications,
    @ApplicationScope private val scope: CoroutineScope,
) {
    /** Per-session multicast cache over [SessionStreamClient.stream] (a cold flow - every
     * individual `collect` would otherwise open its own SSE connection): a session's transcript
     * collector and its [DeviceToolDispatch] call [live] for the SAME session id and must share one
     * underlying connection, not open two (task brief). See [buildMulticastLiveFlow]'s own doc for
     * why entries are evicted on upstream completion (review finding F1) - without that, a session's
     * cached flow goes permanently dead the moment its FIRST run's stream completes, silently
     * breaking every later turn in that same session.
     */
    private val liveStreams = ConcurrentHashMap<String, SharedFlow<SessionEvent>>()

    /** `enqueued` is `true` on both `200` and `202` responses (api-contract.md section 3) - the
     * HTTP status code is the only reliable discriminator, never the body's `enqueued` flag. */
    suspend fun send(sessionId: String, text: String): SendResult {
        val response = api.sendMessage(sessionId, SendMessageRequest(text))
        if (!response.isSuccessful) throw errorFor(json, response)
        val body = response.body() ?: throw HttpException(response)
        return if (response.code() == 200) {
            // 200 = a NEW run started (steering an active one is 202 Enqueued, whose watch already
            // exists from the send that started that run — never re-armed here).
            runNotifications.onRunStarted(sessionId, preview = text, deviceControl = deviceControlInPlay())
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
     *
     * **A `409` is not a failure here - it is the server telling us this turn is a STEER.** `/query`
     * refuses a second concurrent run (`{"message": "Session is already running."}`), and the caller
     * reaches this route whenever [com.mewbo.aura.ui.chat.RunPhase] has left `Sending`/`Streaming`
     * while the run itself has not ended: a `stop()` (a CLIENT-side detach by contract - the backend
     * run keeps going), a `stream_error` (which means "you are no longer seeing this run", not "the
     * run failed"), or the live collector failing. In every one of those the user is looking at a
     * session the server still considers busy, so their follow-up is exactly the steer [send]
     * exists for - and before this re-route it surfaced as a raw `HTTP 409` error card instead of a
     * queued message.
     *
     * Re-routing rather than pre-checking liveness is what keeps the happy path at ONE round trip,
     * and it is strictly safer than asking first: a liveness read is stale the moment it returns,
     * whereas the `409` IS the server's answer about the very request being made. The web console
     * routes on a polled `activeSession.running` and carries that same race with no recovery.
     *
     * Attachments cannot follow onto `/message` (no such field), so a re-routed turn sends text
     * only - the identical limitation the console's own running branch has, which skips the upload
     * entirely. They stay uploaded against the session; they simply do not ride THIS turn.
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
        // Checked BEFORE the generic non-2xx throw - a 409 is a routing correction, not an error.
        if (response.code() == HTTP_RUN_ALREADY_ACTIVE) return send(sessionId, text)
        if (!response.isSuccessful) throw errorFor(json, response)
        return if (response.code() == 202) {
            // 202 = a run started; 200 = a slash command handled inline (nothing to follow/notify).
            runNotifications.onRunStarted(
                sessionId,
                preview = text,
                deviceControl = deviceControlInPlay(),
            )
            SendResult.RunStarted(runId = sessionId)
        } else {
            SendResult.SlashHandled
        }
    }

    /**
     * Retry/rewind: the backend TRUNCATES this session's transcript at [fromTs] before re-running
     * it - a destructive in-place REWIND of the SAME session, not a new one (contrast
     * [SessionRepository.forkSession], which never touches the source). Goes through [errorFor]
     * exactly like [send]/[sendQuery], so a 410 raises [SessionTerminatedException] instead of a
     * bare [HttpException].
     */
    suspend fun retryFrom(sessionId: String, fromTs: String, model: String?): SendResult {
        val response = api.recoverSession(sessionId, RecoverSessionRequest(action = "retry", fromTs = fromTs, model = model))
        if (!response.isSuccessful) throw errorFor(json, response)
        // A retry always starts a run; there is no fresh query text, so the notification falls back
        // to its generic body.
        runNotifications.onRunStarted(sessionId, preview = null, deviceControl = deviceControlInPlay())
        return SendResult.RunStarted(runId = response.body()?.runId ?: sessionId)
    }

    /**
     * Signals the backend that the user wants the current run to stop, and reports only what was
     * DELIVERED — never whether anything stopped, because measurably nothing has to.
     *
     * **Deliberately unconditional: never gate this on believing a run is live.** The endpoint
     * documents itself as "always safe to make" and answers `200`/`interrupted: false` on an idle
     * session, while this client's [com.mewbo.aura.ui.chat.RunPhase] describes what it is WATCHING
     * rather than what the server is DOING — the two diverge by design (see this class's own doc and
     * [sendQuery]'s `409` re-route). A pre-check would therefore be both a wasted round trip and
     * stale by the time it returned; the response IS the server's answer about this very request.
     *
     * Never throws. Every failure is an arm of [InterruptResult], because the sole caller
     * ([com.mewbo.aura.ui.chat.ChatViewModel.stop]) fires this beside work that must happen whether
     * or not the network is reachable — an exception here would take that work with it. This is the
     * ONE method on this class that does not route through [errorFor]: [errorFor] exists to turn a
     * `410` into a typed throw for the three routes that START a run, where a terminated session must
     * abort the send. Interrupting a terminated session aborts nothing, so it reports
     * [InterruptResult.SessionTerminated] as data instead.
     */
    suspend fun interrupt(sessionId: String): InterruptResult = runCatching {
        interpretInterruptResponse(api.interruptSession(sessionId))
    }.getOrElse { e ->
        if (e is kotlinx.coroutines.CancellationException) throw e
        InterruptResult.Failed
    }

    /**
     * Delivers the human's [answers] (plus optional group-level [notes]) for an ask-user question
     * ([callId]/[callToken] from the originating `user_question` event) — whether the card is still
     * pending OR the run already stopped waiting ([com.mewbo.aura.data.model.QuestionResolution.RunMovedOn]);
     * the endpoint accepts both and reports which happened via the answered SSE event's `delivery`.
     * This is a HUMAN-tap answer, wired from the chat surface's ViewModel — deliberately NOT part of
     * `live()`'s auto-serviced `device_tool_call` pipeline (that answers the machine automatically; a
     * question waits for a person). Status-code interpretation lives in the testable top-level
     * [interpretAnswerResponse]; the [ResponseBody] is closed either way (the wire forbids retrying,
     * exactly like [AuraApi.postDeviceToolResult]).
     */
    suspend fun answerQuestion(
        sessionId: String,
        callId: String,
        callToken: String,
        answers: List<QuestionAnswerItemDto>,
        notes: String? = null,
    ): QuestionAnswerResult {
        val response = api.answerQuestion(sessionId, callId, QuestionAnswerRequest(callToken = callToken, answers = answers, notes = notes))
        return try {
            interpretAnswerResponse(response)
        } finally {
            response.body()?.close()
            response.errorBody()?.close()
        }
    }

    /**
     * The ONE seam every path that follows a run goes through: `ChatViewModel.subscribeLive`, and
     * the assist overlay via `AuraSession`'s `liveEvents = { id -> runRepository.live(id) }` lambda
     * into `AssistTurnMachine`. The flow it hands out has [deviceToolDispatch] built INTO it - see
     * [buildMulticastLiveFlow] (this method's whole body, and its only logic) and
     * [DeviceToolDispatch]'s KDoc for why device-tool servicing lives in the pipeline rather than in
     * either caller.
     */
    fun live(sessionId: String): Flow<SessionEvent> =
        buildMulticastLiveFlow(sessionId, streamClient.stream(sessionId), liveStreams, scope, deviceToolDispatch)

    /**
     * Whether this run needs the device-tool channel held past its terminal event.
     *
     * True for a grant already held AND for one this run could still take, because the
     * hold has to be armed at run START and the grant is taken part-way through it: the
     * model calls `device_control_start` several steps in, by which time the only chance
     * to arm the watch is long gone. Arming on "could take control" is therefore not
     * over-arming — it is the only ordering the platform allows, since Android forbids
     * starting a foreground service once the app is in the background, which is exactly
     * where a run that drives the phone ends up.
     */
    private fun deviceControlInPlay(): Boolean =
        deviceControlSession.isActive() || deviceControlSession.canTakeControl()
}

/**
 * Maps a NON-2xx [response] to the most specific exception it implies. A permanently terminated
 * session (410 + envelope `code == "session_terminated"`) becomes a
 * [SessionTerminatedException] the UI renders as its terminal state; everything else stays a generic
 * [HttpException] — the earlier behaviour, byte-identical for every other error.
 *
 * Pulled out as a top-level function (exactly like [buildMulticastLiveFlow]) so
 * [RunRepository]'s own test suite can drive it with a synthetic `Response.error(...)` without
 * constructing a full [RunRepository], whose SSE collaborators aren't JVM-constructible. It is the
 * ONE place both send routes ([RunRepository.send]/[RunRepository.sendQuery]) read the error body,
 * so error-envelope handling never leaks into a screen. Reading the body never throws: an absent or
 * non-envelope body (or one whose code isn't `session_terminated`) falls through to [HttpException].
 */
internal fun errorFor(json: Json, response: Response<*>): Throwable {
    val envelope = response.errorBody()?.string()?.let { body ->
        runCatching { json.decodeFromString<ApiErrorEnvelope>(body) }.getOrNull()
    }?.error
    return if (envelope?.code == "session_terminated") {
        SessionTerminatedException(envelope.reason ?: "This session is permanently terminated")
    } else {
        HttpException(response)
    }
}

/**
 * Maps an answer-POST [response] to a [QuestionAnswerResult]. Pulled top-level (like [errorFor]) so
 * [RunRepository]'s own test suite can drive it with a synthetic `Response.success/error(...)` without
 * a full [RunRepository]. `200` ⇒ [QuestionAnswerResult.Resolved]; `404`/`409` ⇒
 * [QuestionAnswerResult.AlreadyResolved] (answered elsewhere — the `user_question_answered` event
 * settles the card, so this is not an error); every other non-2xx (`403`/`422`/`410`/transport) ⇒
 * [QuestionAnswerResult.Failed].
 */
/**
 * Maps an interrupt-POST [response] to an [InterruptResult]. Pulled top-level (like [errorFor] and
 * [interpretAnswerResponse]) so the test suite can drive every branch with a synthetic
 * `Response.success/error(...)` rather than a live run.
 *
 * The `202`-vs-`200` split is read off the STATUS, not off the body's `interrupted` flag, for the
 * reason [AuraApi.sendMessage]'s own 200/202 branch is: the flag restates the code and adds nothing,
 * while a tolerantly-defaulted DTO field would read `false` on a body that never carried it.
 */
internal fun interpretInterruptResponse(response: Response<*>): InterruptResult = when (response.code()) {
    202 -> InterruptResult.Interrupted
    200 -> InterruptResult.NoActiveRun
    410 -> InterruptResult.SessionTerminated
    else -> InterruptResult.Failed
}

internal fun interpretAnswerResponse(response: Response<ResponseBody>): QuestionAnswerResult = when {
    response.isSuccessful -> QuestionAnswerResult.Resolved
    response.code() == 404 || response.code() == 409 -> QuestionAnswerResult.AlreadyResolved
    else -> QuestionAnswerResult.Failed
}

/** How long the shared upstream (and therefore the real SSE connection) survives its LAST
 * subscriber leaving. Not a tuning knob - it closes a specific hole. `ChatViewModel.subscribeLive`
 * re-subscribes with a cancel-old-then-collect-new pattern, and both of its mid-run callers are
 * reachable with the OLD collector still active (`send`'s steer path when `/message` answers `200`
 * RunStarted; and the fresh-turn path in the window between `completion` and `stream_end`, where
 * `runPhase` is already `Done` but the collector is still running). Cancellation and the new
 * `launch` are both dispatched, so between them the subscriber count can transit 1 -> 0 -> 1; at
 * `stopTimeoutMillis = 0` that transient zero lets `WhileSubscribed` stop the upstream, which evicts
 * the cache entry (`onCompletion`) and tears down + re-opens the real SSE connection MID-RUN. Until
 * now `DeviceToolExecutor`'s own permanent subscription accidentally held the count above zero and
 * hid this (`buildMulticastLiveFlow`'s doc even cited it as a reason the transition "never happens");
 * that subscriber is gone, so the gap is closed explicitly instead - the standard idiom, and no
 * phantom subscriber required. 5s comfortably covers a dispatch hop while still reaping an abandoned
 * run's connection promptly. */
private const val LIVE_STREAM_STOP_TIMEOUT_MS = 5_000L

/**
 * The `shareIn` wrapping [RunRepository.live] applies to a session's raw upstream, plus the
 * [DeviceToolDispatch] that makes its `device_tool_call` events actually get answered - pulled into
 * its own top-level function (taking the raw upstream `Flow` + the cache + the dispatch directly,
 * rather than going through a real [SessionStreamClient]/OkHttp) so [RunRepository]'s own test suite
 * can drive it with a synthetic upstream instead of a real SSE connection.
 *
 * **Device-tool dispatch is a step in this PIPELINE ([onEach]), deliberately not a second
 * subscriber.** It sits UPSTREAM of the [shareIn], which is what buys all four of these at once:
 * dispatch sees every event regardless of who is subscribed when it arrives (a `replay = 0`
 * `SharedFlow` otherwise makes that a race); it needs no terminal-event detection, because it lives
 * and dies with the upstream itself, however that upstream ends; it can't pin the subscriber count
 * above zero and so can't keep an abandoned run's SSE reconnecting forever; and each session's flow
 * carries its own, so one session can never cancel another's dispatch. [DeviceToolDispatch]'s KDoc
 * has the full history - every one of those was a real defect in the design where the executor
 * subscribed to this flow instead. [DeviceToolDispatch.dispatch] must not suspend (it launches the
 * call on an app-lifetime scope and returns), or it would stall this pipeline and freeze chat
 * rendering mid-turn; a reconnect's full-backlog replay re-dispatches the same calls, which the
 * executor's own ledger makes idempotent.
 *
 * Two review findings (F1, F2) both trace back to the SAME root cause: [SessionStreamClient.stream]
 * is a COLD flow that COMPLETES once it sees `stream_end` (its own terminal-frame handling), but
 * `shareIn`'s `WhileSubscribed` only restarts collection on a subscriber-count transition back to 0.
 * Without eviction, a completed session's cached `SharedFlow` goes permanently dead and every LATER
 * turn in that same session silently receives nothing (frozen transcript, eternal spinner, device
 * calls never dispatched - F1). [onCompletion] evicts the cache entry the moment the underlying
 * upstream stops for ANY reason (`stream_end`, or `WhileSubscribed`'s own no-subscribers stop), so
 * the NEXT [RunRepository.live] call for this session id always builds a fresh upstream connection
 * instead of returning the dead one. (F1's original note claimed the count "never" reaches 0,
 * because the device-tool executor was then a permanent SECOND subscriber holding it up. It no
 * longer is - dispatch moved into the pipeline above - so the count really can reach 0, transiently,
 * mid-run. That is precisely what [LIVE_STREAM_STOP_TIMEOUT_MS] now covers; eviction-on-completion
 * remains necessary either way.)
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
    dispatch: DeviceToolDispatch,
): Flow<SessionEvent> = cache.getOrPut(sessionId) {
    rawUpstream
        .onEach { if (it is SessionEvent.DeviceToolCall) dispatch.dispatch(sessionId, it.payload) }
        .catch { e -> emit(SessionEvent.StreamError(message = e.message ?: e.toString())) }
        // **Nothing about the device-control GRANT may hang off this completion.** It briefly did,
        // and the reasoning was wrong in a way worth recording: upstream completion reads like "the
        // run is over", but it fires on every `WhileSubscribed` stop AND on each of the hold's
        // transport rebuilds — so a grant released here is revoked every few seconds, by the very
        // hold that exists to protect it. Releasing a grant belongs to the hold's own epoch, which
        // is the unit that actually means "the agent is done driving". See `notify/CLAUDE.md`.
        .onCompletion { cache.remove(sessionId) }
        .shareIn(
            scope = scope,
            started = SharingStarted.WhileSubscribed(stopTimeoutMillis = LIVE_STREAM_STOP_TIMEOUT_MS),
            replay = 0,
        )
}
