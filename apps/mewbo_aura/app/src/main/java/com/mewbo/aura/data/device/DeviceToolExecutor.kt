package com.mewbo.aura.data.device

import com.mewbo.aura.data.api.DeviceToolErrorDto
import com.mewbo.aura.data.api.DeviceToolResultRequest
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.di.ApplicationScope
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject

/**
 * Answers one `device_tool_call`: dedup ([DeviceToolCallLedger]) -> staleness (`now > expires_at`)
 * -> run the matching [DeviceToolHandler] -> POST the result ([DeviceToolResultReporter]) - order
 * fixed by the task brief. A Hilt singleton (data/ layer, zero Compose/UI imports).
 *
 * **This class owns no subscription and no lifecycle - it is a pure dispatcher.** It is reached ONLY
 * as the [DeviceToolDispatch] binding that `buildMulticastLiveFlow` wires into a session's event
 * pipeline (an `onEach` UPSTREAM of the `shareIn`), so every path that follows a run - chat, the
 * assist overlay, whatever comes next - services device tool calls by construction. That placement
 * is load-bearing, and it replaced an earlier design where this class subscribed to the session's
 * `SharedFlow` itself; being a second SUBSCRIBER is what made all of the following possible, and all
 * of it is now structurally gone rather than defended against:
 *
 * - It pinned the subscriber count above zero forever, so `WhileSubscribed` could never reap an
 *   abandoned run's upstream - and `SessionStreamClient` reconnects on the server's idle-close, so
 *   that run would have gone on re-opening its SSE connection indefinitely.
 * - Ending that subscription on the stream's terminal event looked like the fix, but isn't sound:
 *   `SessionStreamClient` `trySend`s each frame and sets `terminated = true` REGARDLESS of whether
 *   the send landed, so under buffer pressure the `stream_end` frame is DROPPED while the loop still
 *   ends - no subscriber ever sees a terminal value, and the collector would leak anyway.
 * - Tracking one collector per session flow needed a map, an identity key, and a teardown race
 *   (an older collector's cleanup evicting the entry a newer one just installed => two collectors on
 *   one flow => the same `call_id` dispatched twice, with only [callLedger] between that and a
 *   duplicate SMS).
 *
 * A pipeline `onEach` has none of those failure modes: it lives and dies with the upstream itself,
 * whatever way that upstream dies, and each session's flow carries its own - so one session can
 * never cancel another's dispatch (the assist overlay opening mid-run must not stop a live chat
 * run's tools being answered). Being upstream of the `shareIn` also means dispatch does not depend
 * on WHO is subscribed at emit time, which a `replay = 0` `SharedFlow` otherwise makes racy.
 *
 * **A run's device tools are serviced exactly as long as somebody is following that run.** This
 * REVERSES an earlier claim in this KDoc that app-scope servicing continued past a client-side
 * `ChatViewModel.stop()`. It doesn't any more, deliberately: `stop()` and `bind()` both cancel the
 * transcript collector, the subscriber count falls to zero, `WhileSubscribed` stops the upstream and
 * the pipeline goes with it, so NEW calls on that run stop being answered (for `bind()` this is
 * exactly what the old, now-deleted `ChatViewModel` detach did - a restoration, not a regression).
 * That is the right trade: if the user pressed Stop, silently sending an SMS or launching the clock
 * app afterwards is far worse than letting the call time out server-side. An ALREADY-dispatched call
 * still runs to completion and reports - see [dispatch].
 *
 * Because a reconnect replays the FULL backlog (data/CLAUDE.md), the same `device_tool_call` reaches
 * [dispatch] again on every re-collection of the upstream; [callLedger]'s `recordIfNew` is what makes
 * that idempotent, and it always was - this placement doesn't add a new replay path, it inherits the
 * existing one.
 */
