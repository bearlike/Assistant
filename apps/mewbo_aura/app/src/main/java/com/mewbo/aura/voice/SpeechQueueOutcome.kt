package com.mewbo.aura.voice

/**
 * What a failed synthesis does to the REST of the speak-along queue.
 *
 * Extracted from [RemoteSynthesizer] for the reason `DictationDecision` and `SendDecision` were:
 * the class owning this logic needs `Context`, `AudioManager` and `MediaPlayer`, so it cannot be
 * constructed on a plain-JVM runner — and this decision is the part that can be silently wrong.
 * A rule that only ever runs inside an un-testable class is a rule nobody can hold to account.
 *
 * **There is deliberately no "skip this one and carry on" member.** That was the original
 * behaviour and it is the defect: continuing past a failure makes a listener hear a sentence
 * vanish from the middle of a reply with nothing to indicate it happened, which is worse than the
 * audio simply ending. Adding such a member back is the regression this union exists to prevent.
 */
sealed interface SpeechQueueOutcome {

    /** Wait, then try this SAME utterance once more. Only ever reached for the server's own
     * "at capacity, come back in N seconds". */
    data class RetryAfter(val delayMillis: Long) : SpeechQueueOutcome

    /** End the run: report this utterance failed, drop what is queued behind it, and refuse
     * anything a still-streaming reply enqueues afterwards. */
    data object StopRun : SpeechQueueOutcome

    companion object {
        /** A ceiling on the server-supplied delay. It sends 5; honouring an arbitrary value
         * verbatim would hang a read on a header this client does not control. */
        const val MAX_RETRY_DELAY_MS = 30_000L

        private const val MILLIS_PER_SECOND = 1_000L

        /** Attempts are 1-based: `attempt = 1` is the first try, so only it may earn a retry. */
        private const val FIRST_ATTEMPT = 1

        /**
         * [attempt] is 1-based. Exactly one failure kind is retryable, and only once.
         *
         * A [SpeechCapacityExhausted] is the server stating BOTH that the condition is transient
         * and how long to wait, which is the only basis on which waiting is better than guessing.
         * Everything else — a gateway 502, an undecodable clip, a dead socket — will fail the
         * same way a second time, so a retry spends the delay for nothing. And a SECOND capacity
         * refusal means the deployment is genuinely saturated rather than briefly busy; retrying
         * each sentence of a long reply would turn a read into a series of stalls.
         */
        fun forFailure(failure: Throwable, attempt: Int): SpeechQueueOutcome {
            if (failure !is SpeechCapacityExhausted || attempt != FIRST_ATTEMPT) return StopRun
            val requested = failure.retryAfterSeconds * MILLIS_PER_SECOND
            return RetryAfter(requested.coerceIn(0, MAX_RETRY_DELAY_MS))
        }
    }
}
