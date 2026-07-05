package com.mewbo.aura.ui.orb

import kotlin.math.PI
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ShaderRotationAccumulatorTest {

    @Test
    fun `first call establishes the clock without advancing the angle`() {
        val accumulator = ShaderRotationAccumulator()
        val angle = accumulator.advance(timeSeconds = 5f, revsPerSec = 1f)
        assertEquals(0f, angle, 1e-6f)
    }

    @Test
    fun `advances proportionally to elapsed time and revolution speed`() {
        val accumulator = ShaderRotationAccumulator()
        accumulator.advance(timeSeconds = 0f, revsPerSec = 1f)
        val angle = accumulator.advance(timeSeconds = 0.25f, revsPerSec = 1f) // quarter turn at 1 rev/sec
        assertEquals((PI / 2).toFloat(), angle, 1e-3f)
    }

    @Test
    fun `wraps around at two pi`() {
        val accumulator = ShaderRotationAccumulator()
        accumulator.advance(timeSeconds = 0f, revsPerSec = 1f)
        val angle = accumulator.advance(timeSeconds = 1.25f, revsPerSec = 1f) // 1.25 full revolutions
        assertEquals((PI / 2).toFloat(), angle, 1e-3f)
        assertTrue(angle >= 0f && angle < (2 * PI).toFloat())
    }

    @Test
    fun `clamps a large dropped-frame gap instead of winding forward in one jump`() {
        val accumulator = ShaderRotationAccumulator()
        accumulator.advance(timeSeconds = 0f, revsPerSec = 1f)
        val angle = accumulator.advance(timeSeconds = 10f, revsPerSec = 1f) // huge gap, clamped to 0.25s
        assertEquals((PI / 2).toFloat(), angle, 1e-3f) // 0.25s clamp * 1 rev/sec = quarter turn only
    }
}
