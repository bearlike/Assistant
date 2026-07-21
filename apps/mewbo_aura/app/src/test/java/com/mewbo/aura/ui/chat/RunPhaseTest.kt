package com.mewbo.aura.ui.chat

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
}