@Singleton
class DeviceToolExecutor @Inject constructor(
    private val resultReporter: DeviceToolResultReporter,
    private val callLedger: DeviceToolCallLedger,
    private val clock: DeviceClock,
    private val gate: DeviceToolGate,
    private val controlSession: DeviceControlSession,
    handlers: List<@JvmSuppressWildcards DeviceToolHandler>,
    @ApplicationScope private val scope: CoroutineScope,
) : DeviceToolDispatch {
    /** Built from an injected [List] (assembled in [com.mewbo.aura.di.DeviceModule] for
     * production) rather than five named concrete handler constructor params - the concrete
     * handlers (`SetAlarmHandler` etc.) need a real Android `Context` and aren't constructible in
     * a plain-JVM unit test (no Robolectric, apps/mewbo_aura/CLAUDE.md); this list shape lets
     * executor tests pass trivial in-memory fakes instead. */
    private val handlers: Map<String, DeviceToolHandler> = handlers.associateBy { it.toolId }

    /**
     * Starts answering [call] and returns IMMEDIATELY. Two distinct properties ride on that, layered
     * on top of each other - both were review findings, and neither survives making this suspend or
     * parenting the work to the caller:
     *
     * (1) **Never awaited inline.** This is called from the shared upstream's `onEach`, and a
     * `SharedFlow.emit` only advances once EVERY subscriber's collect body returns. Awaiting the
     * handler here would stall delivery to the transcript collector for as long as a slow
     * handler/result-POST takes, freezing live chat rendering mid-turn (review finding, fix round 1).
     * Per-call ordering doesn't matter - calls are independent, and [callLedger] already guards a
     * replayed `call_id` from double-executing.
     *
     * (2) **Launched on [scope] (app-lifetime), never on the caller's coroutine.** A started call
     * MUST outlive the stream that delivered it. The pipeline dies whenever the last subscriber goes
     * away (`WhileSubscribed`) - a Stop, a session switch, the overlay dismissing - and that can land
     * while a call is genuinely in flight: between the handler's real-world side effect (an SMS
     * actually SENT) and the result POST that tells the server it succeeded. Killed there, the server
     * never hears back, times out, and has the model retry - a genuinely duplicate SMS (review
     * finding F8). As a child of the pipeline's coroutine that is exactly what would happen; as a
     * sibling on [scope] it always runs to completion and reports, whatever happens to the stream it
     * arrived on.
     */
    override fun dispatch(sessionId: String, call: DeviceToolCallPayload) {
        scope.launch { handle(sessionId, call) }
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
        // the catalog already omits a user-disabled tool from advertisement, so a
        // dispatch for one can only come from a stale/misbehaving server - refuse it here rather than
        // silently running the handler. Checked BEFORE handler lookup: a disabled tool is by
        // definition a known one, so `tool_disabled` is the honest code (never `unknown_tool`).
        if (call.toolId in gate.disabledToolIds()) {
            return Outcome(
                status = "error",
                result = null,
                error = DeviceToolErrorDto(code = "tool_disabled", message = "Tool '${call.toolId}' is disabled in device settings"),
            )
        }
        // The ANSWER half of the grant. Its ADVERTISE half is
        // `DeviceToolCatalog.availableTools`, and both read the one
        // `DeviceControlSession` — the catalog asks whether control is possible,
        // this asks whether it is held. Two questions, one owner: the divergence
        // that shipped a playbook for absent tools came from two booleans
        // derived independently in two modules, not from asking twice.
        //
        // The refusal is DISCRIMINATED rather than a single "unavailable": never
        // started is the model's to fix in this same run, while a grant whose
        // substrate went away is the user's, and reporting the first for the
        // second sends both round a loop that cannot terminate — `start` would
        // refuse for the identical reason. The session owns that distinction.
        if (call.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS) {
            controlSession.controlRefusal()?.let { refusal ->
                return Outcome(
                    status = "error",
                    result = null,
                    error = DeviceToolErrorDto(code = refusal.code, message = refusal.message),
                )
            }
        }
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
            // this call runs on [scope] directly, never under the
            // stream that delivered it (see [dispatch]'s own KDoc on why) - so a genuine cancellation
            // here means the whole executor/app scope is going down, NOT that the run was stopped or
            // the session switched. Reporting that to the backend as a normal handler_error would be
            // a lie; let it propagate instead.
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
