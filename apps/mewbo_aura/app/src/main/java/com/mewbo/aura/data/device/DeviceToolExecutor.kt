package com.mewbo.aura.data.device

import com.mewbo.aura.data.api.DeviceToolErrorDto
import com.mewbo.aura.data.api.DeviceToolResultRequest
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.di.ApplicationScope
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject

/**
 * Dispatches `device_tool_call` events off whichever session's live SSE flow it's currently
 * [attach]ed to, per call: dedup ([DeviceToolCallLedger]) -> staleness (`now > expires_at`) ->
 * run the matching [DeviceToolHandler] -> POST the result ([DeviceToolResultReporter]) - order
 * fixed by the task brief. A Hilt singleton (data/ layer, zero Compose/UI imports): exactly one
 * [attach]ed session's worth of dispatch is live at a time, tracked by [job].
 *
 * [attach] takes the caller's own already-multicast `Flow<SessionEvent>`
 * ([com.mewbo.aura.data.repo.RunRepository.live] is `shareIn`-backed per session id) rather than
 * subscribing to the session stream itself, so a session's device-tool servicing rides the SAME
 * underlying SSE connection the chat transcript collector uses instead of opening a second one
 * (task brief: "multicast if needed, don't fork chat rendering" - chat rendering itself,
 * [com.mewbo.aura.data.model.TranscriptReducer]/`ChatViewModel.applyEvent`, is untouched by this
 * class). Runs on [scope] (app-lifetime), not the caller's own coroutine scope, so a client-side
 * `ChatViewModel.stop()` - which explicitly leaves the backend run itself going (v1 semantics,
 * `ChatViewModel.stop` doc) - does not also stop servicing that still-live run's device tool
 * calls; only a genuine session switch ([attach] to a different id, or [detach]) does.
 */
@Singleton
class DeviceToolExecutor @Inject constructor(
    private val resultReporter: DeviceToolResultReporter,
    private val callLedger: DeviceToolCallLedger,
    private val clock: DeviceClock,
    handlers: List<@JvmSuppressWildcards DeviceToolHandler>,
    @ApplicationScope private val scope: CoroutineScope,
) {
    /** Built from an injected [List] (assembled in [com.mewbo.aura.di.DeviceModule] for
     * production) rather than five named concrete handler constructor params - the concrete
     * handlers (`SetAlarmHandler` etc.) need a real Android `Context` and aren't constructible in
     * a plain-JVM unit test (no Robolectric, apps/mewbo_aura/CLAUDE.md); this list shape lets
     * executor tests pass trivial in-memory fakes instead. */
    private val handlers: Map<String, DeviceToolHandler> = handlers.associateBy { it.toolId }

    private var job: Job? = null

    /** (Re)starts dispatch for [sessionId] off [events], cancelling whatever session was
     * previously attached - callers never need to [detach] before a fresh [attach]. */
    fun attach(sessionId: String, events: Flow<SessionEvent>) {
        job?.cancel()
        job = scope.launch {
            events.collect { event ->
                // [handle] (including its network result POST) runs on its OWN child coroutine of
                // [scope] DIRECTLY - NOT `this@launch`/the collect job's own scope - for two
                // distinct reasons layered on top of each other:
                //
                // (1) Not awaited inline: [events] is `RunRepository.live`'s zero-buffer `shareIn`
                // `SharedFlow`, and a SharedFlow's emit() only advances once EVERY subscriber's
                // collect body returns. Awaiting handle() here would stall delivery to the OTHER
                // subscriber (ChatViewModel's transcript collector) for as long as a slow
                // handler/POST takes, freezing live chat rendering mid-turn (review finding, fix
                // round 1). Per-call ordering doesn't matter - calls are independent and
                // [callLedger] already guards a replayed call_id from double-executing.
                //
                // (2) Not parented to the COLLECT job either: if it were a child of `job` (plain
                // `launch{}`, implicitly `this@launch`), [detach]'s `job.cancel()` on a session
                // switch would cancel an ALREADY-DISPATCHED call mid-flight - between the
                // handler's real-world side effect (e.g. an SMS actually sent) and the result POST
                // that tells the server it succeeded. The server, never hearing back, would then
                // time out and have the model retry - a genuinely duplicate SMS (review finding
                // F8). Launching on [scope] directly makes a started call a SIBLING of `job`, not
                // its child, so it always runs to completion and reports regardless of what
                // happens to the session it was dispatched under.
                if (event is SessionEvent.DeviceToolCall) scope.launch { handle(sessionId, event.payload) }
            }
        }
    }

    /** Stops dispatch entirely - called on every real session switch/unbind (`ChatViewModel.bind`)
     * so a previous session's device tool calls are never serviced once the user has navigated
     * away from it. */
    fun detach() {
        job?.cancel()
        job = null
    }

    internal suspend fun handle(sessionId: String, call: DeviceToolCallPayload) {
        if (!callLedger.recordIfNew(call.callId)) return

        // A generous skew grace, not a strict `now > expiresAt` cut - the comparison is against
        // the DEVICE's own wall clock, which has no guaranteed sync with the server's (review
        // finding F5). Without grace, a phone clock running even ~30s ahead would classify every
        // single call as stale forever, permanently breaking every device tool with zero signal
        // to the user. A call dropped as stale is still reported (best-effort - the wire contract
        // already treats any non-200 result POST as terminal, never retried) so the server at
        // least learns the call failed instead of silently timing out with no explanation.
        if (clock.nowEpochSeconds() > call.expiresAt + STALENESS_GRACE_SECONDS) {
            postResult(
                sessionId,
                call,
                Outcome(
                    status = "error",
                    result = null,
                    error = DeviceToolErrorDto(code = "stale_call", message = "Call expired before the device could process it"),
                ),
            )
            return
        }

        postResult(sessionId, call, executeOutcome(call))
    }

    private suspend fun postResult(sessionId: String, call: DeviceToolCallPayload, outcome: Outcome) {
        resultReporter.report(
            sessionId = sessionId,
            callId = call.callId,
            request = DeviceToolResultRequest(
                callToken = call.callToken,
                status = outcome.status,
                result = outcome.result,
                error = outcome.error,
            ),
        )
    }

    private suspend fun executeOutcome(call: DeviceToolCallPayload): Outcome {
        val handler = handlers[call.toolId]
            ?: return Outcome(
                status = "error",
                result = null,
                error = DeviceToolErrorDto(code = "unknown_tool", message = "No handler for tool_id '${call.toolId}'"),
            )
        return try {
            Outcome(status = "ok", result = handler.execute(call.args), error = null)
        } catch (e: DeviceToolError) {
            Outcome(status = "error", result = null, error = DeviceToolErrorDto(code = e.code, message = e.message ?: "Error"))
        } catch (e: CancellationException) {
            // Gitea #181 fix wave, finding 2: this call runs as a SIBLING of `job` on `scope`
            // directly (see attach()'s own KDoc on why) - a genuine cancellation here means the
            // whole executor/app scope is going down, not a per-call handler failure. Reporting it
            // to the backend as a normal handler_error would be a lie; let it propagate instead.
            throw e
        } catch (e: Exception) {
            Outcome(
                status = "error",
                result = null,
                error = DeviceToolErrorDto(code = "handler_error", message = e.message ?: e.toString()),
            )
        }
    }

    private data class Outcome(val status: String, val result: JsonObject?, val error: DeviceToolErrorDto?)

    private companion object {
        const val STALENESS_GRACE_SECONDS = 60.0
    }
}
