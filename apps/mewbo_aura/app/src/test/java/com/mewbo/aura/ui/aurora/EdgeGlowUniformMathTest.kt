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

    // Color is a plain two-stop blend by the bloom's own intensity (see AuroraEdgeGlow.kt's
    // shader), never an angle/phase-driven wheel - that removes the whole periodic-wrong-hue bug
    // class rather than bounding it. The tests below cover the two things that DO vary: horizontal
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

    // ---- The border profile (`perimeterBias`) ----
    // The whole design rests on one claim: at bias 0 every derived term is an EXACT identity, so
    // chat and the assist overlay render the same bytes they did before the knob existed. "Close
    // enough" is not the claim and a delta would not be testing it — these assert exact equality,
    // which the arithmetic supports (`1 + (k - 1) * 0` is `1` in IEEE, and `x * 1` is exact).

    @Test
    fun `at zero bias every derived term is exactly its unbiased value`() {
        assertEquals(0f, EdgeGlowUniformMath.borderAmount(0f))
        assertEquals(1f, EdgeGlowUniformMath.bottomWeight(0f))
        assertEquals(1f, EdgeGlowUniformMath.reachFraction(0f))
        assertEquals(1f, EdgeGlowUniformMath.perimeterGain(0f))
    }

    @Test
    fun `at zero bias the center weight is the state's own strength, untouched`() {
        for (state in ALL_STATES) {
            assertEquals(
                "centerWeight($state) must be byte-identical at bias 0",
                EdgeGlowUniformMath.centerWeightStrength(state),
                EdgeGlowUniformMath.centerWeight(state, override = null, perimeterBias = 0f),
            )
        }
        // The in-app chat passes a near-zero override rather than the state default; the identity
        // has to hold for the value the caller actually supplies, not just for the default path.
        assertEquals(
            0.02f,
            EdgeGlowUniformMath.centerWeight(EdgeGlowState.Thinking, override = 0.02f, perimeterBias = 0f),
        )
    }

    @Test
    fun `full bias levels the bottom, gains the perimeter, contracts the reach and flattens the center`() {
        assertEquals(1f, EdgeGlowUniformMath.borderAmount(1f))
        assertTrue(
            "the bottom-anchored term must never be BRIGHTENED past the bottom-anchored balance — " +
                "that is the bottom wash again, louder",
            EdgeGlowUniformMath.bottomWeight(1f) <= 1f,
        )
        assertTrue(
            "the perimeter floor must be gained UP — damping the bottom alone just dims the surface",
            EdgeGlowUniformMath.perimeterGain(1f) > 1f,
        )
        assertTrue(
            "the reach must contract — a border at a haze's decay length is a haze",
            EdgeGlowUniformMath.reachFraction(1f) < 1f,
        )
        // A border's bottom edge is EVEN; a center-weighted one dips between its bright centre and
        // its bright corners. Flattened to exactly 0 whatever the state or the caller's override.
        for (state in ALL_STATES) {
            assertEquals(0f, EdgeGlowUniformMath.centerWeight(state, override = null, perimeterBias = 1f))
        }
        assertEquals(
            0f,
            EdgeGlowUniformMath.centerWeight(EdgeGlowState.Thinking, override = 0.9f, perimeterBias = 1f),
        )
    }

    /**
     * The identity that makes the border EVEN rather than a bottom wash with brighter edges: the
     * biased bottom weight IS the biased rail peak, so both peak at the same value the whole way
     * round. Retuning the gain without carrying the bottom weight with it is what this catches.
     */
    @Test
    fun `at full bias the bottom edge and the side rails peak at the same value`() {
        val railPeak = EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Listening(0f)) *
            EdgeGlowUniformMath.perimeterGain(1f)

        assertEquals(railPeak, EdgeGlowUniformMath.bottomWeight(1f), 1e-6f)
    }

    /**
     * The device report this round answers: the border read "practically invisible" because the
     * rails were gained to 0.70 of the shader's glow term and the bottom was pulled DOWN to match,
     * so evenness was bought at 70% luminance. The parity level is the surface's whole brightness
     * and the rest of the chain (`iIntensity`, `peakAlpha`, the window's obscuring cap) only takes
     * away from it — so this asserts the border spends all of the one factor it owns.
     *
     * Deliberately an EXACT equality: `0.35f * (1f / 0.35f)` is exactly 1.0 in float32, so a
     * tolerance here would quietly accept a re-lowered gain that lands nearby.
     */
    @Test
    fun `at full bias the rails reach the glow term's full strength`() {
        val railPeak = EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Listening(0f)) *
            EdgeGlowUniformMath.perimeterGain(1f)

        assertEquals(1f, railPeak)
    }

    /**
     * One clamp, applied in [EdgeGlowUniformMath.borderAmount], so the shader's own `iPerimeterBias`
     * and every CPU-side term derived from it can never disagree about the domain — a caller passing
     * 1.5 must not push the bottom weight below its biased floor while the shader saturates at 1.
     */
    @Test
    fun `the bias domain is clamped once, and every derived term follows it`() {
        assertEquals(0f, EdgeGlowUniformMath.borderAmount(-0.5f))
        assertEquals(1f, EdgeGlowUniformMath.borderAmount(1.5f))
        assertEquals(0.4f, EdgeGlowUniformMath.borderAmount(0.4f))

        assertEquals(EdgeGlowUniformMath.bottomWeight(0f), EdgeGlowUniformMath.bottomWeight(-0.5f))
        assertEquals(EdgeGlowUniformMath.reachFraction(0f), EdgeGlowUniformMath.reachFraction(-0.5f))
        assertEquals(EdgeGlowUniformMath.perimeterGain(0f), EdgeGlowUniformMath.perimeterGain(-0.5f))
        assertEquals(EdgeGlowUniformMath.bottomWeight(1f), EdgeGlowUniformMath.bottomWeight(1.5f))
        assertEquals(EdgeGlowUniformMath.reachFraction(1f), EdgeGlowUniformMath.reachFraction(1.5f))
        assertEquals(EdgeGlowUniformMath.perimeterGain(1f), EdgeGlowUniformMath.perimeterGain(1.5f))
    }

    /**
     * The bias is a continuous rebalance, not a two-state switch — every term has to move
     * monotonically between its endpoints or a caller part-way along gets a shape neither profile
     * describes. A constant-returning stub passes each endpoint test above in isolation; nothing
     * passes both those and this.
     *
     * **`bottomWeight` is asserted NON-INCREASING, not strictly falling, and that is a statement
     * about the design rather than a weakened assertion.** At the current parity of 1.0 the bottom
     * term is level with the rails, so the damping sits at its identity and the function IS
     * constant — no test can distinguish it from a stub while that holds. What keeps the bottom
     * from swallowing the lower third at full bias is the reach contraction, asserted strictly
     * below. Restore the strict form the moment parity drops under 1.
     */
    @Test
    fun `every term moves monotonically between the two profiles`() {
        val biases = (0..10).map { it / 10f }
        val bottom = biases.map { EdgeGlowUniformMath.bottomWeight(it) }
        val reach = biases.map { EdgeGlowUniformMath.reachFraction(it) }
        val gain = biases.map { EdgeGlowUniformMath.perimeterGain(it) }

        assertTrue("bottom weight must never RISE with the bias", bottom.zipWithNext().all { it.first >= it.second })
        assertTrue("reach must contract monotonically", reach.zipWithNext().all { it.first > it.second })
        assertTrue("perimeter gain must rise monotonically", gain.zipWithNext().all { it.first < it.second })
    }

    @Test
    fun `perimeter floor is edge-lit in every live state and zero when hidden`() {
        assertEquals(0f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Hidden))
        assertEquals(0.35f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Listening(0f)))
        assertEquals(0.30f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Resting))
        assertEquals(0.15f, EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Thinking))
        assertTrue(EdgeGlowUniformMath.perimeterFloor(EdgeGlowState.Igniting(0.5f)) > 0f)
    }

    private companion object {
        /** Every arm of the state union, so a bias identity is asserted over all of them rather
         * than over the one a test author happened to pick. Adding a state without extending this
         * leaves its identity unasserted, which the `when`s themselves will not catch. */
        val ALL_STATES = listOf(
            EdgeGlowState.Hidden,
            EdgeGlowState.Igniting(0.5f),
            EdgeGlowState.Listening(5f),
            EdgeGlowState.Thinking,
            EdgeGlowState.Resting,
        )
    }
}
