package com.mewbo.aura.data.device

import com.mewbo.aura.data.model.DeviceToolCallPayload

/**
 * Single-method seam over [DeviceToolExecutor]'s per-call dispatch -
 * [com.mewbo.aura.data.repo.RunRepository] depends on this narrow interface rather than the whole
 * executor (which drags in the handler list, the ledger, the clock and the result reporter) so its
 * own tests can substitute a trivial fake, the same reason [DeviceToolResultReporter] exists instead
 * of the executor depending on the full `AuraApi`.
 *
 * Why the repository dispatches at all: `device_*` tools are advertised to the server on EVERY
 * `/query` ([com.mewbo.aura.data.repo.RunRepository.sendQuery] -> [DeviceToolCatalog.availableTools]),
 * so EVERY entry point that follows a run must also be able to ANSWER a `device_tool_call` - the
 * server blocks on one for its full `DEVICE_TOOL_TIMEOUT_S` (30s) otherwise and hands the model a
 * `device_timeout`. Dispatch is therefore wired into the session's event PIPELINE itself
 * (`buildMulticastLiveFlow`'s `onEach`, upstream of the `shareIn`), not into any one client: you
 * cannot obtain a session's event flow without dispatch already being part of it, which makes
 * "advertised, but nobody answering" structurally impossible instead of a rule each new caller has
 * to remember. It shipped the other way round exactly once - the executor was subscribed from
 * `ChatViewModel` and nowhere else, so the assist overlay (its in-overlay first turn, which
 * follows the run itself and never launches the chat) advertised all nine tools and answered none of
 * them: every device tool call from the overlay burned the full 30s server timeout, and a turn
 * needing two of them never got past the first.
 *
 * **[dispatch] MUST NOT suspend, and must not throw.** It is called from inside the shared upstream's
 * `onEach`, so anything awaited here would stall the whole pipeline - and a `SharedFlow.emit` only
 * advances once every subscriber's collect body returns, so a slow handler/POST would freeze live
 * chat rendering mid-turn. [DeviceToolExecutor.dispatch] launches the call on its own app-lifetime
 * scope and returns immediately; see there for why that launch is also what keeps an in-flight call
 * alive when the stream it arrived on dies (review finding F8).
 */
fun interface DeviceToolDispatch {
    fun dispatch(sessionId: String, call: DeviceToolCallPayload)
}
