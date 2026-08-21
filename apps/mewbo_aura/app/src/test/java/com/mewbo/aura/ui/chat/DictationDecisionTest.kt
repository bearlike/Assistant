package com.mewbo.aura.ui.chat

import com.mewbo.aura.voice.TranscriberError
import com.mewbo.aura.voice.TranscriberEvent
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [DictationDecision.next] is the pure `TranscriberEvent` -> [DictationState] mapping
 * [ChatViewModel.startDictation] applies on every event its collected `Transcriber` flow emits
 * - covered directly here since `ChatViewModel` can't be constructed in a plain JVM
 * test (no Robolectric in this module, same reasoning [SendDecisionTest] documents).
 */
class DictationDecisionTest {

    @Test
    fun `Ready (re)enters Listening with no partial and silent rms`() {
        assertEquals(
            DictationState.Listening(),
            DictationDecision.next(DictationState.Idle, TranscriberEvent.Ready),
        )
    }

    @Test
    fun `Rms while Idle starts Listening at the normalized amplitude`() {
        // -2f is the bottom of the empirically-observed range (matches FakeTranscriber's own
        // script) - normalizes to 0f.
        assertEquals(
            DictationState.Listening(rmsDb = 0f),
            DictationDecision.next(DictationState.Idle, TranscriberEvent.Rms(-2f)),
        )
    }

    @Test
    fun `Rms preserves an already-landed partial - an amplitude tick must not blank the words shown`() {
        val current = DictationState.Listening(partial = "hey mewbo", rmsDb = 0.1f)
        assertEquals(
            DictationState.Listening(partial = "hey mewbo", rmsDb = 1f),
            DictationDecision.next(current, TranscriberEvent.Rms(10f)), // 10f is the top of the range -> 1f
        )
    }

    @Test
    fun `Rms above the observed range clamps to 1f, below it clamps to 0f`() {
        assertEquals(1f, (DictationDecision.next(DictationState.Idle, TranscriberEvent.Rms(50f)) as DictationState.Listening).rmsDb)
        assertEquals(0f, (DictationDecision.next(DictationState.Idle, TranscriberEvent.Rms(-50f)) as DictationState.Listening).rmsDb)
    }

    @Test
    fun `Partial preserves the current rms - a new word must not reset the amplitude to silence`() {
        val current = DictationState.Listening(partial = null, rmsDb = 0.75f)
        assertEquals(
            DictationState.Listening(partial = "hey", rmsDb = 0.75f),
            DictationDecision.next(current, TranscriberEvent.Partial("hey")),
        )
    }

    @Test
    fun `a second Partial REPLACES the first rather than appending - SpeechRecognizer's own partials are cumulative`() {
        val afterFirst = DictationDecision.next(DictationState.Listening(), TranscriberEvent.Partial("hey"))
        val afterSecond = DictationDecision.next(afterFirst, TranscriberEvent.Partial("hey mewbo, what's up"))
        assertEquals(DictationState.Listening(partial = "hey mewbo, what's up"), afterSecond)
    }

    @Test
    fun `Final surfaces the transcript as a one-shot Final regardless of the prior state`() {
        assertEquals(
            DictationState.Final("hello there"),
            DictationDecision.next(DictationState.Listening(partial = "hello there"), TranscriberEvent.Final("hello there")),
        )
    }

    @Test
    fun `EVERY Error variant reverts to Idle, ServiceFailed included`() {
        // Exhaustive over the enum on purpose: a new code added without a decision here would
        // otherwise fall through whatever branch happened to be last. The composer always returns
        // to rest; which errors additionally SAY something is a side effect at the call site
        // (`ChatViewModel.startDictation`), never part of this pure mapping — the same split
        // `Unavailable`'s mic-disable already lives on.
        val listening = DictationState.Listening(partial = "partial", rmsDb = 0.5f)
        for (code in TranscriberError.entries) {
            assertEquals("$code", DictationState.Idle, DictationDecision.next(listening, TranscriberEvent.Error(code)))
        }
    }
}
