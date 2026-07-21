package com.mewbo.aura.voice

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [InputModality.fromExtra] is the one seam that turns the assist-overlay handoff's plain-string
 * `EXTRA_HANDOFF_MODALITY` extra back into a real [InputModality] ([ChatViewModel][com.mewbo.aura.ui.chat.ChatViewModel].bind
 * is the only caller) - covering it directly here since it's the actual parse the
 * production code runs, not just an implementation detail of a bigger class.
 */
class InputModalityTest {

    @Test
    fun `fromExtra parses the Voice name`() {
        assertEquals(InputModality.Voice, InputModality.fromExtra("Voice"))
    }

    @Test
    fun `fromExtra parses the Text name`() {
        assertEquals(InputModality.Text, InputModality.fromExtra("Text"))
    }

    @Test
    fun `fromExtra defaults to Text for null - the ordinary non-handoff bind case`() {
        assertEquals(InputModality.Text, InputModality.fromExtra(null))
    }

    @Test
    fun `fromExtra defaults to Text for an unrecognized string rather than throwing`() {
        assertEquals(InputModality.Text, InputModality.fromExtra("garbage"))
    }
}
