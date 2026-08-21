package com.mewbo.aura.data.device.shizuku

/**
 * Decides when the screen has stopped moving after an action.
 *
 * At shell UID there is no push equivalent of `onAccessibilityEvent`, so the
 * only way to know a tap navigated somewhere is to look again. This is also the
 * only post-action feedback channel we have: `show_touches` renders nothing for
 * an injected event (injection enters below `PointerChoreographer`, the sole
 * stage that draws spots), so an action is verified by EFFECT — did anything
 * change — rather than by any indicator.
 *
 * **Bounded by construction, never `waitForIdle`.** `uiautomator2` disables
 * framework idle-waiting by default because a device that never idles — an
 * animation, a video, a blinking cursor, an ad — hangs the caller forever.
 * Inside a 30s dispatch budget that is a timeout generator, so this settles on
 * N identical reads OR a hard cap, whichever comes first, and the cap sits well
 * under the budget.
 *
 * **The real bound is [timeoutMs] PLUS one read**, because the deadline is
 * checked before starting another poll rather than mid-read — a read already
 * in flight is allowed to finish. That matters because the read is not free:
 * an element read measured ~2.0s on the dev container, so a settle there costs
 * ~7s rather than the ~1s the interval alone suggests. Still comfortably
 * inside 30s, and still incapable of running away, which is the property the
 * cap exists for.
 *
 * The clock is injected so the rule is testable without sleeping.
 */
class SettlePolicy(
    private val stableReads: Int = DEFAULT_STABLE_READS,
    private val pollIntervalMs: Long = DEFAULT_POLL_INTERVAL_MS,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
) {
    /**
     * Poll [read] until it returns the same value [stableReads] times running,
     * or [timeoutMs] elapses. Returns the last value read either way — a
     * timeout is not a failure, it is a screen that is still moving, and the
     * freshest observation is still the best answer available.
     */
    suspend fun <T> settle(
        now: () -> Long,
        sleep: suspend (Long) -> Unit,
        read: suspend () -> T,
    ): SettleResult<T> {
        val deadline = now() + timeoutMs
        var last = read()
        var identical = 1
        while (identical < stableReads && now() < deadline) {
            sleep(pollIntervalMs)
            val next = read()
            identical = if (next == last) identical + 1 else 1
            last = next
        }
        return SettleResult(value = last, settled = identical >= stableReads)
    }

    companion object {
        /** Three identical reads, 0.5s apart, 6.0s cap — leaves 24s of the
         * 30s dispatch budget for everything else in the round trip. */
        const val DEFAULT_STABLE_READS = 3
        const val DEFAULT_POLL_INTERVAL_MS = 500L
        const val DEFAULT_TIMEOUT_MS = 6_000L
    }
}

/** [SettlePolicy.settle]'s outcome: the freshest value, and whether it stabilised. */
data class SettleResult<T>(val value: T, val settled: Boolean)
