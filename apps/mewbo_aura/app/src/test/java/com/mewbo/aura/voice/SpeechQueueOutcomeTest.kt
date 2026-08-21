package com.mewbo.aura.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What a failed synthesis is allowed to do to the rest of a read-aloud queue.
 *
 * This is the rule `RemoteSynthesizer.synthesize` applies on every failure. It lives apart from the
 * synthesizer because that class needs `Context`, `AudioManager` and `MediaPlayer` and cannot be
 * built on a plain-JVM runner — the same extraction `DictationDecision` and `SendDecision` already
 * use. Without it this decision would be reachable only through an Android instrumentation run,
 * which in practice means never asserted at all.
 */
class SpeechQueueOutcomeTest {

    @Test
    fun `a capacity refusal is retried once, honouring the server's own delay`() {
        val outcome = SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(retryAfterSeconds = 5), attempt = 1)

        assertEquals(SpeechQueueOutcome.RetryAfter(5_000L), outcome)
    }

    @Test
    fun `a SECOND capacity refusal stops the run rather than retrying again`() {
        // By then the deployment is saturated rather than briefly busy, and retrying every
        // sentence of a long reply would turn a read into a series of stalls.
        val outcome = SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(retryAfterSeconds = 5), attempt = 2)

        assertEquals(SpeechQueueOutcome.StopRun, outcome)
    }

    @Test
    fun `every other failure stops the run immediately, with no retry`() {
        // Retrying a 502 or an undecodable clip is guessing: it fails the same way and the delay
        // buys nothing. Only the server's own "come back in N" is worth waiting on, because only
        // that one states the condition is transient.
        val failures = listOf(
            RuntimeException("gateway 502"),
            IllegalStateException("undecodable clip"),
            java.io.IOException("socket closed"),
        )

        for (failure in failures) {
            assertEquals("$failure", SpeechQueueOutcome.StopRun, SpeechQueueOutcome.forFailure(failure, attempt = 1))
        }
    }

    @Test
    fun `no failure ever means skip this sentence and carry on`() {
        // THE regression guard. "Skip and continue" was the original behaviour: a listener heard a
        // sentence vanish from the middle of a reply with nothing to indicate it happened. Every
        // outcome must either recover the sentence or end the read — never silently drop one.
        val outcomes = listOf(
            SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(5), attempt = 1),
            SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(5), attempt = 2),
            SpeechQueueOutcome.forFailure(RuntimeException(), attempt = 1),
            SpeechQueueOutcome.forFailure(RuntimeException(), attempt = 9),
        )

        assertTrue(
            "an outcome that is neither a retry nor a stop would be a silent skip",
            outcomes.all { it is SpeechQueueOutcome.RetryAfter || it == SpeechQueueOutcome.StopRun },
        )
    }

    @Test
    fun `an absurd Retry-After is capped rather than honoured verbatim`() {
        // The header is the server's to send and this client's to bound; an hour would hang a read
        // on a value we do not control.
        val outcome = SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(retryAfterSeconds = 3_600), attempt = 1)

        assertEquals(SpeechQueueOutcome.RetryAfter(SpeechQueueOutcome.MAX_RETRY_DELAY_MS), outcome)
    }

    @Test
    fun `a zero or negative Retry-After cannot spin a delay-free retry loop`() {
        assertEquals(SpeechQueueOutcome.RetryAfter(0L), SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(0), attempt = 1))
        assertEquals(SpeechQueueOutcome.RetryAfter(0L), SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(-5), attempt = 1))
        // Bounded anyway: a zero-delay retry happens at most ONCE, because attempt 2 stops.
        assertEquals(SpeechQueueOutcome.StopRun, SpeechQueueOutcome.forFailure(SpeechCapacityExhausted(0), attempt = 2))
    }
}
