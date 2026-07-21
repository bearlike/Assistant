package com.mewbo.aura.ui.composer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RmsWaveformMathTest {

    @Test
    fun `center bar is taller than an edge bar at the same signal level`() {
        val center = RmsWaveformMath.BarCount / 2
        val edge = 0
        val centerHeight = RmsWaveformMath.barTarget(center, rms = 1f, timeSeconds = 0f)
        val edgeHeight = RmsWaveformMath.barTarget(edge, rms = 1f, timeSeconds = 0f)
        assertTrue(centerHeight > edgeHeight)
    }

    @Test
    fun `shape is symmetric around the center index`() {
        val barCount = RmsWaveformMath.BarCount
        assertEquals(
            RmsWaveformMath.shape(0, barCount),
            RmsWaveformMath.shape(barCount - 1, barCount),
            1e-6f,
        )
    }

    @Test
    fun `silence never fully collapses a bar to zero height`() {
        val silent = RmsWaveformMath.barTarget(index = 0, rms = 0f, timeSeconds = 0f)
        assertTrue(silent > 0f)
    }

    @Test
    fun `bar target is monotonically non-decreasing in rms at a fixed time`() {
        val low = RmsWaveformMath.barTarget(index = 5, rms = 0.1f, timeSeconds = 1.5f)
        val high = RmsWaveformMath.barTarget(index = 5, rms = 0.9f, timeSeconds = 1.5f)
        assertTrue(high >= low)
    }

    @Test
    fun `bar target is clamped to the 0 to 1 range even for out-of-range input`() {
        val underflow = RmsWaveformMath.barTarget(index = 0, rms = -5f, timeSeconds = 0f)
        val overflow = RmsWaveformMath.barTarget(index = 0, rms = 5f, timeSeconds = 0f)
        assertTrue(underflow in 0f..1f)
        assertTrue(overflow in 0f..1f)
    }

    @Test
    fun `resting bars are uniform height across every index (a dot row, not a taper)`() {
        val rest = RmsWaveformMath.barTarget(index = 0, rms = 0f, timeSeconds = 0f)
        val center = RmsWaveformMath.barTarget(index = RmsWaveformMath.BarCount / 2, rms = 0f, timeSeconds = 0f)
        assertEquals(rest, center, 1e-6f)
    }

    @Test
    fun `resting bar height renders as a circular dot (Rev E F6 - height equals bar width)`() {
        val restHeightDp = RmsWaveformMath.barTarget(index = 0, rms = 0f, timeSeconds = 0f) * RmsWaveformMath.MaxBarHeight.value
        assertEquals(RmsWaveformMath.BarWidth.value, restHeightDp, 0.01f)
    }

    @Test
    fun `bar targets travel - the loudest bar index shifts over time at constant rms`() {
        fun argmax(t: Float) = (0 until RmsWaveformMath.BarCount)
            .maxBy { RmsWaveformMath.barTarget(it, rms = 1f, timeSeconds = t) }
        val indices = listOf(0f, 1.3f, 2.6f, 3.9f).map { argmax(it) }.toSet()
        assertTrue("pseudo-spectrum must move, not scale a fixed shape", indices.size > 1)
    }

    @Test
    fun `spring converges to a constant target without oscillating overshoot`() {
        val h = FloatArray(RmsWaveformMath.BarCount) { RmsWaveformMath.MinBarFraction }
        val v = FloatArray(RmsWaveformMath.BarCount)
        var crossings = 0
        var prevAbove = false
        repeat(240) { // 4 simulated seconds at 60fps against a frozen-time (constant) target
            RmsWaveformMath.stepBars(h, v, rms = 0.8f, timeSeconds = 5f, dtSeconds = 1f / 60f)
            val above = h[14] > RmsWaveformMath.barTarget(14, 0.8f, 5f)
            if (it > 0 && above != prevAbove) crossings++
            prevAbove = above
        }
        assertEquals(RmsWaveformMath.barTarget(14, 0.8f, 5f), h[14], 0.02f)
        assertTrue("critically damped - at most one crossing, not a ring", crossings <= 1)
    }

    @Test
    fun `energy spreads to neighbors - a spiked bar lifts adjacent bars`() {
        val h = FloatArray(RmsWaveformMath.BarCount) { RmsWaveformMath.MinBarFraction }
        val v = FloatArray(RmsWaveformMath.BarCount)
        h[14] = 1f
        val leftBefore = h[13]
        repeat(6) { RmsWaveformMath.stepBars(h, v, rms = 0f, timeSeconds = 0f, dtSeconds = 1f / 60f) }
        assertTrue("coupling must diffuse the spike outward", h[13] > leftBefore)
    }

    @Test
    fun `silence collapses every bar back to the uniform dot row`() {
        val h = FloatArray(RmsWaveformMath.BarCount) { 0.9f }
        val v = FloatArray(RmsWaveformMath.BarCount)
        repeat(600) { RmsWaveformMath.stepBars(h, v, rms = 0f, timeSeconds = 1f, dtSeconds = 1f / 60f) }
        h.forEach { assertEquals(RmsWaveformMath.MinBarFraction, it, 0.02f) }
    }

    @Test
    fun `a dropped frame cannot explode the integration - dt is clamped`() {
        val h = FloatArray(RmsWaveformMath.BarCount) { RmsWaveformMath.MinBarFraction }
        val v = FloatArray(RmsWaveformMath.BarCount)
        RmsWaveformMath.stepBars(h, v, rms = 1f, timeSeconds = 0f, dtSeconds = 4f)
        h.forEach { assertTrue(it in 0f..1f) }
    }

    @Test
    fun `a single large-but-clamped dt does not slam the spring to the ceiling (numerical stability)`() {
        // The clamp alone (MAX_STEP_SECONDS) does not make a stiff critically-damped spring
        // (SPRING_HZ) stable under one big semi-implicit-Euler step - explicit substepping inside
        // stepBars is what keeps a single janky/slow frame from overshooting straight to 1f
        // instead of springing smoothly. Reproduces the on-device redroid finding: every bar
        // reads uniformly maxed-out ("triangle" all over again) instead of a tapered pseudo-spectrum.
        val h = FloatArray(RmsWaveformMath.BarCount) { RmsWaveformMath.MinBarFraction }
        val v = FloatArray(RmsWaveformMath.BarCount)
        RmsWaveformMath.stepBars(h, v, rms = 0.667f, timeSeconds = 0.6f, dtSeconds = 1f / 20f)
        val center = RmsWaveformMath.BarCount / 2
        assertTrue("a single clamped-max step must not overshoot the spring to the ceiling", h[center] < 0.9f)
    }
}
