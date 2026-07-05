package com.mewbo.aura.ui.composer

import com.mewbo.aura.ui.theme.AuraMotion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RmsWaveformMathTest {

    @Test
    fun `center bar is taller than an edge bar at the same signal level`() {
        val center = RmsWaveformMath.BarCount / 2
        val edge = 0
        val centerHeight = RmsWaveformMath.heightFraction(center, smoothedRms = 1f)
        val edgeHeight = RmsWaveformMath.heightFraction(edge, smoothedRms = 1f)
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
        val silent = RmsWaveformMath.heightFraction(index = 0, smoothedRms = 0f)
        assertTrue(silent > 0f)
    }

    @Test
    fun `height fraction is monotonically non-decreasing in the smoothed rms signal`() {
        val low = RmsWaveformMath.heightFraction(index = 5, smoothedRms = 0.1f)
        val high = RmsWaveformMath.heightFraction(index = 5, smoothedRms = 0.9f)
        assertTrue(high >= low)
    }

    @Test
    fun `height fraction is clamped to the 0 to 1 range even for out-of-range input`() {
        val underflow = RmsWaveformMath.heightFraction(index = 0, smoothedRms = -5f)
        val overflow = RmsWaveformMath.heightFraction(index = 0, smoothedRms = 5f)
        assertTrue(underflow in 0f..1f)
        assertTrue(overflow in 0f..1f)
    }

    @Test
    fun `envelope spec duration matches the named M2 rms decay token`() {
        assertEquals(AuraMotion.rmsDecayMs, RmsWaveformMath.envelopeSpec().durationMillis)
    }

    @Test
    fun `resting bars are uniform height across every index (a dot row, not a taper)`() {
        val rest = RmsWaveformMath.heightFraction(index = 0, smoothedRms = 0f)
        val center = RmsWaveformMath.heightFraction(index = RmsWaveformMath.BarCount / 2, smoothedRms = 0f)
        assertEquals(rest, center, 1e-6f)
    }

    @Test
    fun `resting bar height renders as a circular dot (Rev E F6 - height equals bar width)`() {
        val restHeightDp = RmsWaveformMath.heightFraction(index = 0, smoothedRms = 0f) * RmsWaveformMath.MaxBarHeight.value
        assertEquals(RmsWaveformMath.BarWidth.value, restHeightDp, 0.01f)
    }
}
