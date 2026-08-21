package com.mewbo.aura.ui.settings

import com.mewbo.aura.voice.SpeechBoostState
import com.mewbo.aura.voice.SpeechVolumeBoost
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * How the volume-boost row reads, and — the part that matters — what it refuses to claim.
 *
 * Pure, no Compose: both claims are functions of a level and a state, exactly like
 * [resolveSpeechEngineName]'s suite.
 */
class VolumeBoostRowTest {

    @Test
    fun `zero reads as Off, never as a level`() {
        assertEquals("Off", resolveVolumeBoostLabel(0))
    }

    @Test
    fun `a level is SIGNED, because this control only ever adds`() {
        // "6 dB" beside a volume label reads as an absolute level the device is being set to.
        assertEquals("+6 dB", resolveVolumeBoostLabel(6))
        assertEquals("+20 dB", resolveVolumeBoostLabel(20))
    }

    @Test
    fun `every offered level renders`() {
        SpeechVolumeBoost.LEVELS_DECIBELS.forEach { level ->
            val label = resolveVolumeBoostLabel(level)
            assertTrue("level $level rendered blank", label.isNotBlank())
            if (level != SpeechVolumeBoost.OFF_DECIBELS) {
                assertTrue("level $level lost its sign", label.startsWith("+"))
            }
        }
    }

    @Test
    fun `the row claims nothing before an attach has been attempted`() {
        // The screen's own law: a status indicator that guesses is worse than none. Before any
        // speech there is genuinely no answer, so no badge may appear.
        assertFalse(SpeechBoostState.Untested.refuses(6))
    }

    @Test
    fun `a successful attach also claims nothing`() {
        // Deliberate, and the subtlest rule here: the effect existing on a session is NOT proof
        // that the selected engine's audio passes through it — an on-device TTS engine that plays
        // its own audio never sees the session id at all. A "Supported" badge here would be the
        // wrong-green this screen exists to prevent, so `Applied` renders as no badge.
        assertFalse(SpeechBoostState.Applied(6).refuses(6))
    }

    @Test
    fun `only a measured refusal of the CURRENT level surfaces`() {
        assertTrue(SpeechBoostState.Refused(6).refuses(6))
        assertFalse("a refusal of a level since changed is not a current one", SpeechBoostState.Refused(6).refuses(15))
    }
}
