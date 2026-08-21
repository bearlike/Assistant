package com.mewbo.aura.ui.control

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The veil's ORDER, which is the whole of its correctness — and the only part of it a device is not
 * needed for.
 *
 * Nothing here touches Android or Compose: [VeilFade] takes its three durations as constructor
 * arguments and returns the sequence as data, so a script is asserted step by step instead of being
 * slept through and photographed.
 */
class VeilFadeTest {

    /** Short and coarse so a script is small enough to read in a failure message: 4 fade frames. */
    private val fade = VeilFade(fadeMs = 40L, frameMs = 10L, windowSettleMs = 48L)

    // --- the ordering the capture depends on ---

    @Test
    fun `the fade is fully complete before the windows leave the frame`() {
        val steps = fade.hide()

        // Every step but the last is a visible fade frame; the hide is the last thing that happens.
        val fadeFrames = steps.dropLast(1)
        assertTrue("a hide with no fade frames is a cut", fadeFrames.isNotEmpty())
        assertTrue(
            "a window may not leave the frame mid-ramp: $steps",
            fadeFrames.all { it.visible },
        )
        assertEquals(
            "the ramp must reach full transparency before the hide",
            0f,
            fadeFrames.last().envelope,
            0f,
        )
    }

    @Test
    fun `the last hide step is hidden and held for the settle`() {
        // This is the state the capture — or the injected tap — runs under. A capture reading the
        // framebuffer while this step is in effect can catch neither a lit glow nor a half-faded
        // one, which is what makes the fade a sequence rather than a race against the shutter.
        val last = fade.hide().last()

        assertEquals(0f, last.envelope, 0f)
        assertTrue("the windows must be out of input dispatch, not merely transparent", !last.visible)
        assertEquals(48L, last.holdMs)
    }

    @Test
    fun `the restore brings the windows back transparent before ramping`() {
        val first = fade.show().first()

        // Visible again while still fully transparent: the window manager needs a frame to bring
        // the surface back, and a restore that started at rest opacity would pop.
        assertTrue(first.visible)
        assertEquals(0f, first.envelope, 0f)
    }

    @Test
    fun `the restore ends at exactly full envelope`() {
        // Exactly, not approximately. Each window's alpha is `restAlpha * envelope`, so a script
        // ending at 0.98 leaves an agent driving the phone behind a permanently dimmed
        // announcement — and no later event corrects it, because every subsequent veil ramps back
        // to the same wrong value.
        assertEquals(1f, fade.show().last().envelope, 0f)
        assertTrue(fade.show().last().visible)
    }

    // --- the envelope itself ---

    @Test
    fun `no step ever exceeds full envelope`() {
        // The load-bearing bound. The decoration window rests AT the Android 12 obscuring ceiling,
        // so an envelope above 1f restores it brighter than it was added — at which point it
        // silently swallows every touch on the screen, the user's and the agent's injected taps
        // alike, with nothing reporting a problem.
        (fade.hide() + fade.show()).forEach {
            assertTrue("envelope out of range: $it", it.envelope in 0f..1f)
        }
    }

    @Test
    fun `the hide ramp only ever dims and the show ramp only ever brightens`() {
        assertMonotonic(fade.hide().dropLast(1).map { it.envelope }, rising = false)
        assertMonotonic(fade.show().map { it.envelope }, rising = true)
    }

    @Test
    fun `the first hide step already dims`() {
        // A leading step at rest opacity would be a wasted frame — the ramp would start one frame
        // late and end with a visible jump to zero.
        assertTrue("the ramp starts at rest: ${fade.hide()}", fade.hide().first().envelope < 1f)
    }

    // --- degenerate durations still end in the right state ---

    @Test
    fun `a fade shorter than one frame still ends hidden and still restores fully`() {
        val instant = VeilFade(fadeMs = 0L, frameMs = 16L, windowSettleMs = 48L)

        // An empty ramp would strand the windows wherever the previous step left them — hidden, on
        // the restore path, which is the one failure this surface may never have.
        assertEquals(1f, instant.show().last().envelope, 0f)
        assertTrue(instant.show().last().visible)
        assertTrue(!instant.hide().last().visible)
        assertEquals(0f, instant.hide().last().envelope, 0f)
    }

    @Test
    fun `the default fade is at least four frames and completes before the settle begins`() {
        val steps = VeilFade().hide()
        val fadeFrames = steps.dropLast(1)

        // Four frames is roughly where a luminance ramp stops reading as a cut. The defaults are
        // chosen short because this runs on every injected tap, so this guards the floor, not the
        // ceiling.
        assertTrue("only ${fadeFrames.size} fade frames", fadeFrames.size >= 4)
        assertEquals(
            "the fade must cost its whole declared duration before the settle starts",
            VeilFade.FADE_MS,
            fadeFrames.sumOf { it.holdMs },
        )
        assertEquals(VeilFade.WINDOW_SETTLE_MS, steps.last().holdMs)
    }

    private fun assertMonotonic(values: List<Float>, rising: Boolean) {
        values.zipWithNext { a, b ->
            val ok = if (rising) b >= a else b <= a
            assertTrue("not monotonic at $a -> $b in $values", ok)
        }
    }
}
