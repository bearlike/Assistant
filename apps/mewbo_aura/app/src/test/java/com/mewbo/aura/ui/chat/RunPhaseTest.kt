package com.mewbo.aura.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [RunPhase.isRunInFlight] is the ComposerOptionsSheet scope-freeze gate: the
 * Project/Tools picks freeze ONLY while a turn is actively Sending/Streaming, not for the entire
 * lifetime of an already-created session.
 */
class RunPhaseTest {

    @Test
    fun `sending and streaming are the only in-flight phases`() {
        assertTrue(RunPhase.Sending.isRunInFlight)
        assertTrue(RunPhase.Streaming.isRunInFlight)
    }

    @Test
    fun `idle done and error carry no live run - scope stays editable`() {
        assertFalse(RunPhase.Idle.isRunInFlight)
        assertFalse(RunPhase.Done.isRunInFlight)
        assertFalse(RunPhase.Error.isRunInFlight)
    }

    @Test
    fun `the in-flight set is exactly Sending and Streaming, across the whole enum`() {
        // This extension is the SINGLE spelling of {Sending, Streaming} — `ChatScreen`'s composer
        // gate and `ChatTranscript`'s `isRunLive` (the spark + the disclaimer gate) both read it,
        // rather than each re-spelling the comparison. An inline copy drifting from this one
        // desyncs the spark from the disclaimer, and neither failure announces itself.
        //
        // Asserted over `entries` rather than phase-by-phase so a NEW RunPhase cannot be added
        // without deciding, here, whether it carries a live run.
        assertEquals(listOf(RunPhase.Sending, RunPhase.Streaming), RunPhase.entries.filter { it.isRunInFlight })
    }
}
