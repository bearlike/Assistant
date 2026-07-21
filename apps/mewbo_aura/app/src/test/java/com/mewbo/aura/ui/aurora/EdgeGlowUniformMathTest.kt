package com.mewbo.aura.ui.aurora

import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class EdgeGlowUniformMathTest {

    @Test
    fun `hidden is the only state that targets invisible`() {
        assertEquals(0f, EdgeGlowUniformMath.targetVisible(EdgeGlowState.Hidden))
        assertEquals(1f, EdgeGlowUniformMath.targetVisible(EdgeGlowState.Igniting(0.5f)))
        assertEquals(1f, EdgeGlowUniformMath.targetVisible(EdgeGlowState.Listening(5f)))
        assertEquals(1f, EdgeGlowUniformMath.targetVisible(EdgeGlowState.Thinking))
    }

    @Test
    fun `ignite progress passes through the state's own progress, clamped to 0 to 1`() {
        assertEquals(0f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Igniting(-1f), reducedMotion = false))
        assertEquals(0.42f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Igniting(0.42f), reducedMotion = false))
        assertEquals(1f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Igniting(2f), reducedMotion = false))
    }

    @Test
    fun `states other than igniting always read as fully revealed`() {
        assertEquals(1f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Listening(5f), reducedMotion = false))
        assertEquals(1f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Thinking, reducedMotion = false))
        assertEquals(1f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Hidden, reducedMotion = false))
    }

    @Test
    fun `reduced motion forces the sweep to its completed frame regardless of carried progress`() {
        assertEquals(1f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Igniting(0.1f), reducedMotion = true))
    }

    @Test
    fun `listening breathes plus or minus the named amplitude around a resting baseline of 1`() {
        // At t=0 the breathe sine is 0, so intensity sits exactly at the resting midpoint.
        val resting = EdgeGlowUniformMath.intensity(EdgeGlowState.Listening(5f), timeSeconds = 0f, reducedMotion = false)
        assertEquals(1f, resting, 1e-4f)

        // Sample densely over one full breathe period and confirm it actually reaches near both
        // the peak and trough the named amplitude promises - not just "varies somehow".
        val periodSeconds = AuraMotion.listeningBreathePeriodMs / 1000f
        val samples = (0..200).map { i ->
            EdgeGlowUniformMath.intensity(
                state = EdgeGlowState.Listening(5f),
                timeSeconds = i * periodSeconds / 200f,
                reducedMotion = false,
            )
        }
        assertTrue((samples.maxOrNull()!! - 1f) > AuraMotion.listeningBreatheAmplitude * 0.9f)
        assertTrue((1f - samples.minOrNull()!!) > AuraMotion.listeningBreatheAmplitude * 0.9f)
    }

    @Test
    fun `louder rms widens the breathe swing (M2 - loosely synced to RMS)`() {
        val periodSeconds = AuraMotion.listeningBreathePeriodMs / 1000f
        fun peakDeviation(rmsDb: Float): Float {
            val samples = (0..200).map { i ->
                EdgeGlowUniformMath.intensity(
                    state = EdgeGlowState.Listening(rmsDb),
                    timeSeconds = i * periodSeconds / 200f,
                    reducedMotion = false,
                )
            }
            return samples.maxOrNull()!! - 1f
        }

        val quiet = peakDeviation(rmsDb = 0f)
        val loud = peakDeviation(rmsDb = 10f)

        assertTrue("louder rms should widen the breathe swing", loud > quiet)
        // At rms=0 the swing should sit right at the named base amplitude - no boost yet, so the
        // "dead field" bug (rms silently ignored) can't creep back in disguised as a no-op at zero.
        assertEquals(AuraMotion.listeningBreatheAmplitude, quiet, 1e-3f)
    }

    @Test
    fun `reduced motion freezes listening intensity to a flat baseline`() {
        val frozen = EdgeGlowUniformMath.intensity(EdgeGlowState.Listening(9f), timeSeconds = 3.7f, reducedMotion = true)
        assertEquals(1f, frozen)
    }

    @Test
    fun `thinking intensifies above the resting baseline`() {
        val thinking = EdgeGlowUniformMath.intensity(EdgeGlowState.Thinking, timeSeconds = 0f, reducedMotion = false)
        assertTrue(thinking > 1f)
    }

    @Test
    fun `visibility transition duration derives from the M1 sweep and dismiss-speed tokens`() {
        val expected = (AuraMotion.edgeSweepMs / AuraMotion.dismissSpeedMultiplier).toInt()
        assertEquals(expected, EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS)
    }

    // the old angle-based 4-stop hue wheel (and its huePhase/RESTING_EXTENT_FRACTION
    // uniform math) is gone entirely - color is now a plain two-stop blend by the bloom's own
    // intensity (see AuroraEdgeGlow.kt's shader), which removes the whole periodic-wrong-hue bug
    // class rather than bounding it. The tests below cover what replaced it: horizontal
    // center-weighting and the waviness noise modulation.

    @Test
    fun `center weight never fully darkens the corners in any state, thinking suppresses harder`() {
        // The shader computes horizontalWeight = 1 - centerWeight * smoothstep(...), so at the far
        // corners (smoothstep saturates to 1) brightness floors at 1 - centerWeight. Strictly less
        // than 1 for every state guarantees that floor is always > 0 - never a hard vignette to zero,
        // even for Thinking's "no corner reach" (reads as none, isn't literally zero).
        for (state in listOf(EdgeGlowState.Hidden, EdgeGlowState.Igniting(1f), EdgeGlowState.Listening(5f), EdgeGlowState.Thinking)) {
            assertTrue("centerWeightStrength($state) must stay < 1", EdgeGlowUniformMath.centerWeightStrength(state) in 0f..<1f)
        }
        // Thinking suppresses corners harder than Listening ("no corner
        // reach" vs "faint reach").
        assertTrue(
            EdgeGlowUniformMath.centerWeightStrength(EdgeGlowState.Thinking) >
                EdgeGlowUniformMath.centerWeightStrength(EdgeGlowState.Listening(5f)),
        )
    }

    @Test
    fun `decay depth contracts for thinking, igniting targets listening's wide resting reach`() {
        // idle-listening reads WIDE (measured reach into the corners),
        // streaming/thinking contracts to hug the pill. Igniting is the entry animation immediately
        // before Listening begins, so it targets Listening's reach, not its own.
        assertEquals(AuraColors.auroraOverlayBloomDecayDepth, EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Listening(5f)))
        assertEquals(
            AuraColors.auroraOverlayBloomDecayDepth,
            EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Igniting(0.5f)),
        )
        assertTrue(
            "thinking's decay depth must contract below listening's wide reach",
            EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Thinking) < EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Listening(5f)),
        )
    }

    @Test
    fun `wave amplitude is a subtle modulation, not a dominant distortion of the base falloff`() {
        assertTrue(EdgeGlowUniformMath.WAVE_AMPLITUDE in 0f..0.5f)
    }

    @Test
    fun `wave frequency scales inversely with draw width (a fixed cycle count per device)`() {
        val narrow = EdgeGlowUniformMath.waveFreqX(widthPx = 1000f)
        val wide = EdgeGlowUniformMath.waveFreqX(widthPx = 2000f)
        assertEquals(narrow / 2f, wide, 1e-6f)
    }

    @Test
    fun `wave frequency is defensively zero for a non-positive width`() {
        assertEquals(0f, EdgeGlowUniformMath.waveFreqX(widthPx = 0f))
        assertEquals(0f, EdgeGlowUniformMath.waveFreqX(widthPx = -10f))
    }

    @Test
    fun `wave drift is disabled under reduced motion, present otherwise`() {
        assertEquals(0f, EdgeGlowUniformMath.waveSpeedHz(reducedMotion = true))
        assertTrue(EdgeGlowUniformMath.waveSpeedHz(reducedMotion = false) > 0f)
    }

    @Test
    fun `resting is a visible, static, low-intensity pool between thinking and listening reach`() {
        assertEquals(1f, EdgeGlowUniformMath.targetVisible(EdgeGlowState.Resting))
        assertEquals(1f, EdgeGlowUniformMath.igniteProgress(EdgeGlowState.Resting, reducedMotion = false))
        val t0 = EdgeGlowUniformMath.intensity(EdgeGlowState.Resting, timeSeconds = 0f, reducedMotion = false)
        val t3 = EdgeGlowUniformMath.intensity(EdgeGlowState.Resting, timeSeconds = 3.3f, reducedMotion = false)
        assertTrue("resting must sit below the active baseline", t0 < 1f && t0 > 0f)
        assertEquals("resting must not breathe/oscillate", t0, t3)
        val resting = EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Resting)
        assertTrue(resting < EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Listening(5f)))
        assertTrue(resting > EdgeGlowUniformMath.decayDepthDp(EdgeGlowState.Thinking))
    }

    @Test
    fun `resting carries its own transition key`() {
        assertEquals(4, EdgeGlowUniformMath.transitionKey(EdgeGlowState.Resting))
    }

    @Test
    fun `perimeter floor is edge-lit in every live state and zero when hidden`() {
        assertEquals(0f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Hidden))
        assertEquals(0.35f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Listening(0f)))
        assertEquals(0.30f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Resting))
        assertEquals(0.15f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Thinking))
        assertTrue(EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Igniting(0.5f)) > 0f)
    }
}
